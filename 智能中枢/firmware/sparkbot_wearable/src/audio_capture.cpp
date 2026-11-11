#ifdef AUDIO_CAPTURE

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <esp32-hal-psram.h>
#include <esp_err.h>
#include <esp_system.h>
#include <math.h>

#include "board_config.h"
#include "wake_sleep_phrases.h"
#include "wozai_audio.h"

#ifdef PET_FOUNTAIN
#include "fountain_display.h"
#include "face_display.h"
#include "music_player.h"
#endif

#if __has_include("wifi_config.h")
#include "wifi_config.h"
#endif

#ifndef SPARKBOT_WIFI_SSID
#define SPARKBOT_WIFI_SSID ""
#endif

#ifndef SPARKBOT_WIFI_PASSWORD
#define SPARKBOT_WIFI_PASSWORD ""
#endif

#ifndef SPARKBOT_HUB_BASE_URL
#define SPARKBOT_HUB_BASE_URL ""
#endif

#ifndef SPARKBOT_DEVICE_ID
#define SPARKBOT_DEVICE_ID "sparkbot_wearable_001"
#endif

namespace {

constexpr uint8_t kCodecAddress = 0x18;
constexpr int kCodecSdaPin = 4;
constexpr int kCodecSclPin = 5;
constexpr i2s_port_t kI2sPort = I2S_NUM_0;
constexpr int kI2sMclkPin = 45;
constexpr int kI2sBclkPin = 39;
constexpr int kI2sWsPin = 41;
constexpr int kI2sDataOutPin = 42;
constexpr int kI2sDataInPin = 40;
constexpr int kPowerAmpEnablePin = 46;
constexpr uint32_t kSampleRate = 16000;
constexpr uint32_t kMclkFrequency = kSampleRate * 256;
constexpr size_t kCaptureBufferSamples = 2048;
constexpr size_t kDefaultWavFrames = kSampleRate;
constexpr size_t kMaxConversationFrames = kSampleRate * 8;
constexpr size_t kMaxWavBytes = 44 + kMaxConversationFrames * sizeof(int16_t);
constexpr size_t kVadChunkFrames = kCaptureBufferSamples / 2;
// 尾部静音判定：原来 6/8 chunk（384/512ms）太急，三字唤醒词会被切成两截，
// 唤醒词一断就唤不醒。统一放到 10 chunk（640ms），等整句说完再收。
// 2026-11-11 二次定值：10→7（448ms）。流式探针上线后唤醒词不再依赖这段
// 判定（探针 1.2s 就走），7 chunk 只影响对话态收尾速度——448ms 的字间
// 停顿容忍度足够，说完到开始识别的等待少了 200ms。
constexpr size_t kVadSilenceChunks = 7;
// 唤醒词只有三个字（「贾维斯」约 0.9 秒），说完之后 256ms 静音就足够判定
// 说完了。原来跟表情态一样等 640ms，白白多等 384ms；实测三字之间最长停顿
// 也就 100ms 左右，256ms 不会被字间停顿骗到。
constexpr size_t kWakeVadSilenceChunks = 4;
// 至少要连续 8 块（512ms）有声才当成一句话上传。
// 原来是 2 块（128ms），屋子里的咳嗽、碰桌子、说话尾音都会被当成唤醒词上传，
// 每传一次要占用 4~6 秒（ASR + 轮询），这段时间麦克风是「聋」的，
// 用户真正喊唤醒词反而抢不到窗口。
constexpr size_t kVadMinimumSpeechChunks = 5;
// 上面那条「累计 5 块过门限」在人正常说话时经常凑不满：字与字之间的停顿
// 低于门限就不计数。真机实测喊「贾维斯」，逐块峰值是 2926 / 828 / 7575 /
// 3294 / 1918 / 801 —— 只有 4 块过线还被 828、801 打断，判定成「没说话」，
// 整整一段 7575 峰值的音频被丢掉，用户只能再喊一遍。
// 所以补一条按**时间跨度**判的规则：从开口算起跨过 500ms 就算说过话。
// 咳嗽/关门只有 1~2 块 + 尾静音，跨度 300~400ms 过不了线；三字词 + 尾静音
// 至少 700ms，两者能干净分开。
constexpr size_t kVadMinSpeechSpanFrames = kSampleRate / 2;  // 500ms
// 表情态是没有唤醒词的「一直都在」，门限太低会把屋里别人说话、喇叭尾音
// 全当成命令上传。
// 420 是在 ADC 采样率还没修正时定的：那时 ADC 实际只跑 5.33kHz，噪声被
// 三次采样保持拉平，块峰值只有 130 上下，420 显得很宽。pre_div 改成 1 之后
// 是真正的 16kHz，真实底噪块峰值抬到 130~786（真机实测），420 直接被底噪
// 顶穿——4 分钟内传了 4 条「嗯。」「咳。」，既白跑 ASR 又让屏幕闪思考脸。
// 2026-11-06 定值过程（踩过错，记下来免得再犯）：
//   一度抬到 2500，理由是「对着设备说话 12000 量级」。那个 12000 是从**上传
//   后的 WAV** 量出来的，而上传前固件已经把峰值归一化到 0.7（22937），
//   所以那个数字跟原声大小毫无关系。结果 2500 把自己的声音也挡在外面——
//   设备整段只听不传，用户说什么都没反应。
//   原始 VAD 块峰值（真机统计）：
//     通过 VAD 的 utterance  527 587 600 … 1033 1106 1257 1416 1961 2611 … 9817
//     被丢弃的噪声          165 182 … 587 687 742 902 1072 1186 1866 …
//   两组是**重叠**的：靠幅度分不开「人说话」和「屋里聊天」，只能定在
//   「高于底噪、低于正常说话」的地方。底噪 p90=262、偶发尖峰 700~900，
//   所以取 900：压得住底噪和单个尖峰，正常说话（1000+）都能进来。
//   屋里聊天也会进来——这个代价必须接受，靠中枢的静音/门控过滤，总比聋掉好。
constexpr uint32_t kVadThreshold = 700;
// 2026-11-10 三次定值：待机唤醒 700→500（独立于对话态的 700）。
// 现场实测用户在正常距离说话，设备端 VAD 峰值只有 200~750，700 仍然
// 漏一大半（波形分析：上传音频 rms 只有 117，ASR 只能猜出「嗯」）。
// 待机被环境音误触发的代价只是白传一次 ASR（屏幕黑着，中枢门控会过滤），
// 对话态保持 700 不动——那边误触发的会直接变成指令上传。
constexpr uint32_t kWakeVadThreshold = 500;
// 中枢写完状态后设备最多晚 60ms 才取到。唤醒是「说完就想听到回应」的场景，
// 150ms 的轮询间隔等于平均白等 75ms、最坏 150ms，不值得省这点请求。
constexpr uint32_t kWakePollIntervalMs = 60;
// 中枢一轮要 3~9 秒（模型侧）。轮询间隔 500ms 意味着平均白白多等 250ms，
// 最坏多等 500ms，纯属浪费；中枢自己是毫秒级写库，150ms 完全跟得上。
constexpr uint32_t kConversationPollIntervalMs = 150;
// 语音回复不额外衰减：ES8311 的 DAC 音量本来就只有 0xB2（约 -6dB），
// 再除以 2 在稍微吵一点的房间里就基本听不见了。音乐走的是另一条路，不受影响。
constexpr int16_t kPlaybackVolumeDivisor = 1;
// 流式播放预缓冲：48KB → 96KB = 3s 音频（16k/16bit/单声道 = 32KB/s）。
// 2026-11-06 用户实测「说话一卡一卡」：回程下载 ~125KB/s 平均看够用，但
// WiFi 抖动时 RTT 会冲到 55ms、瞬时吞吐掉到播放速率以下，48KB 只够扛 1.5s，
// 一句 6.8 秒的回复必然断流写静音。加厚到 3s，代价是首字晚 0.3~0.5 秒。
constexpr size_t kStreamPrebufferBytes = 96u * 1024u;
// PSRAM 要不到 96KB 时逐级减半，下限 24KB：
// 宁可缓冲薄一点，也不要因为分配失败让整句回复播不出来。
constexpr size_t kStreamPrebufferMinBytes = 24u * 1024u;
// 上一轮回复播完到下一次开始录音之间的静默窗口。喇叭尾音还在屋里回荡，
// 立刻开录会把「自己的声音」当成下一句，形成自问自答的死循环。
constexpr uint32_t kPostReplyGuardMs = 450;

bool i2c_started = false;
bool i2s_started = false;
// ES8311 的 ADC 配置是否已经生效。待机循环不要再每轮重配一遍：
// 每次重配 + 重启 I2S 时钟，功放（和背光共用 GPIO46，一直是使能状态）
// 都会把这声「咔」放大出来——这就是用户听到的「每隔几秒冒一声」。
bool codec_adc_ready = false;

int16_t capture_buffer[kCaptureBufferSamples];
int16_t* wav_mono_buffer = nullptr;
uint8_t* wav_file = nullptr;
size_t wav_frames = 0;
size_t wav_bytes = 0;
// 本轮语音是从第几帧开始被 VAD 确认的。对话路径靠它砍掉句首那段空等：
// VAD 从窗口起点就开始录，用户可能 4 秒后才开口，前面全是底噪。
size_t speech_onset_frames = 0;
String last_session_action;
// 屏幕/功放当前是否开着。上报给中枢，用来回答「屏幕亮不亮」这类问题。
bool g_display_on = true;

void start_i2c() {
  if (i2c_started) {
    return;
  }

  Wire.begin(kCodecSdaPin, kCodecSclPin, 100000);
  Wire.setTimeOut(50);
  i2c_started = true;
}

bool write_codec_register(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kCodecAddress);
  Wire.write(reg);
  Wire.write(value);
  const uint8_t error = Wire.endTransmission(true);
  if (error != 0) {
    Serial.printf("[ES8311] write failed: reg=0x%02X value=0x%02X error=%u\n",
                  reg,
                  value,
                  error);
    return false;
  }
  delayMicroseconds(300);
  return true;
}

bool read_codec_register(uint8_t reg, uint8_t* value) {
  Wire.beginTransmission(kCodecAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  if (Wire.requestFrom(kCodecAddress, static_cast<size_t>(1), true) != 1) {
    return false;
  }
  *value = Wire.read();
  return true;
}

bool read_and_print(uint8_t reg, const char* name) {
  uint8_t value = 0;
  if (!read_codec_register(reg, &value)) {
    Serial.printf("[ES8311] read failed: %s reg=0x%02X\n", name, reg);
    return false;
  }
  Serial.printf("[ES8311] %s (0x%02X) = 0x%02X\n", name, reg, value);
  return true;
}

bool configure_codec_for_adc() {
  start_i2c();
  Serial.println("[ES8311] configuring ADC, 16kHz/16bit, I2S slave");

  // This order follows Espressif's ES8311 open/start sequence. The DAC and PA
  // stay disabled; only the analog ADC path is enabled for this test.
  uint8_t reset = 0;
  if (!read_codec_register(0x00, &reset)) {
    Serial.println("[ES8311] device did not respond before configuration");
    return false;
  }

  const uint8_t open_sequence[][2] = {
      {0x0D, 0xFA},
      {0x44, 0x08},
      {0x44, 0x08},
      {0x01, 0x30},
      {0x02, 0x00},
      {0x03, 0x10},
      // 麦克风采音增益 0x24→0x26（2026-11-10）：寄存器 0x16 高位（0x20 段）
      // 是别的功能位，绝不能动——0x28 实测整条麦克风链路输出全零。
      // 只抬低位增益场 4→6（约 +3dB）：正常说话音量 200~750 的峰值翻倍到
      // 400~1500，稳过 700 的门限。若编出来 peak=0 说明低位场边界也不对，
      // 立即回退 0x24。
      {0x16, 0x27},
      {0x04, 0x10},
      {0x05, 0x00},
      {0x0B, 0x00},
      {0x0C, 0x00},
      {0x10, 0x1F},
      {0x11, 0x7F},
      {0x00, 0x80},
  };
  for (const auto& entry : open_sequence) {
    if (!write_codec_register(entry[0], entry[1])) {
      return false;
    }
  }

  if (!read_codec_register(0x00, &reset)) {
    return false;
  }
  reset &= static_cast<uint8_t>(~0x40);  // ES8311 is the I2S slave.
  if (!write_codec_register(0x00, reset)) {
    return false;
  }

  // ESP32 supplies MCLK, BCLK and LRCK. The ES8311 uses MCLK as its clock source.
  if (!write_codec_register(0x01, 0x3F)) {
    return false;
  }

  uint8_t register_value = 0;
  if (!read_codec_register(0x06, &register_value)) {
    return false;
  }
  register_value &= static_cast<uint8_t>(~0x20);  // normal BCLK polarity
  if (!write_codec_register(0x06, register_value)) {
    return false;
  }

  const uint8_t format_sequence[][2] = {
      {0x13, 0x10},
      {0x1B, 0x0A},
      {0x1C, 0x6A},
      {0x44, 0x08},
      // 4.096 MHz MCLK / 16 kHz 的系数（官方 esp_codec_dev 的 coeff_div 表：
      // {4096000, 16000, pre_div=0x01, pre_multi=0x01, adc_div=0x01, dac_div=0x01,
      //  fs_mode=0x00, lrck_h=0x00, lrck_l=0xff, bclk_div=0x04, adc_osr=0x10, dac_osr=0x20}）。
      // 这里原本写的是 {0x02, 0x40}，也就是 pre_div=3 —— 那是 12.288 MHz 那一行。
      // pre_div 多除了 3，ADC 实际只出 5.33 kHz，I2S 按 16 kHz 读走时每个采样被
      // 重复 3 次（实测相邻采样重复率 67%），送进 ASR 是失真音频，识别结果全是幻听。
      // 播放路径本来就是 0x00，所以只有录音坏、出声正常。
      {0x02, 0x00},
      {0x05, 0x00},
      {0x03, 0x10},
      {0x04, 0x20},
      {0x07, 0x00},
      {0x08, 0xFF},
      {0x06, 0x03},
  };
  for (const auto& entry : format_sequence) {
    if (!write_codec_register(entry[0], entry[1])) {
      return false;
    }
  }

  uint8_t dac_format = 0;
  uint8_t adc_format = 0;
  if (!read_codec_register(0x09, &dac_format) ||
      !read_codec_register(0x0A, &adc_format)) {
    return false;
  }
  dac_format = static_cast<uint8_t>((dac_format & 0xFC) | 0x0C);
  adc_format = static_cast<uint8_t>((adc_format & 0xFC) | 0x0C);
  if (!write_codec_register(0x09, dac_format) ||
      !write_codec_register(0x0A, adc_format)) {
    return false;
  }

  // Enable ADC only. Keep DAC muted and leave the external NS4150B disabled.
  adc_format &= static_cast<uint8_t>(~0x40);
  if (!write_codec_register(0x0A, adc_format)) {
    return false;
  }
  const uint8_t start_sequence[][2] = {
      {0x17, 0xBF},
      {0x0E, 0x02},
      {0x14, 0x1A},
      {0x0D, 0x01},
      {0x15, 0x40},
      {0x37, 0x08},
      {0x45, 0x00},
  };
  for (const auto& entry : start_sequence) {
    if (!write_codec_register(entry[0], entry[1])) {
      return false;
    }
  }

  read_and_print(0x00, "RESET");
  read_and_print(0x02, "CLK_PRE_DIV");
  read_and_print(0x09, "SDPIN");
  read_and_print(0x0A, "SDPOUT");
  read_and_print(0x0D, "SYSTEM0D");
  read_and_print(0x14, "SYSTEM14");
  read_and_print(0x16, "ADC_GAIN");
  return true;
}

bool configure_codec_for_playback() {
  start_i2c();
  Serial.println("[ES8311] configuring DAC, 16kHz/16bit, I2S slave");

  uint8_t system_power = 0;
  if (!read_codec_register(0x0D, &system_power)) {
    Serial.println("[ES8311] device did not respond before playback configuration");
    return false;
  }

  // This is the Espressif esp_codec_dev ES8311 open sequence.
  const uint8_t open_sequence[][2] = {
      {0x0D, 0xFA},
      {0x44, 0x08},
      {0x44, 0x08},
      {0x01, 0x30},
      {0x02, 0x00},
      {0x03, 0x10},
      // 麦克风采音增益 0x24→0x26（2026-11-10）：寄存器 0x16 高位（0x20 段）
      // 是别的功能位，绝不能动——0x28 实测整条麦克风链路输出全零。
      // 只抬低位增益场 4→6（约 +3dB）：正常说话音量 200~750 的峰值翻倍到
      // 400~1500，稳过 700 的门限。若编出来 peak=0 说明低位场边界也不对，
      // 立即回退 0x24。
      {0x16, 0x27},
      {0x04, 0x10},
      {0x05, 0x00},
      {0x0B, 0x00},
      {0x0C, 0x00},
      {0x10, 0x1F},
      {0x11, 0x7F},
      {0x00, 0x80},
  };
  for (const auto& entry : open_sequence) {
    if (!write_codec_register(entry[0], entry[1])) {
      return false;
    }
  }

  uint8_t reset = 0;
  if (!read_codec_register(0x00, &reset)) {
    return false;
  }
  reset &= static_cast<uint8_t>(~0x40);  // ES8311 is the I2S slave.
  if (!write_codec_register(0x00, reset)) {
    return false;
  }

  // 4.096 MHz MCLK / 16 kHz, 16-bit Philips I2S, mono DAC output.
  const uint8_t playback_sequence[][2] = {
      {0x01, 0x3F},
      {0x13, 0x10},
      {0x1B, 0x0A},
      {0x1C, 0x6A},
      {0x44, 0x58},
      {0x09, 0x0C},
      {0x0A, 0x0C},
      {0x02, 0x00},
      {0x05, 0x00},
      {0x03, 0x10},
      {0x04, 0x20},
      {0x07, 0x00},
      {0x08, 0xFF},
      {0x06, 0x03},
      {0x17, 0xBF},
      {0x0E, 0x02},
      {0x12, 0x00},
      {0x14, 0x1A},
      {0x0D, 0x01},
      {0x15, 0x40},
      {0x37, 0x08},
      {0x45, 0x00},
      {0x31, 0x00},
      {0x32, 0xB2},
  };
  for (const auto& entry : playback_sequence) {
    if (!write_codec_register(entry[0], entry[1])) {
      return false;
    }
  }

  read_and_print(0x00, "RESET");
  read_and_print(0x09, "SDPIN");
  read_and_print(0x0A, "SDPOUT");
  read_and_print(0x02, "CLK_PRE_DIV");
  read_and_print(0x04, "DAC_OSR");
  read_and_print(0x0D, "SYSTEM0D");
  read_and_print(0x14, "SYSTEM14");
  read_and_print(0x12, "DAC_POWER");
  read_and_print(0x31, "DAC_MUTE");
  read_and_print(0x32, "DAC_VOLUME");
  return true;
}

bool start_i2s(i2s_mode_t direction) {
  if (i2s_started) {
    return true;
  }

  const i2s_config_t config = {
      .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | direction),
      .sample_rate = kSampleRate,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = direction == I2S_MODE_TX
                            ? I2S_CHANNEL_FMT_ONLY_LEFT
                            : I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      // 4×256 只有 32ms 的缓冲，播放时每写一块还要画一次屏（240×190×2 走 40MHz SPI
      // 约 18ms），DMA 必然被抽干，声音就变成卡顿、重复的「鬼畜」音。
      // 扩到 16×256 ≈ 128ms 给 CPU 留出余量。
      // 2026-11-11 再扩到 48×256 ≈ 768ms：流式唤醒探针上传期间（约 0.3~0.5s）
      // 用户还在说命令，16 档缓冲装不下会溢出丢字——加大后话尾由会话态
      // 第一轮从 DMA 无缝读走。
      .dma_buf_count = 48,
      .dma_buf_len = 256,
      .use_apll = true,
      // 欠载（DMA 被抽干）时一定要输出静音，不能让它循环重播最后一段缓冲。
      // 设成 false 时，一旦数据没跟上，喇叭会把最后 128ms 再念一遍，
      // 听感就是「很卡」变「很很卡很卡」这种鬼畜重复。
      .tx_desc_auto_clear = true,
      .fixed_mclk = static_cast<int>(kMclkFrequency),
      .mclk_multiple = I2S_MCLK_MULTIPLE_256,
      .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
  };
  const esp_err_t install_error = i2s_driver_install(kI2sPort, &config, 0, nullptr);
  if (install_error != ESP_OK) {
    Serial.printf("[I2S] driver install failed: %s\n", esp_err_to_name(install_error));
    return false;
  }

  const i2s_pin_config_t pins = {
      .mck_io_num = kI2sMclkPin,
      .bck_io_num = kI2sBclkPin,
      .ws_io_num = kI2sWsPin,
      .data_out_num = kI2sDataOutPin,
      .data_in_num = direction == I2S_MODE_RX ? kI2sDataInPin : I2S_PIN_NO_CHANGE,
  };
  const esp_err_t pin_error = i2s_set_pin(kI2sPort, &pins);
  if (pin_error != ESP_OK) {
    Serial.printf("[I2S] pin setup failed: %s\n", esp_err_to_name(pin_error));
    i2s_driver_uninstall(kI2sPort);
    return false;
  }

  i2s_zero_dma_buffer(kI2sPort);
  i2s_started = true;
  Serial.printf("[I2S] started: MCLK=%d BCLK=%d LRCK=%d DOUT=%d, rate=%luHz mode=%s\n",
                kI2sMclkPin,
                kI2sBclkPin,
                kI2sWsPin,
                kI2sDataOutPin,
                static_cast<unsigned long>(kSampleRate),
                direction == I2S_MODE_RX ? "RX" : "TX");
  return true;
}

void stop_i2s() {
  if (!i2s_started) {
    return;
  }
  i2s_stop(kI2sPort);
  i2s_driver_uninstall(kI2sPort);
  i2s_started = false;
}

void capture_audio() {
  if (!start_i2s(I2S_MODE_RX)) {
    return;
  }
  if (!configure_codec_for_adc()) {
    Serial.println("[AUDIO] ES8311 configuration failed; stopping I2S");
    stop_i2s();
    return;
  }

  delay(100);
  size_t bytes_read = 0;
  const esp_err_t read_error = i2s_read(kI2sPort,
                                        capture_buffer,
                                        sizeof(capture_buffer),
                                        &bytes_read,
                                        pdMS_TO_TICKS(1000));
  if (read_error != ESP_OK) {
    Serial.printf("[AUDIO] capture failed: %s\n", esp_err_to_name(read_error));
    stop_i2s();
    return;
  }

  const size_t sample_count = bytes_read / sizeof(capture_buffer[0]);
  const size_t frame_count = sample_count / 2;
  int16_t minimum[2] = {32767, 32767};
  int16_t maximum[2] = {-32768, -32768};
  int32_t peak[2] = {0, 0};
  int64_t sum[2] = {0, 0};
  uint64_t square_sum[2] = {0, 0};
  size_t nonzero_count[2] = {0, 0};
  for (size_t index = 0; index < frame_count; ++index) {
    for (size_t channel = 0; channel < 2; ++channel) {
      const int32_t sample = capture_buffer[index * 2 + channel];
      minimum[channel] = min(minimum[channel], capture_buffer[index * 2 + channel]);
      maximum[channel] = max(maximum[channel], capture_buffer[index * 2 + channel]);
      peak[channel] = max(peak[channel], abs(sample));
      sum[channel] += sample;
      if (sample != 0) {
        ++nonzero_count[channel];
      }
    }
  }

  Serial.printf("[AUDIO] bytes=%u samples=%u frames=%u\n",
                static_cast<unsigned int>(bytes_read),
                static_cast<unsigned int>(sample_count),
                static_cast<unsigned int>(frame_count));
  for (size_t channel = 0; channel < 2; ++channel) {
    const double mean = frame_count == 0
                            ? 0.0
                            : static_cast<double>(sum[channel]) / frame_count;
    for (size_t index = 0; index < frame_count; ++index) {
      const double centered = static_cast<double>(capture_buffer[index * 2 + channel]) - mean;
      square_sum[channel] += static_cast<uint64_t>(centered * centered);
    }
    const double rms = frame_count == 0
                           ? 0.0
                           : sqrt(static_cast<double>(square_sum[channel]) / frame_count);
    Serial.printf("[AUDIO] channel=%s min=%d max=%d mean=%.2f peak=%ld rms_centered=%.2f nonzero=%u\n",
                  channel == 0 ? "left" : "right",
                  minimum[channel],
                  maximum[channel],
                  mean,
                  static_cast<long>(peak[channel]),
                  rms,
                  static_cast<unsigned int>(nonzero_count[channel]));
  }
  Serial.println("[AUDIO] Short waveform statistics only; no audio file is saved.");
  stop_i2s();
}

void write_little_endian16(uint8_t* output, uint16_t value) {
  output[0] = static_cast<uint8_t>(value & 0xFF);
  output[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
}

void write_little_endian32(uint8_t* output, uint32_t value) {
  output[0] = static_cast<uint8_t>(value & 0xFF);
  output[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
  output[2] = static_cast<uint8_t>((value >> 16) & 0xFF);
  output[3] = static_cast<uint8_t>((value >> 24) & 0xFF);
}

bool capture_mono_samples(size_t requested_frames = kDefaultWavFrames) {
  if (wav_mono_buffer == nullptr || wav_file == nullptr ||
      requested_frames == 0 || requested_frames > kMaxConversationFrames) {
    Serial.println("[WAV] audio buffers are not ready");
    return false;
  }
  if (!start_i2s(I2S_MODE_RX)) {
    return false;
  }
  if (!configure_codec_for_adc()) {
    Serial.println("[WAV] ES8311 configuration failed; stopping I2S");
    stop_i2s();
    return false;
  }

  delay(100);
  size_t captured_frames = 0;
  while (captured_frames < requested_frames) {
#ifdef PET_FOUNTAIN
    face::tick();  // 采集期间让表情继续呼吸/眨眼
#endif
    size_t bytes_read = 0;
    const esp_err_t read_error = i2s_read(kI2sPort,
                                          capture_buffer,
                                          sizeof(capture_buffer),
                                          &bytes_read,
                                          pdMS_TO_TICKS(1000));
    if (read_error != ESP_OK || bytes_read < sizeof(int16_t) * 2) {
      Serial.printf("[WAV] capture failed: error=%s bytes=%u\n",
                    esp_err_to_name(read_error),
                    static_cast<unsigned int>(bytes_read));
      stop_i2s();
      return false;
    }

    const size_t frames_read = bytes_read / (sizeof(int16_t) * 2);
    const size_t frames_to_copy = min(frames_read, requested_frames - captured_frames);
#ifdef PET_FOUNTAIN
    if (fountain::active()) {
      fountain::feed_stereo(capture_buffer, frames_to_copy);
    }
#endif
    for (size_t frame = 0; frame < frames_to_copy; ++frame) {
      wav_mono_buffer[captured_frames + frame] = capture_buffer[frame * 2];
    }
    captured_frames += frames_to_copy;
  }
  stop_i2s();
  wav_frames = captured_frames;
  return true;
}

bool capture_until_silence(
    uint32_t max_seconds,
    size_t required_silence_chunks = kVadSilenceChunks,
    uint32_t vad_threshold = kVadThreshold,
    uint32_t post_speech_seconds = 0,
    size_t min_speech_run = 2,
    size_t probe_frames = 0,
    bool clear_dma = true) {
  if (wav_mono_buffer == nullptr || wav_file == nullptr) {
    Serial.println("[VAD] audio buffers are not ready");
    return false;
  }
  if (!start_i2s(I2S_MODE_RX)) {
    return false;
  }
  if (!codec_adc_ready) {
    if (!configure_codec_for_adc()) {
      Serial.println("[VAD] ES8311 configuration failed; stopping I2S");
      stop_i2s();
      return false;
    }
    codec_adc_ready = true;
    delay(100);  // 只在首次配置后等 codec 稳定
  }
  // I2S 常开，上一轮/等待中枢时 DMA 里压着旧数据，不清掉会把上次的尾巴当成本次。
  // 例外：流式唤醒探针命中后转入会话的第一轮，DMA 里压着用户说了一半的
  // 命令尾巴（喊「贾维斯，打开计算器」时探针只含前 1.2s），清掉就丢命令。
  if (clear_dma) {
    i2s_zero_dma_buffer(kI2sPort);
  }
  const size_t window_frames = min(
      static_cast<size_t>(kMaxConversationFrames),
      static_cast<size_t>(max_seconds) * static_cast<size_t>(kSampleRate));
  // 待机窗口是从「本轮起始」算的，如果用户在窗口末尾才开口，剩下的时间
  // 不够说完一整句，唤醒词会被硬生生切断（实测约 1/3 的失败是这样来的）。
  // 所以语音一旦被确认，窗口就改成「从语音起点再给 post_speech_seconds 秒」，
  // 既不用为了兜住末尾而把整个窗口拉长（空转更久），也不会切掉半句。
  const size_t post_speech_frames =
      static_cast<size_t>(post_speech_seconds) * static_cast<size_t>(kSampleRate);
  size_t max_frames = window_frames;
  size_t captured_frames = 0;
  speech_onset_frames = 0;
  size_t silence_chunks = 0;
  size_t speech_chunks = 0;
  size_t startup_chunks = 0;
  size_t candidate_run = 0;
  bool speech_started = false;
  uint32_t round_peak = 0;
  while (captured_frames + kVadChunkFrames <= max_frames) {
#ifdef PET_FOUNTAIN
    face::tick();  // 待机时表情要保持活着
#endif
    size_t bytes_read = 0;
    const esp_err_t read_error = i2s_read(
        kI2sPort,
        capture_buffer,
        sizeof(capture_buffer),
        &bytes_read,
        pdMS_TO_TICKS(1000));
    if (read_error != ESP_OK || bytes_read < sizeof(int16_t) * 2) {
      Serial.printf("[VAD] capture failed: error=%s bytes=%u\n",
                    esp_err_to_name(read_error),
                    static_cast<unsigned int>(bytes_read));
      stop_i2s();
      return false;
    }

    const size_t frames_read = min(
        bytes_read / (sizeof(int16_t) * 2),
        kVadChunkFrames);
#ifdef PET_FOUNTAIN
    // 语音链路与喷泉共用同一路麦克风数据：边听边跳，不需要第二个 I2S 读者
    if (fountain::active()) {
      fountain::feed_stereo(capture_buffer, frames_read);
    }
#endif
    int32_t minimum = 32767;
    int32_t maximum = -32768;
    int64_t sum = 0;
    for (size_t frame = 0; frame < frames_read; ++frame) {
      const int16_t sample = capture_buffer[frame * 2];
      minimum = min(minimum, static_cast<int32_t>(sample));
      maximum = max(maximum, static_cast<int32_t>(sample));
      sum += sample;
    }
    const int32_t mean = frames_read == 0
                             ? 0
                             : static_cast<int32_t>(sum / frames_read);
    uint32_t peak = 0;
    for (size_t frame = 0; frame < frames_read; ++frame) {
      const int32_t centered = capture_buffer[frame * 2] - mean;
      peak = max(peak, static_cast<uint32_t>(abs(centered)));
      wav_mono_buffer[captured_frames + frame] = capture_buffer[frame * 2];
    }
    captured_frames += frames_read;

    // ES8311 produces a short settling transient after each start. Do not
    // treat that transient as speech, otherwise standby uploads noise clips.
    const bool over_threshold = startup_chunks >= 5 && peak >= vad_threshold;
    ++startup_chunks;
    if (!speech_started) {
      // 环境里单个噪声尖峰（关门、衣服摩擦）通常只持续一块（64ms）。
      // 要求连续若干块都超门限才算「开始说话」，否则待机会被噪声反复唤醒。
      // 唤醒词路径用 2 块（128ms）保住响应速度；对话态用 3 块（192ms），
      // 咳嗽、清嗓子这类 100~200ms 的短促声音几乎都是 2 块，多要一块就能挡掉。
      candidate_run = over_threshold ? candidate_run + 1 : 0;
      if (candidate_run >= min_speech_run) {
        speech_started = true;
        speech_chunks = candidate_run;
        silence_chunks = 0;
        // 连续两块都过门限才算开口，所以真正的起点要往回推这两块
        const size_t onset = captured_frames - candidate_run * kVadChunkFrames;
        speech_onset_frames = captured_frames > onset ? onset : 0;
        if (post_speech_frames > 0) {
          max_frames = min(
              static_cast<size_t>(kMaxConversationFrames),
              captured_frames + post_speech_frames);
        }
      }
    } else if (over_threshold) {
      ++speech_chunks;
      silence_chunks = 0;
    } else {
      ++silence_chunks;
    }
    // 流式唤醒探针（2026-11-11 用户提议）：攒够 1.2s（唤醒词长度）就立刻
    // 上传，不等尾音静音——喊「贾维斯，打开计算器」时，亮屏和识别发生在
    // 命令说到一半，说完的尾巴留在 DMA 里由会话态第一轮无缝接走。
    if (probe_frames > 0 && speech_started &&
        captured_frames - speech_onset_frames >= probe_frames) {
      break;
    }
    const bool active = over_threshold;
    if (peak > round_peak) round_peak = peak;
    Serial.printf("[VAD] peak=%lu active=%s captured_ms=%lu\n",
                  static_cast<unsigned long>(peak),
                  active ? "yes" : "no",
                  static_cast<unsigned long>(captured_frames * 1000 / kSampleRate));

    // 「够了」的判据有两个，满足其一即可：
    //   1. 累计 5 块过门限（老规则，适用于连续大音量）；
    //   2. 从开口到现在的跨度 >= 0.5s（新规则，见 kVadMinSpeechSpanFrames）。
    // 只用第 1 条会把三字词整段丢掉：实测「贾维斯」被拆成「1 块 + 3 块」，
    // 中间字间停顿低于门限不计数，凑不满 5 块 → 报 no speech → 白喊一遍。
    const bool enough_speech =
        speech_chunks >= kVadMinimumSpeechChunks ||
        (captured_frames > speech_onset_frames &&
         captured_frames - speech_onset_frames >= kVadMinSpeechSpanFrames);
    if (speech_started && enough_speech && silence_chunks >= required_silence_chunks) {
      break;
    }
  }
  // 这里**故意不停 I2S**：待机循环会反复进来，每停一次再启一次，
  // 功放就会「咔」一声。保持 RX 常开，只让上层读到数据即可。
  wav_frames = captured_frames;
  const bool span_enough =
      captured_frames > speech_onset_frames &&
      captured_frames - speech_onset_frames >= kVadMinSpeechSpanFrames;
  if (!speech_started ||
      (speech_chunks < kVadMinimumSpeechChunks && !span_enough)) {
    // 带上整轮最大峰值，方便按真人音量校准门限（阈值调高调低都看这个数）
    Serial.printf("[VAD] no speech detected (round peak=%lu, need=%lu)\n",
                  static_cast<unsigned long>(round_peak),
                  static_cast<unsigned long>(vad_threshold));
    return false;
  }
  Serial.printf("[VAD] utterance captured: frames=%u seconds=%.2f peak=%lu\n",
                static_cast<unsigned int>(wav_frames),
                static_cast<double>(wav_frames) / kSampleRate,
                static_cast<unsigned long>(round_peak));
  return true;
}

#ifdef PET_FOUNTAIN
// ---------------- 桌宠两种状态：表情态 ⇄ 待机态 ----------------
// GPIO46 同时驱动屏幕背光和功放使能，翻转它必然在喇叭里留一声「咔」。
// 翻转前先把 ES8311 的 DAC 静音（reg 0x31 = 0x60），翻转后再解除，
// 熄屏 / 亮屏就不会「啪」一下。
void set_display_and_amp(bool on) {
  g_display_on = on;
  if (i2c_started) {
    write_codec_register(0x31, 0x60);  // DAC mute
    delay(15);
  }
  digitalWrite(kPowerAmpEnablePin, on ? HIGH : LOW);
  if (on && i2c_started) {
    delay(15);
    write_codec_register(0x31, 0x00);  // DAC unmute
  }
}

// 表情态：亮屏 + 挂着表情。上电默认就是这个状态。
void enter_face_state() {
  if (!face::ready()) face::begin();
  set_display_and_amp(true);
  face::resume();
  face::set_expression(face::kIdle);
  face::tick();
  Serial.println("[PET] face state: screen on, listening");
}

// 待机态：熄屏。只有在这个状态下才需要唤醒词。
// show_sleepy=false 用于上电直进待机：不画「闭眼睡着」、不留 350ms，
// 从上电起全程黑屏（对话结束回落待机时仍用默认 true，保留闭眼过渡）。
void play_sleep_phrase();  // 熄屏前的随机结束语（定义在播放区，这里前置声明）

void enter_standby_state(bool show_sleepy = true) {
  if (show_sleepy) {
    if (fountain::active()) {
      // 喷泉还在跳：先说结束语再关喷泉 —— 关掉后功放（=背光 GPIO46）没电，说了也是哑的
      play_sleep_phrase();
    } else {
      face::set_expression(face::kSleepy);
      face::tick();
      delay(350);  // 让「闭眼睡着」露一下再熄屏，否则看起来像被粗暴关掉
      play_sleep_phrase();  // 随机结束语，说完才熄屏
    }
  }
  if (fountain::active()) {
    fountain::set_active(false);
  }
  face::suspend();
  set_display_and_amp(false);
  Serial.println("[PET] standby: screen off, say 贾维斯 to wake");
}
#endif  // PET_FOUNTAIN

// 唤醒播报拆成两段（2026-11-10 音画同步）：
//   prepare_wakeup_playback()  —— 黑屏期间就把 TX 通路备好（重装 I2S、配 codec），
//                                 这些动作要几百毫秒，以前表情挂在亮着的屏幕上干等；
//   play_wakeup_audio_prepared() —— 开功放 + 上说话嘴型 + 立即出声。
// 顺序必须是「先备通路 → 亮屏 → 出声」：功放和背光共用 GPIO46，
// 黑屏里出声就是哑的；备好之后亮屏与出声只隔一个 delay(20)，肉眼即同步。
bool prepare_wakeup_playback() {
  // 待机时 I2S 是常开的 RX 模式，切 TX 必须重装驱动（start_i2s 对已启动直接返回）
  stop_i2s();
  codec_adc_ready = false;
  if (!start_i2s(I2S_MODE_TX)) {
    return false;
  }
  if (!configure_codec_for_playback()) {
    Serial.println("[AUDIO] ES8311 playback configuration failed; stopping I2S");
    stop_i2s();
    return false;
  }
  return true;
}

// 按 chunk 把一段裸 PCM 推给 I2S；drive_mouth=true 时嘴型随音量张合
void play_pcm_clip(const unsigned char* pcm_data, unsigned int pcm_len, bool drive_mouth) {
  constexpr size_t kMonoChunkSamples = 1024;
  int16_t mono_buffer[kMonoChunkSamples];
  const int16_t* pcm_samples = reinterpret_cast<const int16_t*>(pcm_data);
  size_t offset = 0;
  uint32_t chunk_index = 0;
  bool write_failed = false;
  while (offset < pcm_len / sizeof(int16_t)) {
    const size_t remaining =
        pcm_len / sizeof(int16_t) - offset;
    const size_t samples = min(remaining, kMonoChunkSamples);
    int32_t chunk_peak = 0;
    for (size_t index = 0; index < samples; ++index) {
      mono_buffer[index] = pcm_samples[offset + index] / kPlaybackVolumeDivisor;
      const int32_t magnitude = abs(static_cast<int32_t>(mono_buffer[index]));
      if (magnitude > chunk_peak) chunk_peak = magnitude;
    }

    size_t bytes_written = 0;
    const esp_err_t write_error = i2s_write(kI2sPort,
                                            mono_buffer,
                                            samples * sizeof(int16_t),
                                            &bytes_written,
                                            pdMS_TO_TICKS(1000));
    if (write_error != ESP_OK || bytes_written != samples * sizeof(int16_t)) {
      Serial.printf("[AUDIO] playback failed: error=%s bytes=%u\n",
                    esp_err_to_name(write_error),
                    static_cast<unsigned int>(bytes_written));
      write_failed = true;
      break;
    }
#ifdef PET_FOUNTAIN
    if (drive_mouth) {
      face::feed_amp(chunk_peak / 32768.0f * 3.2f);
      if ((chunk_index & 1u) == 0u) {
        face::tick();
      }
    }
#endif
    ++chunk_index;
    offset += samples;
  }

  i2s_zero_dma_buffer(kI2sPort);
  delay(30);
  // 同上：GPIO46 与背光共用，保持高电平，避免功放使能翻转的「咔」声
  stop_i2s();
#ifdef PET_FOUNTAIN
  face::reset_amp();
#endif
  if (!write_failed) {
    Serial.println("[AUDIO] playback completed");
  }
}

// 唤醒应答：随机抽一条招呼语（2026-11-10 用户要求多样，不再永远是「我在」）
void play_wakeup_audio_prepared() {
  digitalWrite(kPowerAmpEnablePin, HIGH);
  Serial.println("[AUDIO] PA GPIO46 enabled");
  delay(20);
#ifdef PET_FOUNTAIN
  face::set_expression(face::kSpeaking);
  face::tick();  // 说话嘴型先挂上，feed_amp 会随音量张合
#endif
  const PetPhraseClip& clip = PET_WAKE_PHRASES[esp_random() % PET_WAKE_PHRASE_COUNT];
  play_pcm_clip(clip.data, clip.len, true);
}

// 熄屏前的结束语：闭眼挂着说一句，说完才真正熄屏（也是随机多样）
void play_sleep_phrase() {
  if (!prepare_wakeup_playback()) {
    return;  // 备通路失败就静默入睡，别为一句告别卡住休眠
  }
  digitalWrite(kPowerAmpEnablePin, HIGH);
  delay(20);
  const PetPhraseClip& clip = PET_SLEEP_PHRASES[esp_random() % PET_SLEEP_PHRASE_COUNT];
  play_pcm_clip(clip.data, clip.len, false);
}

void play_wakeup_audio() {
  // 串口 p 命令等非唤醒场景用：备通路 + 出声一次做完（无屏幕同步诉求）
  if (!prepare_wakeup_playback()) {
    return;
  }
  play_wakeup_audio_prepared();
}

void play_test_tone() {
  // 待机时 I2S 是常开的 RX 模式，切 TX 必须重装驱动（start_i2s 对已启动直接返回）
  stop_i2s();
  codec_adc_ready = false;
  if (!start_i2s(I2S_MODE_TX)) {
    return;
  }
  if (!configure_codec_for_playback()) {
    Serial.println("[AUDIO] ES8311 playback configuration failed; stopping I2S");
    stop_i2s();
    return;
  }

  digitalWrite(kPowerAmpEnablePin, HIGH);
  Serial.println("[AUDIO] PA GPIO46 enabled; playing 1kHz tone for 3 seconds");
  delay(20);

  constexpr size_t kChunkSamples = 256;
  constexpr size_t kToneSamples = kSampleRate * 3;
  constexpr float kToneFrequency = 1000.0F;
  constexpr float kAmplitude = 12000.0F;
  int16_t tone_buffer[kChunkSamples];
  size_t offset = 0;
  bool write_failed = false;
  while (offset < kToneSamples) {
    const size_t samples = min(kChunkSamples, kToneSamples - offset);
    for (size_t index = 0; index < samples; ++index) {
      const float phase =
          2.0F * PI * kToneFrequency * static_cast<float>(offset + index) /
          static_cast<float>(kSampleRate);
      tone_buffer[index] = static_cast<int16_t>(sinf(phase) * kAmplitude);
    }

    size_t bytes_written = 0;
    const esp_err_t write_error = i2s_write(kI2sPort,
                                            tone_buffer,
                                            samples * sizeof(int16_t),
                                            &bytes_written,
                                            pdMS_TO_TICKS(1000));
    if (write_error != ESP_OK || bytes_written != samples * sizeof(int16_t)) {
      Serial.printf("[AUDIO] tone failed: error=%s bytes=%u\n",
                    esp_err_to_name(write_error),
                    static_cast<unsigned int>(bytes_written));
      write_failed = true;
      break;
    }
    offset += samples;
  }

  i2s_zero_dma_buffer(kI2sPort);
  delay(30);
  // GPIO46 与背光共用，保持高电平，避免功放使能翻转的「咔」声
  stop_i2s();
  if (!write_failed) {
    Serial.println("[AUDIO] 1kHz tone completed");
  }
}

// ── 唤醒音频裁剪：只把「最响的那一簇」传上去 ──────────────────────────
// 待机窗口 3 秒，原来的做法是把这 3 秒整段上传（94252 字节）。但真正有内容的
// 只有 0.9 秒，其余都是房间底噪。实测局域网 85~130 KB/s，94252 字节要
// 0.6~1.2 秒 —— 这是唤醒链路上最大的一段时间开销；而且带底噪送进 ASR 更容易
// 听成「查维斯」以外的怪词。这里按中枢 wake_audio.py 完全相同的参数裁一遍：
// 25ms 一窗求峰值，从最响的窗向两侧扩张（允许 50ms 掉线），前后各留
// 0.2 / 0.3 秒气口，再把峰值归一化到 0.7 满量程。裁完通常 0.9~1.3 秒。
bool trim_wav_to_loudest_span() {
  constexpr size_t kWindowFrames = kSampleRate / 40;          // 25ms
  constexpr uint32_t kCropFloor = 200;
  constexpr size_t kGapWindows = 2;                           // 50ms
  constexpr size_t kPadHeadFrames = kSampleRate / 5;          // 0.20s
  constexpr size_t kPadTailFrames = kSampleRate * 3 / 10;     // 0.30s
  constexpr size_t kMinSegmentFrames = kSampleRate / 5;       // 0.20s
  constexpr size_t kMaxWindows = kMaxConversationFrames / kWindowFrames;
  static uint32_t window_peaks[kMaxWindows];

  if (wav_mono_buffer == nullptr || wav_frames < kWindowFrames * 2) {
    return false;
  }
  const size_t windows = wav_frames / kWindowFrames;
  const size_t usable_windows = windows < kMaxWindows ? windows : kMaxWindows;
  uint32_t loudest = 0;
  size_t loudest_index = 0;
  for (size_t index = 0; index < usable_windows; ++index) {
    const int16_t* block = wav_mono_buffer + index * kWindowFrames;
    uint32_t peak = 0;
    for (size_t offset = 0; offset < kWindowFrames; ++offset) {
      const int32_t value = block[offset];
      const uint32_t magnitude = static_cast<uint32_t>(value < 0 ? -value : value);
      if (magnitude > peak) {
        peak = magnitude;
      }
    }
    window_peaks[index] = peak;
    if (peak > loudest) {
      loudest = peak;
      loudest_index = index;
    }
  }
  // 整段都没过绝对下限，说明只有房间底噪，裁了也没意义，走原音频。
  if (usable_windows < 2 || loudest < kCropFloor) {
    return false;
  }
  const uint32_t threshold = max(loudest * 35u / 100u, kCropFloor);
  size_t start_index = loudest_index;
  size_t gap = 0;
  while (start_index > 0) {
    if (window_peaks[start_index - 1] >= threshold) {
      --start_index;
      gap = 0;
    } else if (gap < kGapWindows) {
      --start_index;
      ++gap;
    } else {
      break;
    }
  }
  size_t end_index = loudest_index;
  gap = 0;
  while (end_index + 1 < usable_windows) {
    if (window_peaks[end_index + 1] >= threshold) {
      ++end_index;
      gap = 0;
    } else if (gap < kGapWindows) {
      ++end_index;
      ++gap;
    } else {
      break;
    }
  }
  const size_t span_begin = start_index * kWindowFrames;
  const size_t span_end =
      min(wav_frames, (end_index + 1) * kWindowFrames + kPadTailFrames);
  const size_t start_frame =
      span_begin > kPadHeadFrames ? span_begin - kPadHeadFrames : 0;
  if (span_end <= start_frame || span_end - start_frame < kMinSegmentFrames) {
    return false;
  }
  const size_t span_frames = span_end - start_frame;
  const size_t before_frames = wav_frames;

  // 峰值归一化：ASR 在接近满量程七成时最稳，太轻听不清，削波会糊掉声母。
  uint32_t span_peak = 0;
  for (size_t index = 0; index < span_frames; ++index) {
    const int32_t value = wav_mono_buffer[start_frame + index];
    const uint32_t magnitude = static_cast<uint32_t>(value < 0 ? -value : value);
    if (magnitude > span_peak) {
      span_peak = magnitude;
    }
  }
  if (start_frame != 0) {
    memmove(wav_mono_buffer,
            wav_mono_buffer + start_frame,
            span_frames * sizeof(int16_t));
  }
  wav_frames = span_frames;
  if (span_peak > 0) {
    const float gain = (0.7f * 32767.0f) / static_cast<float>(span_peak);
    if (gain < 0.98f || gain > 1.02f) {
      for (size_t index = 0; index < wav_frames; ++index) {
        int32_t value = static_cast<int32_t>(
            static_cast<float>(wav_mono_buffer[index]) * gain);
        if (value > 32767) {
          value = 32767;
        } else if (value < -32768) {
          value = -32768;
        }
        wav_mono_buffer[index] = static_cast<int16_t>(value);
      }
    }
  }
  Serial.printf("[VAD] wake trim: %u -> %u frames (%.2fs -> %.2fs)\n",
                static_cast<unsigned int>(before_frames),
                static_cast<unsigned int>(wav_frames),
                static_cast<double>(before_frames) / kSampleRate,
                static_cast<double>(wav_frames) / kSampleRate);
  return true;
}

// 对话路径专用：只砍掉句首那段空等，不做「最响簇」裁剪。
// 原因：VAD 从窗口起点就开始录（8 秒窗口），用户可能到第 4 秒才开口，
// 前半段全是底噪。但一句话中间的停顿能有几百毫秒，用 trim_wav_to_loudest_span
// 那套 50ms 的 gap 容差会把后半句直接切掉，所以这里只按 VAD 给出的起点裁。
// 顺带做峰值归一化：麦克风 PGA 只有 24 dB，实测人声峰值常年在 -30 dBFS 附近，
// ASR 在这个电平上会幻听，抬到 0.7 满量程后才稳定。
// 峰值归一化到 0.7 满量程（只放大不衰减）。会话裁剪与流式唤醒探针共用：
// 麦克风 PGA 只有 24 dB，实测人声峰值常年在 -30 dBFS 附近，ASR 在这个
// 电平上会幻听，抬到 0.7 满量程后才稳定。
void normalize_capture_peak() {
  if (wav_mono_buffer == nullptr || wav_frames == 0) {
    return;
  }
  uint32_t peak = 0;
  for (size_t index = 0; index < wav_frames; ++index) {
    const int32_t value = wav_mono_buffer[index];
    const uint32_t magnitude = static_cast<uint32_t>(value < 0 ? -value : value);
    if (magnitude > peak) {
      peak = magnitude;
    }
  }
  // 只放大不衰减：已经够响的句子压小反而丢动态
  if (peak > 0) {
    const float gain = (0.7f * 32767.0f) / static_cast<float>(peak);
    if (gain > 1.02f) {
      for (size_t index = 0; index < wav_frames; ++index) {
        int32_t value = static_cast<int32_t>(
            static_cast<float>(wav_mono_buffer[index]) * gain);
        if (value > 32767) {
          value = 32767;
        } else if (value < -32768) {
          value = -32768;
        }
        wav_mono_buffer[index] = static_cast<int16_t>(value);
      }
    }
  }
}

bool trim_capture_to_speech_onset() {
  constexpr size_t kPadHeadFrames = kSampleRate / 5;  // 句首留 0.2s，保住声母
  if (wav_mono_buffer == nullptr || wav_frames == 0) {
    return false;
  }
  size_t start = speech_onset_frames;
  if (start > kPadHeadFrames) {
    start -= kPadHeadFrames;
  } else {
    start = 0;
  }
  // 裁完不足 100ms 说明切错了，宁可整段原样送出去
  if (start + kSampleRate / 10 >= wav_frames) {
    Serial.println("[VAD] conversation trim skipped (too short after onset)");
    return false;
  }
  const size_t before_frames = wav_frames;
  if (start > 0) {
    memmove(wav_mono_buffer,
            wav_mono_buffer + start,
            (wav_frames - start) * sizeof(int16_t));
    wav_frames -= start;
  }

  normalize_capture_peak();
  Serial.printf(
      "[VAD] conversation trim: %u -> %u frames (%.2fs -> %.2fs), "
      "onset=%.2fs\n",
      static_cast<unsigned int>(before_frames),
      static_cast<unsigned int>(wav_frames),
      static_cast<double>(before_frames) / kSampleRate,
      static_cast<double>(wav_frames) / kSampleRate,
      static_cast<double>(speech_onset_frames) / kSampleRate);
  return true;
}

void build_wav_file() {
  constexpr uint16_t kChannels = 1;
  constexpr uint16_t kBitsPerSample = 16;
  const uint32_t kDataBytes = static_cast<uint32_t>(wav_frames * sizeof(int16_t));
  wav_bytes = 44 + kDataBytes;
  uint8_t* header = wav_file;
  memset(wav_file, 0, wav_bytes);
  memcpy(header + 0, "RIFF", 4);
  write_little_endian32(header + 4, wav_bytes - 8);
  memcpy(header + 8, "WAVEfmt ", 8);
  write_little_endian32(header + 16, 16);
  write_little_endian16(header + 20, 1);
  write_little_endian16(header + 22, kChannels);
  write_little_endian32(header + 24, kSampleRate);
  write_little_endian32(header + 28, kSampleRate * kChannels * sizeof(int16_t));
  write_little_endian16(header + 32, kChannels * sizeof(int16_t));
  write_little_endian16(header + 34, kBitsPerSample);
  memcpy(header + 36, "data", 4);
  write_little_endian32(header + 40, kDataBytes);
  memcpy(wav_file + 44, wav_mono_buffer, kDataBytes);
}

// 把任意 PCM 片段封装成 16k 单声道 WAV（播放中的语音指令走这条路）
bool build_wav_from_pcm(const int16_t* pcm, size_t frames) {
  constexpr uint16_t kChannels = 1;
  constexpr uint16_t kBitsPerSample = 16;
  if (wav_file == nullptr || pcm == nullptr || frames == 0) {
    return false;
  }
  const uint32_t data_bytes = static_cast<uint32_t>(frames * sizeof(int16_t));
  if (44 + data_bytes > kMaxWavBytes) {
    Serial.println("[WAV] pcm too long for wav buffer");
    return false;
  }
  wav_bytes = 44 + data_bytes;
  uint8_t* header = wav_file;
  memcpy(header + 0, "RIFF", 4);
  write_little_endian32(header + 4, wav_bytes - 8);
  memcpy(header + 8, "WAVEfmt ", 8);
  write_little_endian32(header + 16, 16);
  write_little_endian16(header + 20, 1);
  write_little_endian16(header + 22, kChannels);
  write_little_endian32(header + 24, kSampleRate);
  write_little_endian32(header + 28, kSampleRate * kChannels * sizeof(int16_t));
  write_little_endian16(header + 32, kChannels * sizeof(int16_t));
  write_little_endian16(header + 34, kBitsPerSample);
  memcpy(header + 36, "data", 4);
  write_little_endian32(header + 40, data_bytes);
  memcpy(wav_file + 44, pcm, data_bytes);
  wav_frames = frames;
  return true;
}

void capture_wav() {
  if (!capture_mono_samples()) {
    return;
  }
  build_wav_file();

  Serial.printf("WAV_BEGIN bytes=%lu rate=%lu channels=1 bits=16\n",
                static_cast<unsigned long>(wav_bytes),
                static_cast<unsigned long>(kSampleRate));
  Serial.write(wav_file, wav_bytes);
  Serial.flush();
  Serial.println();
  Serial.println("WAV_END");
}

bool connect_wifi_for_upload() {
  if (strlen(SPARKBOT_WIFI_SSID) == 0) {
    Serial.println("[WIFI] upload disabled: configure include/wifi_config.h first");
    return false;
  }

  WiFi.mode(WIFI_STA);
  // ESP32 默认开着 modem sleep，突发上传时吞吐会被压到几十 KB/s，
  // 同一个 94KB 的唤醒音频因此要传 1 秒以上。桌宠是插电/近距离场景，
  // 关掉省电换延迟，实测上传吞吐能翻倍。
  WiFi.setSleep(false);
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WIFI] reusing connection, ip=%s\n",
                  WiFi.localIP().toString().c_str());
    return true;
  }
  WiFi.begin(SPARKBOT_WIFI_SSID, SPARKBOT_WIFI_PASSWORD);
  Serial.printf("[WIFI] connecting to %s", SPARKBOT_WIFI_SSID);
  const unsigned long deadline = millis() + 15000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[WIFI] connection failed, status=%d\n", static_cast<int>(WiFi.status()));
    return false;
  }
  Serial.printf("[WIFI] connected, ip=%s\n", WiFi.localIP().toString().c_str());
  return true;
}

bool post_audio_to_hub(String& media_id) {
  if (strlen(SPARKBOT_HUB_BASE_URL) == 0) {
    Serial.println("[HUB] upload disabled: configure include/wifi_config.h first");
    return false;
  }

  WiFiClient client;
  HTTPClient http;
  const String url = String(SPARKBOT_HUB_BASE_URL) + "/api/v1/portable/v1/media";
  if (!http.begin(client, url)) {
    Serial.println("[HUB] HTTP client initialization failed");
    return false;
  }
  http.setTimeout(30000);
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("X-Portable-Device-Id", SPARKBOT_DEVICE_ID);
  http.addHeader("X-Media-Type", "audio");
  http.addHeader("X-Audio-Format", "wav");
  Serial.printf("[HUB] POST %s bytes=%u\n", url.c_str(), static_cast<unsigned int>(wav_bytes));
  const int response_code = http.POST(wav_file, wav_bytes);
  const String response = http.getString();
  http.end();
  if (response_code < 200 || response_code >= 300) {
    Serial.printf("[HUB] media upload failed: HTTP %d body=%s\n",
                  response_code,
                  response.c_str());
    return false;
  }

  DynamicJsonDocument document(2048);
  if (deserializeJson(document, response) != DeserializationError::Ok) {
    Serial.println("[HUB] media response is not valid JSON");
    return false;
  }
  media_id = document["media_id"].as<String>();
  if (media_id.isEmpty()) {
    Serial.println("[HUB] media response has no media_id");
    return false;
  }
  Serial.printf("[HUB] media_id=%s\n", media_id.c_str());
  return true;
}

bool submit_audio_request(
    const String& media_id,
    const char* session_mode,
    String& request_id) {
  // millis() 断电就归零：设备重启后前几分钟生成的 ID 会跟上次开机同期
  // 撞车，中枢按重复请求拒绝（409），用户喊的第一句就凭空丢了。
  // 掺上 esp_random() 保证跨开机唯一，真正的重复提交重试才走得到去重逻辑。
  request_id = String("wearable-") + String(millis()) + "-" +
               String(esp_random(), HEX);
  const String conversation_id = String("wearable-") + String(SPARKBOT_DEVICE_ID);
  String body;
  body.reserve(560);
  body = "{\"portable_device_id\":\"";
  body += SPARKBOT_DEVICE_ID;
  body += "\",\"conversation_id\":\"";
  body += conversation_id;
  body += "\",\"request_id\":\"";
  body += request_id;
  body += "\",\"protocol_version\":\"1.0\",\"source\":\"wearable\",\"session_mode\":\"";
  body += session_mode;
  body += "\",\"input_type\":\"audio\",\"media_id\":\"";
  body += media_id;
  // 上报设备自身状态：中枢靠它回答「现在音乐音量多少」「在不在放歌」
  // 「屏幕亮不亮」。说话音量由中枢自己管，这里不上报，避免互相覆盖。
  body += "\",\"device_state\":{\"music_step\":";
#ifdef PET_FOUNTAIN
  body += music_player::volume_step_value();
#else
  body += 0;
#endif
  body += ",\"music\":\"";
#ifdef PET_FOUNTAIN
  body += (music_player::active() ? "playing" : "idle");
#else
  body += "idle";
#endif
  body += "\",\"fountain\":";
  body += (fountain::active() ? "true" : "false");
  body += ",\"screen\":\"";
  body += (g_display_on ? "on" : "off");
  body += "\"}}";

  WiFiClient client;
  HTTPClient http;
  const String url = String(SPARKBOT_HUB_BASE_URL) + "/api/v1/portable/v1/requests";
  if (!http.begin(client, url)) {
    Serial.println("[HUB] request client initialization failed");
    return false;
  }
  http.setTimeout(30000);
  http.addHeader("Content-Type", "application/json");
  const int response_code = http.POST(body);
  const String response = http.getString();
  http.end();
  Serial.printf("[HUB] request HTTP %d body=%s\n", response_code, response.c_str());
  return response_code >= 200 && response_code < 300;
}

bool read_stream_exact(Stream* stream, uint8_t* buffer, size_t length) {
  // 不能再用 Stream::readBytes：Arduino 里它是逐字节 timedRead 实现的，
  // 一段 164KB 的语音要读 2.8 秒，首字延迟全耗在这。
  // HTTPClient::getStreamPtr() 返回的就是 Client，它的 read(buf,len) 会一次性
  // 把 socket 缓冲区里的数据拷出来，比逐字节快一个量级。
  size_t received = 0;
  Client* client = static_cast<Client*>(stream);
  const unsigned long deadline = millis() + 15000;
  while (received < length) {
    const int available = client->available();
    if (available > 0) {
      const size_t want = min(static_cast<size_t>(available), length - received);
      const int count = client->read(buffer + received, want);
      if (count > 0) {
        received += static_cast<size_t>(count);
        continue;
      }
    }
    if (millis() > deadline) {
      return false;
    }
    delay(1);
  }
  return true;
}

uint32_t read_little_endian32(const uint8_t* input) {
  return static_cast<uint32_t>(input[0]) |
         (static_cast<uint32_t>(input[1]) << 8) |
         (static_cast<uint32_t>(input[2]) << 16) |
         (static_cast<uint32_t>(input[3]) << 24);
}

uint16_t read_little_endian16(const uint8_t* input) {
  return static_cast<uint16_t>(input[0]) |
         (static_cast<uint16_t>(input[1]) << 8);
}

bool play_response_audio(const String& request_id) {
  // 流式播放：解析完 WAV 头、预缓冲 1.5s 就切 TX 开播，边下边补。
  // 旧版是整段下载后再播（324KB 要 2.6s，首字延迟全耗在这）。
  // 回程 ~125KB/s vs 播放消耗 32KB/s，净流入是播放的 ~4 倍，预缓冲
  // 足以吸收 WiFi 抖动；真断流就写静音，不让 DMA 复读旧块出破音。
  WiFiClient client;
  HTTPClient http;
  const String url = String(SPARKBOT_HUB_BASE_URL) +
                     "/api/v1/portable/v1/requests/" + request_id + "/audio";
  if (!http.begin(client, url)) {
    Serial.println("[AUDIO] response audio client initialization failed");
    return false;
  }
  http.setTimeout(30000);
  const int response_code = http.GET();
  // 音频是连续小包，关掉 Nagle 让每块数据立刻上交，别攒够一个 MSS 再给。
  client.setNoDelay(true);
  if (response_code != 200) {
    Serial.printf("[AUDIO] response audio unavailable: HTTP %d\n", response_code);
    http.end();
    return false;
  }

  Stream* stream = http.getStreamPtr();
  uint8_t riff[12] = {};
  if (!read_stream_exact(stream, riff, sizeof(riff)) ||
      memcmp(riff, "RIFF", 4) != 0 || memcmp(riff + 8, "WAVE", 4) != 0) {
    Serial.println("[AUDIO] response is not a PCM WAV file");
    http.end();
    return false;
  }

  // 按 chunk 走，不能假设 `data` 在固定偏移 36：
  // macOS say 写出来的 WAV 会夹一个 JUNK chunk，硬编码偏移会直接判失败。
  uint16_t channels = 0;
  uint32_t sample_rate = 0;
  uint16_t bits_per_sample = 0;
  uint32_t remaining = 0;
  bool have_fmt = false;
  bool have_data = false;
  uint8_t chunk_header[8] = {};
  while (!have_data && read_stream_exact(stream, chunk_header, sizeof(chunk_header))) {
    const uint32_t chunk_size = read_little_endian32(chunk_header + 4);
    if (memcmp(chunk_header, "fmt ", 4) == 0) {
      uint8_t fmt[16] = {};
      const size_t want = chunk_size < sizeof(fmt) ? chunk_size : sizeof(fmt);
      if (!read_stream_exact(stream, fmt, want)) break;
      channels = read_little_endian16(fmt + 2);
      sample_rate = read_little_endian32(fmt + 4);
      bits_per_sample = read_little_endian16(fmt + 14);
      have_fmt = true;
      // fmt 块可能比 16 字节长，把多余部分读掉（含奇数补齐字节）
      for (uint32_t skip = chunk_size - want + (chunk_size & 1); skip > 0; --skip) {
        if (stream->read() < 0) break;
      }
    } else if (memcmp(chunk_header, "data", 4) == 0) {
      remaining = chunk_size;
      have_data = true;
    } else {
      // 其他块（JUNK/LIST/fact…）：读掉内容 + 奇数补齐
      for (uint32_t skip = chunk_size + (chunk_size & 1); skip > 0; --skip) {
        if (stream->read() < 0) break;
      }
    }
  }

  if (!have_fmt || !have_data || channels != 1 || sample_rate != kSampleRate ||
      bits_per_sample != 16 || remaining == 0) {
    Serial.printf("[AUDIO] unsupported response format: rate=%lu channels=%u bits=%u data=%lu\n",
                  static_cast<unsigned long>(sample_rate),
                  static_cast<unsigned int>(channels),
                  static_cast<unsigned int>(bits_per_sample),
                  static_cast<unsigned long>(remaining));
    http.end();
    return false;
  }

  // 流式：不再整段进 PSRAM。预缓冲一小段就开播，边下边补。
  // 整段比预缓冲还短时自动退化为「下完再播」，行为与旧版一致。
  const uint32_t pcm_bytes = remaining;
  size_t buffer_capacity =
      pcm_bytes < static_cast<uint32_t>(kStreamPrebufferBytes)
          ? static_cast<size_t>(pcm_bytes)
          : kStreamPrebufferBytes;
  uint8_t* pcm_data = static_cast<uint8_t*>(ps_malloc(buffer_capacity));
  // 96KB 要不到就减半再试（96→48→24），别让一句回复因为内存分配失败直接没了。
  while (pcm_data == nullptr && buffer_capacity > kStreamPrebufferMinBytes) {
    buffer_capacity /= 2;
    pcm_data = static_cast<uint8_t*>(ps_malloc(buffer_capacity));
  }
  if (pcm_data == nullptr) {
    buffer_capacity = pcm_bytes < static_cast<uint32_t>(kStreamPrebufferBytes)
                          ? static_cast<size_t>(pcm_bytes)
                          : kStreamPrebufferMinBytes;
    pcm_data = static_cast<uint8_t*>(ps_malloc(buffer_capacity));
  }
  const bool pcm_in_psram = pcm_data != nullptr;
  if (pcm_data == nullptr) {
    pcm_data = static_cast<uint8_t*>(malloc(buffer_capacity));
  }
  if (pcm_data == nullptr) {
    Serial.printf("[AUDIO] response buffer allocation failed: %u bytes\n",
                  static_cast<unsigned int>(buffer_capacity));
    http.end();
    return false;
  }

  // 预缓冲：短句（≤96KB）整段下完再开播，长句攒满 96KB 再开播。
  // 原策略是「2 秒到点 + 够 8KB 就开播」，看似省首字延迟，实际在 WiFi 抖动时
  // 只攒到 28KB（0.9 秒音频）就开播，剩余 5.5 秒全靠边播边下 —— 只要链路
  // 跟不上播放速率就写静音补位，用户听到的就是「内容一长就丢字」。
  const size_t want_bytes =
      static_cast<size_t>(pcm_bytes) < buffer_capacity
          ? static_cast<size_t>(pcm_bytes)
          : buffer_capacity;
  const unsigned long fill_started = millis();
  // 硬上限 8s：真的完全没网才会走到这里，正常攒满就退出。
  const unsigned long prebuffer_deadline = fill_started + 8000;
  size_t buf_start = 0;
  size_t buf_end = 0;
  uint32_t downloaded = 0;
  while (buf_end < want_bytes && downloaded < pcm_bytes) {
    if (millis() > prebuffer_deadline) break;
    const int available = client.available();
    if (available <= 0) {
      delay(1);
      continue;
    }
    size_t want = min(static_cast<size_t>(available), buffer_capacity - buf_end);
    const uint32_t left = pcm_bytes - downloaded;
    if (want > static_cast<size_t>(left)) want = left;
    const int count = client.read(pcm_data + buf_end, want);
    if (count <= 0) {
      delay(1);
      continue;
    }
    buf_end += static_cast<size_t>(count);
    downloaded += static_cast<uint32_t>(count);
  }
  Serial.printf("[AUDIO] prebuffered %u bytes in %lums (%s), total %lu\n",
                static_cast<unsigned int>(buf_end),
                static_cast<unsigned long>(millis() - fill_started),
                pcm_in_psram ? "psram" : "heap",
                static_cast<unsigned long>(pcm_bytes));

  // 一个字节都没拿到就别开播了：切 TX 只会让功放空放一片静音，
  // 还白等一次 I2S 重启。直接失败，让上层走「没声」分支。
  if (buf_end == 0) {
    Serial.println("[AUDIO] nothing downloaded, abort playback");
    free(pcm_data);
    http.end();
    return false;
  }

  // 预缓冲够一小段就切 TX 开播；下载期间 I2S 仍保持 RX 常开不变
  stop_i2s();
  codec_adc_ready = false;
  if (!start_i2s(I2S_MODE_TX)) {
    free(pcm_data);
    http.end();
    return false;
  }
  if (!configure_codec_for_playback()) {
    Serial.println("[AUDIO] ES8311 response playback configuration failed");
    stop_i2s();
    free(pcm_data);
    http.end();
    return false;
  }

  digitalWrite(kPowerAmpEnablePin, HIGH);
  delay(20);
#ifdef PET_FOUNTAIN
  face::set_expression(face::kSpeaking);
#endif
  // 每块 2048 字节 = 64ms 音频。原来 512 字节一块，每块都要推到屏幕一次，
  // 屏幕刷一次要 ~18ms，喂数速度跟不上播放速度，就会出现卡顿+重复的破音。
  uint8_t pcm_buffer[2048];
  bool success = true;
  uint32_t played = 0;
  uint32_t chunk_index = 0;
  // 断流补静音的量：这块越大，被「吃掉」的语音越多。正常应该是 0。
  uint32_t underrun_chunks = 0;
  uint32_t underrun_bytes = 0;
  unsigned long stall_deadline = millis() + 8000;
  while (played < pcm_bytes) {
    // ① 泵网络：先把已播掉的头部空间压实，再往空出来的尾巴搬。
    // 顺序很关键。原来条件是 `buf_end < buffer_capacity`，也就是缓冲一写满就
    // 再也进不来这个循环 —— 哪怕已经播掉一半、buf_start 前移了很多，也不压缩，
    // TCP 接收窗口随即被堵死，网络彻底停摆；只有等整段播到 buf_start==buf_end
    // 归零后才恢复下载。于是长句变成「攒满→停下载→播干→重新攒」的锯齿，
    // 中间全靠写静音补位，听感正是「内容一长就丢字、卡住」。
    while (downloaded < pcm_bytes) {
      if (buf_start > 0) {
        memmove(pcm_data, pcm_data + buf_start, buf_end - buf_start);
        buf_end -= buf_start;
        buf_start = 0;
      }
      if (buf_end >= buffer_capacity) break;
      const int available = client.available();
      if (available <= 0) break;
      size_t want = min(static_cast<size_t>(available), buffer_capacity - buf_end);
      const uint32_t left = pcm_bytes - downloaded;
      if (want > static_cast<size_t>(left)) want = left;
      const int count = client.read(pcm_data + buf_end, want);
      if (count <= 0) break;
      buf_end += static_cast<size_t>(count);
      downloaded += static_cast<uint32_t>(count);
    }
    const size_t wanted =
        min(static_cast<size_t>(sizeof(pcm_buffer)),
            static_cast<size_t>(pcm_bytes - played));
    if (buf_end - buf_start >= wanted) {
      // ② 有整块就播一块（音量缩放 + I2S + 嘴型，逻辑与旧版一致）
      memcpy(pcm_buffer, pcm_data + buf_start, wanted);
      buf_start += wanted;
      if (buf_start == buf_end) {
        buf_start = 0;
        buf_end = 0;
      }
      played += static_cast<uint32_t>(wanted);
      stall_deadline = millis() + 8000;
      int16_t* pcm_samples = reinterpret_cast<int16_t*>(pcm_buffer);
      int32_t chunk_peak = 0;
      for (size_t index = 0; index < wanted / sizeof(int16_t); ++index) {
        pcm_samples[index] /= kPlaybackVolumeDivisor;
        const int32_t magnitude = abs(static_cast<int32_t>(pcm_samples[index]));
        if (magnitude > chunk_peak) chunk_peak = magnitude;
      }
      // 先把音频写进 DMA（阻塞式，会自动等空间），再去画屏，
      // 这样画屏耗掉的时间由 128ms 的 DMA 缓冲兜着，不会把音频抽干。
      size_t bytes_written = 0;
      const esp_err_t write_error = i2s_write(
          kI2sPort, pcm_buffer, wanted, &bytes_written, pdMS_TO_TICKS(1000));
      if (write_error != ESP_OK || bytes_written != wanted) {
        Serial.printf("[AUDIO] response playback failed: error=%s bytes=%u\n",
                      esp_err_to_name(write_error),
                      static_cast<unsigned int>(bytes_written));
        success = false;
        break;
      }
#ifdef PET_FOUNTAIN
      face::feed_amp(chunk_peak / 32768.0f * 3.2f);
      // 嘴型动画每 2 块（128ms）刷新一次就够顺滑了，刷太勤会抢走喂数的时间
      if ((chunk_index & 1u) == 0u) {
        face::tick();
      }
#endif
      ++chunk_index;
    } else {
      // ③ 断流：写静音兜底，同时继续等网；8s 还没恢复就放弃。
      // 静音块用 512（16ms）而不是 2048（64ms）：断流时块越短，回到「泵网络」
      // 的轮询越密，链路一恢复就能立刻接上，少吞一截内容。播放路径仍用 2048，
      // 那是为了减少画屏次数，两者诉求相反，别统一。
      memset(pcm_buffer, 0, sizeof(pcm_buffer));
      size_t bytes_written = 0;
      i2s_write(kI2sPort, pcm_buffer, 512, &bytes_written, pdMS_TO_TICKS(200));
      underrun_bytes += 512;
      ++underrun_chunks;
#ifdef PET_FOUNTAIN
      face::feed_amp(0.0f);
#endif
      if (millis() > stall_deadline) {
        Serial.println("[AUDIO] response stream stalled, giving up");
        success = false;
        break;
      }
      delay(1);
    }
  }

  http.end();
  free(pcm_data);
  i2s_zero_dma_buffer(kI2sPort);
  delay(30);
  stop_i2s();
  // GPIO46 是「背光 + 功放使能」共用脚，表情首页要常亮就必须一直是高电平。
  // 之前这里先拉低再拉高，功放使能翻转会在喇叭里「咔」一声（实测每隔几秒一次），
  // 所以播放结束不再动它。留 30ms 静音把最后一块音频播完再停 I2S 就够了。
#ifdef PET_FOUNTAIN
  face::reset_amp();
#endif
  // 断流补的静音会实实在在占掉音频时间轴：underrun_ms / 音频总长 就是「丢了多少」。
  Serial.printf("[AUDIO] response playback %s, underrun=%lums/%lums\n",
                success ? "completed" : "failed",
                static_cast<unsigned long>(underrun_bytes / 32u),
                static_cast<unsigned long>(pcm_bytes / 32u));
  return success;
}

void wait_for_audio_request(
    const String& request_id,
    uint32_t poll_interval_ms = kConversationPollIntervalMs,
    bool show_thinking = true) {
  const String url = String(SPARKBOT_HUB_BASE_URL) +
                     "/api/v1/portable/v1/requests/" + request_id;
  const unsigned long deadline = millis() + 60000;
#ifdef PET_FOUNTAIN
  // 只有真进入对话才显示「思考」；待机阶段校验唤醒词时保持首页表情，
  // 否则环境噪声会让屏幕不停闪思考脸
  if (show_thinking) {
    face::set_expression(face::kThinking);
  }
#endif
  while (millis() < deadline) {
    WiFiClient client;
    HTTPClient http;
    if (!http.begin(client, url)) {
      Serial.println("[HUB] request status client initialization failed");
      return;
    }
    http.setTimeout(10000);
    const int response_code = http.GET();
    const String response = http.getString();
    http.end();
    if (response_code != 200) {
      Serial.printf("[HUB] request status failed: HTTP %d body=%s\n",
                    response_code,
                    response.c_str());
      return;
    }

    DynamicJsonDocument document(4096);
    if (deserializeJson(document, response) != DeserializationError::Ok) {
      Serial.println("[HUB] request status is not valid JSON");
      return;
    }
    const String state = document["state"].as<String>();
    const String session_action = document["session_action"].as<String>();
    const String session_mode = document["session_mode"].as<String>();
    last_session_action = session_action;
    Serial.printf("[HUB] request state=%s", state.c_str());
    const char* transcript = document["transcript"] | "";
    const char* response_text = document["response_text"] | "";
    if (transcript[0] != '\0') {
      Serial.printf(" transcript=%s", transcript);
    }
    if (response_text[0] != '\0') {
      Serial.printf(" response=%s", response_text);
    }
    Serial.println();

    if (state == "completed" || state == "succeeded" || state == "failed" ||
        state == "planned") {
      const bool finished = state == "completed" || state == "succeeded";
      if (finished && session_action == "awake") {
#ifdef PET_FOUNTAIN
        // 音画同步（2026-11-10）：黑屏期间先把 TX 通路备好，然后亮屏
        // （= 功放供电）与「我在」出声同一瞬间发生，表情直接上说话嘴型。
        // 顺序不能反：功放和背光共用 GPIO46，黑屏里出声是哑的。
        const bool playback_ready = prepare_wakeup_playback();
        enter_face_state();
        face::set_expression(face::kSpeaking);
        face::tick();
        if (playback_ready) {
          play_wakeup_audio_prepared();
        }
#else
        play_wakeup_audio();
#endif
      } else if (finished && (session_action == "fountain_start" ||
                              session_action == "fountain_stop")) {
        // 先播 TTS 确认，再开关喷泉：说话时不跳，说完立刻开始跳
        play_response_audio(request_id);
#ifdef PET_FOUNTAIN
        const bool fountain_on = session_action == "fountain_start";
        if (fountain_on) {
          face::suspend();  // 喷泉要独占屏幕
          fountain::set_active(true);
          fountain::set_label("MIC");
          Serial.println("[FOUNTAIN] activated (voice)");
        } else {
          fountain::set_active(false);
          face::resume();   // 关掉喷泉后回到表情首页
          face::set_expression(face::kListening);
          Serial.println("[FOUNTAIN] deactivated (voice)");
        }
#endif
      } else if (finished && (session_action == "music_play" ||
                              session_action == "music_next" ||
                              session_action == "music_replay")) {
        // 点歌：先播 TTS 确认曲名，再进播放器（播放器独占 I2S 和屏幕）
        play_response_audio(request_id);
#ifdef PET_FOUNTAIN
        const String stream_path =
            document["device_action"]["stream_path"].as<String>();
        const String music_title = document["device_action"]["title"].as<String>();
        if (stream_path.length() > 0) {
          Serial.printf("[MUSIC] start playback: %s\n", music_title.c_str());
          music_player::play(stream_path.c_str(), music_title.c_str());
          Serial.println("[MUSIC] playback returned to voice loop");
        } else {
          Serial.println("[MUSIC] device_action has no stream_path");
        }
#endif
      } else if (finished &&
                 (session_action == "continue" ||
                  (session_action == "sleep" && session_mode == "conversation"))) {
        // 中枢对噪声残片会回空文本 + sleep，这种情况没有音频可播，
        // 直接跳过，避免多一次 404 请求和一次无谓的 I2S 切换（切换会有轻微响动）。
        const String response_text = document["response_text"].as<String>();
        if (response_text.length() > 0) {
          play_response_audio(request_id);
        }
      }
      if (state == "planned") {
        Serial.println("[HUB] hardware plan awaits confirmation; no command was executed");
      }
      return;
    }
#ifdef PET_FOUNTAIN
    // 轮询间隔里继续推动表情动画（HTTP GET 阻塞期间是静止的，这里补回节奏）
    const unsigned long wait_until = millis() + poll_interval_ms;
    while (millis() < wait_until) {
      face::tick();
      delay(20);
    }
#else
    delay(poll_interval_ms);
#endif
  }
  Serial.println("[HUB] request status polling timed out");
}

#ifdef PET_FOUNTAIN
// ---- 播放中的语音指令 ----
// 播放器把录到的一段 PCM 交过来，这里上传中枢识别，返回要执行的动作。
// 只做网络收发，绝不碰 I2S：播放器自己持有 I2S 全双工。
String music_control_action;
String music_control_path;
String music_control_title;

bool poll_music_command(
    const String& request_id,
    String& action,
    String& stream_path,
    String& title) {
  const String url = String(SPARKBOT_HUB_BASE_URL) +
                     "/api/v1/portable/v1/requests/" + request_id;
  const unsigned long deadline = millis() + 30000;
  while (millis() < deadline) {
    WiFiClient client;
    HTTPClient http;
    if (!http.begin(client, url)) {
      return false;
    }
    http.setTimeout(8000);
    const int response_code = http.GET();
    const String response = http.getString();
    http.end();
    if (response_code != 200) {
      Serial.printf("[MUSIC] control poll HTTP %d\n", response_code);
      return false;
    }
    DynamicJsonDocument document(4096);
    if (deserializeJson(document, response) != DeserializationError::Ok) {
      return false;
    }
    const String state = document["state"].as<String>();
    if (state == "completed" || state == "succeeded" || state == "failed") {
      action = document["session_action"].as<String>();
      stream_path = document["device_action"]["stream_path"].as<String>();
      title = document["device_action"]["title"].as<String>();
      Serial.printf("[MUSIC] control state=%s action=%s\n",
                    state.c_str(),
                    action.c_str());
      return true;
    }
    delay(80);
  }
  Serial.println("[MUSIC] control poll timed out");
  return false;
}

music_player::ControlCommand handle_music_control(const int16_t* pcm,
                                                  size_t frames) {
  music_player::ControlCommand empty = {nullptr, nullptr, nullptr};
  if (!build_wav_from_pcm(pcm, frames)) {
    return empty;
  }
  if (!connect_wifi_for_upload()) {
    return empty;
  }
  String media_id;
  if (!post_audio_to_hub(media_id)) {
    return empty;
  }
  String request_id;
  if (!submit_audio_request(media_id, "music_control", request_id)) {
    return empty;
  }
  String action;
  String stream_path;
  String title;
  if (!poll_music_command(request_id, action, stream_path, title)) {
    return empty;
  }
  music_control_action = action;
  music_control_path = stream_path;
  music_control_title = title;
  music_player::ControlCommand command = {nullptr, nullptr, nullptr};
  if (music_control_action.length() > 0) {
    command.action = music_control_action.c_str();
    command.stream_path = music_control_path.c_str();
    command.title = music_control_title.c_str();
  }
  return command;
}
#endif  // PET_FOUNTAIN

bool submit_captured_wav_to_hub(
    const char* session_mode,
    uint32_t poll_interval_ms = kConversationPollIntervalMs,
    bool show_thinking = true) {
  build_wav_file();
  if (!connect_wifi_for_upload()) {
    return false;
  }

  String media_id;
  if (!post_audio_to_hub(media_id)) {
    return false;
  }
  String request_id;
  if (submit_audio_request(media_id, session_mode, request_id)) {
    Serial.printf("[HUB] audio request accepted: request_id=%s\n", request_id.c_str());
    wait_for_audio_request(request_id, poll_interval_ms, show_thinking);
    return true;
  }
  return false;
}

void upload_wav_to_hub(const char* session_mode = "conversation") {
  if (wav_mono_buffer == nullptr || !capture_mono_samples()) {
    return;
  }
  submit_captured_wav_to_hub(session_mode);
}

// 表情态：亮屏、持续聆听。说的话直接送中枢（聊天 / 搜索 / 动手都行），
// 不需要唤醒词——屏幕上那张脸本身就是「我在」。
// 返回 true = 该熄屏待命了（「退下吧」或空闲超时）；false = 被串口打断。
// 空闲 45s 自动回休眠（2026-11-10）：中枢的会话窗口也只有 45s，醒着超过
// 它之后说的话会被中枢当环境音忽略（白醒），所以设备必须在窗口内自己回去。
bool converse_with_face() {
  constexpr unsigned long kIdleSleepMs = 45000;
#ifdef PET_FOUNTAIN
  enter_face_state();
#endif
  Serial.println("[VOICE] listening; say 退下吧 to sleep");
  unsigned long last_activity_ms = millis();
  // 流式唤醒的衔接（2026-11-11）：探针上传期间用户说了一半的命令还压在
  // DMA 里，会话态第一轮不清 DMA，把尾巴无缝接进识别；之后各轮照常清。
  bool first_round = true;
  while (true) {
    if (Serial.available() > 0) {
      Serial.println("[VOICE] conversation interrupted by serial input");
      return false;
    }
    if (millis() - last_activity_ms >= kIdleSleepMs) {
      Serial.println("[VOICE] idle 45s; back to standby");
      return true;  // 由 run_voice_conversation 统一走 enter_standby_state（闭眼过渡）
    }
    // 上一轮回复的尾音还在屋里回荡，先留静默窗口再清 DMA，
    // 否则会把自己的尾音当成下一句，形成自问自答的死循环。
    delay(kPostReplyGuardMs);
    if (i2s_started) {
      i2s_zero_dma_buffer(kI2sPort);
    }
    last_session_action = "";
    // 对话态多要一块连续过门限（3 块 = 192ms）：房间里的咳嗽、清嗓子
    // 会被挡在本地，不再白传一次 ASR、也不再让表情屏闪一下思考脸。
    if (!capture_until_silence(8, kVadSilenceChunks, kVadThreshold, 0, 3, 0, !first_round)) {
      first_round = false;
      continue;  // 没说话：idle 计时不重置，攒够 45s 就回去睡
    }
    first_round = false;
    last_activity_ms = millis();  // 有人说话了，空闲计时从这重新起算
    // 砍掉句首空等 + 峰值归一化，否则 8 秒窗口里可能 4 秒都是底噪，
    // 且人声电平只有 -30 dBFS，ASR 认不出来只能瞎猜。
    trim_capture_to_speech_onset();
    if (!submit_captured_wav_to_hub("conversation")) {
      Serial.println("[VOICE] hub unreachable; retry in 2s");
#ifdef PET_FOUNTAIN
      face::set_expression(face::kIdle);
#endif
      delay(2000);
      continue;
    }
    if (last_session_action == "sleep") {
      Serial.println("[VOICE] exit phrase recognised");
      return true;
    }
#ifdef PET_FOUNTAIN
    face::set_expression(face::kIdle);
    face::tick();
#endif
  }
}

// 待机态：熄屏，只听唤醒词。返回 true = 唤醒了。
// 流式探针：VAD 触发后攒 1.2s 就上传（不等尾音），唤醒词一出口就亮屏。
constexpr size_t kWakeProbeFrames = static_cast<size_t>(kSampleRate) * 12 / 10;  // 1.2s
bool wait_for_wake_word() {
  while (true) {
    if (Serial.available() > 0) {
      Serial.println("[VOICE] standby interrupted by serial input");
      return false;
    }
    last_session_action = "";
    // 待机窗口 4 秒（比原来的 3 秒宽），语音一旦确认就改成「从语音起点再给
    // 3 秒」，所以开口晚不会被切断，也不会因为窗口变长而让空转更久。
    // probe_frames=1.2s：攒够唤醒词长度立即上传（流式，不等尾音）。
    if (!capture_until_silence(4, kWakeVadSilenceChunks, kWakeVadThreshold, 3, 2, kWakeProbeFrames)) {
      continue;
    }
    // 连锁指令（2026-11-10 第三版）：凡是捕获到的候选一律整段上传。
    // 「贾维斯打开计算器」实测 2.9~3.4s——之前 2.8s 的上限把这些整句
    // 全部当成「房间聊天」跳过了，用户症状就是「只唤醒不做事」甚至
    // 「完全没反应」。窗口本身只有 4s（capture_until_silence 的
    // max_seconds），上传也就 ≤128KB ≈ 1s，可以接受；房间聊天的误上传
    // 由中枢 ASR + 门控过滤（未命中只白花一次校验，设备黑屏无感）。
    // 峰值归一化（2026-11-11）：探针音频不做归一化就会以 -23dBFS 的原始
    // 微弱电平上传，SenseVoice 只能猜出「嗯」「I.」——会话路径一直都有
    // 这一步，唤醒探针此前漏了。归一化后所有探针都以一致电平进识别。
    normalize_capture_peak();
    if (!submit_captured_wav_to_hub("wake", kWakePollIntervalMs, false)) {
      Serial.println("[VOICE] wake request failed; retry in 2s");
      delay(2000);
      continue;
    }
    if (last_session_action == "awake") {
      return true;
    }
    // 连锁指令：「贾维斯放首歌」这类话中枢会直接执行，返回的不是 awake
    // 而是真实的动作（放歌 / 喷泉 / 直接回答）。动作本身就意味着已经唤醒，
    // 也要回到表情态 —— 否则设备一边执行指令一边还在待机听唤醒词，
    // 播放器和麦克风抢同一个 I2S。
    if (last_session_action == "music_play" ||
        last_session_action == "music_next" ||
        last_session_action == "music_replay" ||
        last_session_action == "fountain_start" ||
        last_session_action == "fountain_stop" ||
        last_session_action == "continue") {
      return true;
    }
  }
}

void run_voice_conversation() {
  // 上电即静默待机（2026-11-10 改）：上电全程黑屏，不画首页也不画「闭眼」，
  // 喊「贾维斯」才第一次亮屏。此前（2026-11-06 版）上电会先亮表情首页、
  // 再让闭眼露 350ms 才熄屏，用户实测觉得「上电不该亮屏幕」。
  Serial.println("[VOICE] pet mode: silent standby at boot, say 贾维斯 to wake");
#ifdef PET_FOUNTAIN
  enter_standby_state(false);
#endif
  bool awake = false;
  while (true) {
    if (awake) {
      if (!converse_with_face()) {
        Serial.println("[VOICE] manual mode; send v to resume pet mode");
        return;
      }
#ifdef PET_FOUNTAIN
      enter_standby_state();
#endif
      awake = false;
    } else {
      if (!wait_for_wake_word()) {
        Serial.println("[VOICE] manual mode; send v to resume pet mode");
        return;
      }
      awake = true;
    }
  }
}

void print_help() {
  Serial.println("[CMD] a = initialize ES8311 and capture a short microphone sample");
  Serial.println("[CMD] w = capture 1 second mono WAV over USB (功放保持关闭)");
  Serial.println("[CMD] u = capture WAV and upload it to the hub over Wi-Fi");
  Serial.println("[CMD] k = upload a short clip for server-side wake-word detection");
  Serial.println("[CMD] v = 启动桌宠状态机（表情态 ⇄ 待机态）");
  Serial.println("[CMD] p = play fixed wake response 我在！");
  Serial.println("[CMD] t = play a 3-second 1kHz hardware test tone");
  Serial.println("[CMD] s = scan I2C bus");
#ifdef PET_FOUNTAIN
  Serial.println("[CMD] x = 手动「退下吧」（熄屏待机）   i = 手动亮屏回表情态");
  Serial.println("[CMD] f = 打开音乐喷泉   g = 关闭音乐喷泉   d = dump 频谱电平");
  Serial.println("[CMD] e = 表情预览（依次切换全部表情）");
#endif
  Serial.println("[CMD] h = print this help");
}

void scan_i2c() {
  start_i2c();
  Serial.println("[I2C] scanning...");
  int found = 0;
  for (uint8_t address = 0x03; address <= 0x77; ++address) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission(true) == 0) {
      Serial.printf("  found address: 0x%02X\n", address);
      ++found;
    }
  }
  Serial.printf("[I2C] devices: %d\n", found);
}

void handle_commands() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 'a' || command == 'A') {
      capture_audio();
    } else if (command == 'w' || command == 'W') {
      capture_wav();
    } else if (command == 'u' || command == 'U') {
      upload_wav_to_hub();
    } else if (command == 'k' || command == 'K') {
      upload_wav_to_hub("wake");
    } else if (command == 'v' || command == 'V') {
      run_voice_conversation();
    } else if (command == 'p' || command == 'P') {
      play_wakeup_audio();
    } else if (command == 't' || command == 'T') {
      play_test_tone();
    } else if (command == 's' || command == 'S') {
      scan_i2c();
    } else if (command == 'h' || command == 'H') {
      print_help();
    }
#ifdef PET_FOUNTAIN
    else if (command == 'f' || command == 'F') {
      fountain::begin();
      fountain::set_active(true);
      fountain::set_label("MIC");
      Serial.println("[FOUNTAIN] active (serial)");
    } else if (command == 'x' || command == 'X') {
      enter_standby_state();
    } else if (command == 'i' || command == 'I') {
      enter_face_state();
    } else if (command == 'g' || command == 'G') {
      fountain::set_active(false);
      Serial.println("[FOUNTAIN] inactive (serial)");
    } else if (command == 'd' || command == 'D') {
      fountain::dump_levels();
    } else if (command == 'e' || command == 'E') {
      if (!face::ready()) face::begin();
      face::resume();
      for (int i = 0; i <= static_cast<int>(face::kSleepy); ++i) {
        face::set_expression(static_cast<face::Expression>(i));
        Serial.printf("[FACE] expression %d\n", i);
        const unsigned long until = millis() + 1500;
        while (millis() < until) {
          face::tick();
          delay(20);
        }
      }
      face::set_expression(face::kIdle);
    }
#endif
  }
}

#ifdef PET_FOUNTAIN
// 待机（不在语音会话里）也让喷泉继续跳：自己读麦克风喂给显示，
// 同时保持串口命令可达（收到任意字节就退出去处理）。
void service_fountain_standby() {
  if (!start_i2s(I2S_MODE_RX)) {
    delay(100);
    return;
  }
  if (!configure_codec_for_adc()) {
    stop_i2s();
    delay(200);
    return;
  }
  while (fountain::active() && Serial.available() == 0) {
    size_t bytes_read = 0;
    const esp_err_t error = i2s_read(kI2sPort,
                                     capture_buffer,
                                     sizeof(capture_buffer),
                                     &bytes_read,
                                     pdMS_TO_TICKS(100));
    if (error == ESP_OK && bytes_read >= sizeof(int16_t) * 2) {
      fountain::feed_stereo(capture_buffer, bytes_read / (sizeof(int16_t) * 2));
    }
  }
  stop_i2s();
}
#endif

}  // namespace

void setup() {
  Serial.begin(kSerialBaudRate);
  pinMode(kPowerAmpEnablePin, OUTPUT);
  digitalWrite(kPowerAmpEnablePin, LOW);
  wav_mono_buffer = static_cast<int16_t*>(ps_malloc(
      kMaxConversationFrames * sizeof(int16_t)));
  wav_file = static_cast<uint8_t*>(ps_malloc(kMaxWavBytes));
  delay(1200);
  Serial.println();
  Serial.println("========================================");
  Serial.println(" SparkBot wearable - audio capture test");
  Serial.println(" SAFE MODE: playback is manual-only with command p; PA GPIO46 defaults off");
  Serial.println("========================================");
  print_help();
  if (wav_mono_buffer == nullptr || wav_file == nullptr) {
    Serial.println("[WAV] PSRAM allocation failed; audio upload disabled");
  }
  connect_wifi_for_upload();
  scan_i2c();
#ifdef PET_FOUNTAIN
  // 上电不亮屏（2026-11-10 改）：只做显示初始化，不画首页表情。
  // screen_bus::begin 已不再自动点亮背光；GPIO46 在 setup 开头已拉低，
  // 这里再压一次，保证从上电到进入待机全程黑屏。
  fountain::begin();
  face::begin();
  music_player::set_control_handler(handle_music_control);
  set_display_and_amp(false);
  Serial.println("[PET] boot: screen stays off until wake word");
#endif
#ifdef PET_AUTO_VOICE
  // 可选：上电直接进语音待机（免去每次手动发 v）。在 platformio.ini 的
  // pet_voice 环境 build_flags 里加 -DPET_AUTO_VOICE 即可打开。
  Serial.println("[VOICE] auto start in 2s (PET_AUTO_VOICE)");
  delay(2000);
  run_voice_conversation();
#endif
}

void loop() {
  handle_commands();
#ifdef PET_FOUNTAIN
  if (fountain::active()) {
    service_fountain_standby();
    return;
  }
  face::tick();
#endif
  delay(10);
}

#endif
