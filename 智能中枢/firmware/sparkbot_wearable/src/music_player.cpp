#if defined(PET_FOUNTAIN)

// 网络音乐播放器：中枢拉流 → ES8311 播放 + 喷泉显示 + 语音打断
//
// 与 audio_capture.cpp 的分工：本文件独占 I2S（全双工）和 ES8311，
// 播放期间不调用 audio_capture 里的任何 start_i2s / stop_i2s，
// 避免两边各自 i2s_driver_install 打架。识别指令时只做网络收发，
// 采集 buffer 从 I2S 读好后交给上层，上层不碰 I2S。

#include <Arduino.h>
#include <Client.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <math.h>

#include "face_display.h"
#include "fountain_display.h"
#include "music_player.h"
#include "wifi_config.h"

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
constexpr uint32_t kMclkFrequency = kSampleRate * 256;  // 4.096 MHz
constexpr uint8_t kMicGainReg = 0x05;

constexpr size_t kBlockFrames = 512;            // 32ms 一块
constexpr size_t kMaxCaptureSeconds = 3;
constexpr size_t kMaxCaptureFrames = kSampleRate * kMaxCaptureSeconds;
// 打断检测：两级判定
//   一级「粗判」——麦克风 RMS 超过音乐串音的估计值就怀疑有人在说话；
//   二级「确认」——立刻掐断音乐 160ms，这时麦克风里只剩人声和环境噪声，
//   峰值过线才认定真的在说话。这样既不会被喇叭串音误触发，也不需要 AEC。
constexpr float kCoarseFloor = 260.0f;   // 粗判绝对下限（RMS）
constexpr float kCoarsePeak = 900.0f;    // 粗判同时要求瞬时峰值过线
// 一级粗判必须**连续**这么多块（96ms）都过线才允许进入二级确认。
// 只靠单块会被鼓点、碰桌子的瞬态骗过去，实测每秒能触发 1~2 次，
// 每次都要掐断音乐 -> 听感就是「歌一顿一顿」。
constexpr size_t kCoarseBlocks = 3;
constexpr float kConfirmPeak = 1200.0f;  // 掐断音乐后的确认峰值门限
// 单块瞬时噪声（敲键盘、碰桌子）能轻松冲到 700 左右，必须要求多块同时过线，
// 否则播放中会不停「掐断->录 3s->问中枢」，音乐听起来一顿一顿的。
constexpr size_t kConfirmBlocks = 3;     // 确认阶段测几块
constexpr size_t kConfirmHits = 2;       // 其中至少几块过线才算人声
constexpr float kSilencePeak = 320.0f;   // 录音收尾的静音门限
constexpr size_t kTriggerBlocks = 3;     // 暂停态连续 3 块（96ms）
constexpr size_t kSilenceEndBlocks = 12; // 尾部静音 380ms 就收尾
constexpr size_t kPrerollBlocks = 24;    // 前滚缓冲 0.77s，避免切掉指令开头
// 播放刚启动时 I2S/ES8311 还没稳定，DMA 里是残留数据（实测 peak 直接顶到
// 32768），这期间一律不做打断检测，否则一放歌就被判成「有人在说话」。
constexpr unsigned long kPlayWarmupMs = 700;
// 暂停态没有音乐掩护，环境噪声峰值能到 ~470，单纯看峰值会误触发；
// 同时要求峰值和 RMS 都过线，人声（实测峰值 560~690）才进得来。
constexpr float kPausedPeak = 520.0f;
constexpr float kPausedRms = 170.0f;

// 用户要求「外放默认小声」：原来 -24dB + 0.5 衰减太狠，听着发闷、细节没了，
// 收回到 -18dB + 0.62，仍然是明显小于全音量。
constexpr int kVolumeDbList[4] = {-32, -18, -10, -4};
int volume_step = 1;
constexpr float kSoftwareScale = 0.62f;

bool i2s_ready = false;
bool stop_requested = false;
bool playing_flag = false;
music_player::ControlHandler control_handler = nullptr;

// 音量曲线：ES8311 REG32，0xBF(191) ≈ 0dB，每档 0.5dB
int dac_volume_register(int db) {
  int value = 191 + db * 2;
  if (value < 0) value = 0;
  if (value > 255) value = 255;
  return value;
}

bool codec_write(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kCodecAddress);
  Wire.write(reg);
  Wire.write(value);
  if (Wire.endTransmission(true) != 0) {
    return false;
  }
  delayMicroseconds(300);
  return true;
}

bool codec_read(uint8_t reg, uint8_t* value) {
  Wire.beginTransmission(kCodecAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(kCodecAddress, static_cast<size_t>(1), true) != 1) return false;
  *value = Wire.read();
  return true;
}

bool es8311_init() {
  Wire.begin(kCodecSdaPin, kCodecSclPin, 100000);
  Wire.setTimeOut(50);

  uint8_t probe = 0;
  if (!codec_read(0x00, &probe)) {
    Serial.println("[MUSIC] ES8311 no response");
    return false;
  }

  const uint8_t open_seq[][2] = {
      {0x0D, 0xFA}, {0x44, 0x08}, {0x44, 0x08}, {0x01, 0x30},
      {0x02, 0x00}, {0x03, 0x10}, {0x16, kMicGainReg}, {0x04, 0x10},
      {0x05, 0x00}, {0x0B, 0x00}, {0x0C, 0x00}, {0x10, 0x1F},
      {0x11, 0x7F}, {0x00, 0x80},
  };
  for (const auto& entry : open_seq) {
    if (!codec_write(entry[0], entry[1])) return false;
  }

  uint8_t reset = 0;
  if (!codec_read(0x00, &reset)) return false;
  reset &= static_cast<uint8_t>(~0x40);  // I2S slave
  if (!codec_write(0x00, reset)) return false;
  if (!codec_write(0x01, 0x3F)) return false;

  uint8_t bclk = 0;
  if (!codec_read(0x06, &bclk)) return false;
  bclk &= static_cast<uint8_t>(~0x20);
  if (!codec_write(0x06, bclk)) return false;

  // 4.096MHz MCLK / 16kHz 分频系数（12.288MHz 那张旧表是错的）
  const uint8_t fmt_seq[][2] = {
      {0x13, 0x10}, {0x1B, 0x0A}, {0x1C, 0x6A}, {0x44, 0x08},
      {0x02, 0x00}, {0x05, 0x00}, {0x03, 0x10}, {0x04, 0x20},
      {0x07, 0x00}, {0x08, 0xFF}, {0x06, 0x03},
  };
  for (const auto& entry : fmt_seq) {
    if (!codec_write(entry[0], entry[1])) return false;
  }

  uint8_t dac_fmt = 0;
  uint8_t adc_fmt = 0;
  if (!codec_read(0x09, &dac_fmt) || !codec_read(0x0A, &adc_fmt)) return false;
  dac_fmt = static_cast<uint8_t>((dac_fmt & 0xFC) | 0x0C);
  adc_fmt = static_cast<uint8_t>((adc_fmt & 0xFC) | 0x0C);
  if (!codec_write(0x09, dac_fmt) || !codec_write(0x0A, adc_fmt)) return false;
  adc_fmt &= static_cast<uint8_t>(~0x40);
  if (!codec_write(0x0A, adc_fmt)) return false;

  const uint8_t start_seq[][2] = {
      {0x17, 0xBF}, {0x0E, 0x02}, {0x14, 0x1A}, {0x0D, 0x01},
      {0x15, 0x40}, {0x37, 0x08}, {0x45, 0x00},
  };
  for (const auto& entry : start_seq) {
    if (!codec_write(entry[0], entry[1])) return false;
  }
  return true;
}

bool es8311_enable_dac(int db) {
  bool ok = true;
  ok = codec_write(0x31, 0x00) && ok;  // 解除静音
  ok = codec_write(0x12, 0x00) && ok;  // DAC 上电
  ok = codec_write(0x13, 0x10) && ok;  // 输出驱动
  ok = codec_write(0x37, 0x08) && ok;  // 旁路 EQ
  ok = codec_write(0x32, static_cast<uint8_t>(dac_volume_register(db))) && ok;
  return ok;
}

bool i2s_start_duplex() {
  if (i2s_ready) return true;
  const i2s_config_t config = {
      .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX),
      .sample_rate = kSampleRate,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      // DMA 给足（12 × 256 frames ≈ 192ms），吸收 HTTP 拉流的抖动，
      // 不至于网络稍慢就放空导致「咔」声。
      .dma_buf_count = 12,
      .dma_buf_len = 256,
      .use_apll = true,
      .tx_desc_auto_clear = true,
      .fixed_mclk = static_cast<int>(kMclkFrequency),
      .mclk_multiple = I2S_MCLK_MULTIPLE_256,
      .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
  };
  if (i2s_driver_install(kI2sPort, &config, 0, nullptr) != ESP_OK) {
    Serial.println("[MUSIC] I2S install failed");
    return false;
  }
  const i2s_pin_config_t pins = {
      .mck_io_num = kI2sMclkPin,
      .bck_io_num = kI2sBclkPin,
      .ws_io_num = kI2sWsPin,
      .data_out_num = kI2sDataOutPin,
      .data_in_num = kI2sDataInPin,
  };
  if (i2s_set_pin(kI2sPort, &pins) != ESP_OK) {
    Serial.println("[MUSIC] I2S pin setup failed");
    i2s_driver_uninstall(kI2sPort);
    return false;
  }
  i2s_zero_dma_buffer(kI2sPort);
  i2s_ready = true;
  return true;
}

void i2s_stop_duplex() {
  if (!i2s_ready) return;
  i2s_stop(kI2sPort);
  i2s_driver_uninstall(kI2sPort);
  i2s_ready = false;
}

uint32_t read_le32(const uint8_t* input) {
  return static_cast<uint32_t>(input[0]) |
         (static_cast<uint32_t>(input[1]) << 8) |
         (static_cast<uint32_t>(input[2]) << 16) |
         (static_cast<uint32_t>(input[3]) << 24);
}

// ---------------------------------------------------------------- 拉流
struct MusicStream {
  WiFiClient client;
  HTTPClient http;
  WiFiClient* body = nullptr;
  bool open = false;
  bool unknown_size = false;
  uint32_t data_left = 0;
  size_t position = 0;  // 已消费的 data 字节数
};

void close_stream(MusicStream& stream) {
  if (stream.open) {
    stream.http.end();
    stream.open = false;
  }
  // 拉流是长连接，http.end() 只关 HTTP 层；socket 不显式 stop 掉，
  // 下一次重播复用同一个 WiFiClient 会读到脏状态，GET 直接超时（实测 -7）。
  stream.client.stop();
  stream.body = nullptr;
  stream.data_left = 0;
  stream.unknown_size = false;
}

// 流式读取的原型：open_stream 里读 WAV 头就要用，实现放在后面。
size_t read_stream_exact(Stream* stream, uint8_t* buffer, size_t length,
                         unsigned long timeout_ms = 2000);

bool open_stream(MusicStream& stream, const char* path, size_t offset) {
  close_stream(stream);
  stream.client = WiFiClient();  // 换一个干净的 socket
  const String url = String(SPARKBOT_HUB_BASE_URL) + path;
  if (!stream.http.begin(stream.client, url)) {
    Serial.println("[MUSIC] stream client init failed");
    return false;
  }
  stream.http.setTimeout(15000);
  if (offset > 0) {
    stream.http.addHeader("Range", String("bytes=") + String(offset) + "-");
  }
  const int code = stream.http.GET();
  if (code != 200 && code != 206) {
    Serial.printf("[MUSIC] stream HTTP %d\n", code);
    stream.http.end();
    return false;
  }
  stream.body = stream.http.getStreamPtr();
  uint8_t header[44] = {};
  if (stream.body == nullptr || read_stream_exact(stream.body, header, sizeof(header)) != sizeof(header) ||
      memcmp(header, "RIFF", 4) != 0 || memcmp(header + 8, "WAVE", 4) != 0) {
    Serial.println("[MUSIC] stream is not PCM WAV");
    stream.http.end();
    return false;
  }
  const uint16_t channels = static_cast<uint16_t>(header[22] | (header[23] << 8));
  const uint32_t rate = read_le32(header + 24);
  if (channels != 1 || rate != kSampleRate) {
    Serial.printf("[MUSIC] unsupported format channels=%u rate=%lu\n",
                  static_cast<unsigned int>(channels),
                  static_cast<unsigned long>(rate));
    stream.http.end();
    return false;
  }
  stream.data_left = read_le32(header + 40);
  stream.unknown_size = stream.data_left == 0xFFFFFFFF || stream.data_left == 0;
  stream.position = offset;
  stream.open = true;
  Serial.printf("[MUSIC] stream open at %u bytes (%s)\n",
                static_cast<unsigned int>(offset),
                stream.unknown_size ? "unknown size" : "sized");
  return true;
}

// 同 audio_capture.cpp：Stream::readBytes 是逐字节 timedRead，实测只有
// ~57KB/s。音乐流要 32KB/s（16k/16bit/单声道），余量太小，一有抖动就欠载。
// HTTPClient::getStreamPtr() 返回的其实是 Client，一次 read(buf,len) 就能把
// socket 缓冲里的数据整块拷走，快一个量级。
size_t read_stream_exact(Stream* stream, uint8_t* buffer, size_t length,
                         unsigned long timeout_ms) {
  Client* client = static_cast<Client*>(stream);
  size_t received = 0;
  const unsigned long deadline = millis() + timeout_ms;
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
    if (millis() > deadline) break;
    delay(1);
  }
  return received;
}

// 读一块单声道 PCM；返回实际帧数，0 表示流结束
size_t read_block(MusicStream& stream, int16_t* mono, size_t frames) {
  if (!stream.open || stream.body == nullptr) return 0;
  size_t want = frames * sizeof(int16_t);
  if (!stream.unknown_size && stream.data_left < want) {
    want = stream.data_left;
  }
  if (want == 0) return 0;
  const size_t got = read_stream_exact(stream.body, reinterpret_cast<uint8_t*>(mono), want);
  if (got == 0) return 0;
  if (!stream.unknown_size) {
    stream.data_left -= got;
  }
  stream.position += got;
  return got / sizeof(int16_t);
}

// ---------------------------------------------------------------- 音频输出
int16_t mono_block[kBlockFrames];
int16_t stereo_block[kBlockFrames * 2];
int16_t mic_block[kBlockFrames * 2];

void write_music_block(const int16_t* mono, size_t frames) {
  for (size_t index = 0; index < frames; ++index) {
    const float scaled = static_cast<float>(mono[index]) * kSoftwareScale;
    int32_t value = static_cast<int32_t>(scaled);
    if (value > 32767) value = 32767;
    if (value < -32768) value = -32768;
    stereo_block[index * 2] = static_cast<int16_t>(value);
    stereo_block[index * 2 + 1] = static_cast<int16_t>(value);
  }
  size_t written = 0;
  i2s_write(kI2sPort, stereo_block, frames * sizeof(int16_t) * 2, &written,
            pdMS_TO_TICKS(200));
}

void write_silence(size_t frames) {
  memset(stereo_block, 0, frames * sizeof(int16_t) * 2);
  size_t written = 0;
  i2s_write(kI2sPort, stereo_block, frames * sizeof(int16_t) * 2, &written,
            pdMS_TO_TICKS(200));
}

// 读一块麦克风（取左声道），返回 RMS 电平，同时把峰值写进 peak_out。
// timeout_ms 很关键：播放中如果在这里阻塞 32ms，主循环就没在往 TX 写数据，
// I2S DMA 会周期性放空 -> 喇叭里「咔」一声、声音发闷。播放路径传小值。
float read_mic_level(float* peak_out, uint32_t timeout_ms = 120) {
  size_t bytes = 0;
  const esp_err_t error = i2s_read(kI2sPort, mic_block, sizeof(mic_block), &bytes,
                                   pdMS_TO_TICKS(timeout_ms));
  if (error != ESP_OK || bytes < sizeof(int16_t) * 2) {
    if (peak_out != nullptr) *peak_out = 0.0f;
    return 0.0f;
  }
  const size_t frames = bytes / (sizeof(int16_t) * 2);
  double sum = 0.0;
  int32_t peak = 0;
  for (size_t index = 0; index < frames; ++index) {
    const int32_t sample = mic_block[index * 2];
    sum += static_cast<double>(sample) * sample;
    const int32_t magnitude = sample < 0 ? -sample : sample;
    if (magnitude > peak) peak = magnitude;
  }
  if (peak_out != nullptr) *peak_out = static_cast<float>(peak);
  return static_cast<float>(sqrt(sum / (frames > 0 ? frames : 1)));
}

float block_rms(const int16_t* mono, size_t frames) {
  if (frames == 0) return 0.0f;
  double sum = 0.0;
  for (size_t index = 0; index < frames; ++index) {
    const double sample = mono[index];
    sum += sample * sample;
  }
  return static_cast<float>(sqrt(sum / frames));
}

void apply_volume() {
  const int db = kVolumeDbList[volume_step];
  codec_write(0x32, static_cast<uint8_t>(dac_volume_register(db)));
  Serial.printf("[MUSIC] volume=%ddB (step %d/3)\n", db, volume_step);
}

void poll_serial() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 'v' || command == 'V') {
      volume_step = (volume_step + 1) % 4;
      apply_volume();
    } else if (command == 'x' || command == 'X' || command == 'g' || command == 'G') {
      Serial.println("[MUSIC] stop requested (serial)");
      stop_requested = true;
    }
  }
}

}  // namespace

namespace music_player {

void set_control_handler(ControlHandler handler) { control_handler = handler; }

void step_volume(int delta) {
  int next = (volume_step + delta) % 4;
  if (next < 0) next += 4;
  volume_step = next;
  apply_volume();
}

void cycle_volume() { step_volume(1); }

void request_stop() { stop_requested = true; }

bool active() { return playing_flag; }
int volume_step_value() { return volume_step; }

bool play(const char* stream_path, const char* title) {
  if (stream_path == nullptr || stream_path[0] == '\0') {
    return false;
  }
  playing_flag = true;
  stop_requested = false;

  int16_t* capture = static_cast<int16_t*>(ps_malloc(kMaxCaptureFrames * sizeof(int16_t)));
  if (capture == nullptr) {
    Serial.println("[MUSIC] capture buffer allocation failed");
    playing_flag = false;
    return false;
  }

  if (!i2s_start_duplex() || !es8311_init() || !es8311_enable_dac(kVolumeDbList[volume_step])) {
    Serial.println("[MUSIC] audio init failed");
    i2s_stop_duplex();
    free(capture);
    playing_flag = false;
    return false;
  }
  digitalWrite(kPowerAmpEnablePin, HIGH);  // 功放 = 背光，同一个脚

  face::suspend();
  fountain::set_active(true);
  fountain::set_label("MUSIC");
  Serial.printf("[MUSIC] playing %s\n", title != nullptr ? title : "(untitled)");
  Serial.printf("[MUSIC] volume=%ddB scale=%.2f\n", kVolumeDbList[volume_step], kSoftwareScale);

  MusicStream stream;
  if (!open_stream(stream, stream_path, 0)) {
    fountain::set_active(false);
    face::resume();
    i2s_stop_duplex();
    free(capture);
    playing_flag = false;
    return false;
  }

  bool paused = false;
  bool finished = false;
  size_t offset = 0;
  float bleed_ratio = 0.35f;
  size_t trigger_run = 0;
  unsigned long played_bytes = 0;
  unsigned long last_status_ms = 0;
  float peak_mic = 0.0f;
  const unsigned long warmup_until = millis() + kPlayWarmupMs;

  int16_t* preroll = static_cast<int16_t*>(
      ps_malloc(kPrerollBlocks * kBlockFrames * sizeof(int16_t)));
  size_t preroll_head = 0;
  size_t preroll_count = 0;

  while (!stop_requested) {
    poll_serial();

    if (paused) {
      write_silence(kBlockFrames);
      float peak = 0.0f;
      const float level = read_mic_level(&peak);
      // 暂停时没有音乐，峰值 + RMS 同时过线才认，避免环境噪声误触发
      trigger_run = (peak > kPausedPeak && level > kPausedRms) ? trigger_run + 1 : 0;
      if (trigger_run < kTriggerBlocks) {
        const unsigned long now = millis();
        if (now - last_status_ms >= 3000) {
          last_status_ms = now;
          Serial.printf("[MUSIC] paused, mic=%.0f peak=%.0f\n", level, peak);
        }
        continue;
      }
      trigger_run = 0;
      Serial.printf("[MUSIC] speech detected while paused (peak=%.0f)\n", peak);
    } else {
      const size_t frames = read_block(stream, mono_block, kBlockFrames);
      if (frames == 0) {
        Serial.println("[MUSIC] end of stream");
        finished = true;
        break;
      }
      // 先写音频（保持 TX 不断流），再读麦克风
      write_music_block(mono_block, frames);
      fountain::feed_mono(mono_block, frames);
      played_bytes += frames * sizeof(int16_t);
      offset = stream.position;

      const float music_level = block_rms(mono_block, frames);
      if (millis() < warmup_until) {
        // 暖机期：I2S/ES8311 尚未稳定，只推音乐、不采前滚、不做检测；
        // 但仍然把 RX 读掉，避免 DMA 队列积压。
        float discard = 0.0f;
        read_mic_level(&discard, 0);
        continue;
      }
      float peak = 0.0f;
      // 非阻塞：绝不能让麦克风读把 TX 喂数挤掉
      const float mic_level = read_mic_level(&peak, 2);

      // 前滚缓冲：留下最近 0.77s 的人声，指令开头才不会被吃掉
      if (preroll != nullptr && frames == kBlockFrames) {
        int16_t* slot = preroll + preroll_head * kBlockFrames;
        for (size_t index = 0; index < kBlockFrames; ++index) {
          slot[index] = mic_block[index * 2];
        }
        preroll_head = (preroll_head + 1) % kPrerollBlocks;
        if (preroll_count < kPrerollBlocks) ++preroll_count;
      }

      const float expected = bleed_ratio * music_level;
      const float coarse =
          expected * 1.3f > kCoarseFloor ? expected * 1.3f : kCoarseFloor;
      // 一级粗判：RMS 过串音估计线 **且** 峰值明显高于音乐串音，连续
      // kCoarseBlocks 块才认。只靠单块 RMS 会被鼓点骗，一次误判就是
      // 「掐断音乐 -> 确认 -> 恢复」，听感上歌就断了。
      const bool suspect = mic_level > coarse && peak > kCoarsePeak;
      trigger_run = suspect ? trigger_run + 1 : 0;
      if (!suspect || trigger_run < kCoarseBlocks) {
        if (!suspect && music_level > 200.0f) {
          bleed_ratio = bleed_ratio * 0.98f + (mic_level / music_level) * 0.02f;
          if (bleed_ratio < 0.03f) bleed_ratio = 0.03f;
          if (bleed_ratio > 1.5f) bleed_ratio = 1.5f;
        }
        if (mic_level > peak_mic) peak_mic = mic_level;
        const unsigned long now = millis();
        if (now - last_status_ms >= 2000) {
          last_status_ms = now;
          Serial.printf(
              "[MUSIC] pos=%.1fs mic=%.0f peak=%.0f music=%.0f bleed=%.2f\n",
              static_cast<double>(stream.position) / (kSampleRate * 2),
              mic_level,
              peak_mic,
              music_level,
              bleed_ratio);
          peak_mic = 0.0f;
        }
        continue;
      }

      // 二级确认：立刻清空发送缓冲，音乐停 ~50ms，只测人声
      i2s_zero_dma_buffer(kI2sPort);
      // 掐断的瞬间 RX 里还压着上一块音乐，先读掉再开始计峰，
      // 否则这块残留会被当成「最大峰值」直接判成人声。
      float discard_peak = 0.0f;
      read_mic_level(&discard_peak, 10);
      float confirm_peak = 0.0f;
      size_t confirm_hits = 0;
      for (size_t round = 0; round < kConfirmBlocks; ++round) {
        float measured = 0.0f;
        read_mic_level(&measured, 15);
        if (measured > confirm_peak) confirm_peak = measured;
        if (measured >= kConfirmPeak) ++confirm_hits;
      }
      if (confirm_hits < kConfirmHits) {
        Serial.printf("[MUSIC] duck check rejected (peak=%.0f hits=%u)\n",
                      confirm_peak,
                      static_cast<unsigned int>(confirm_hits));
        trigger_run = 0;
        continue;
      }
      Serial.printf("[MUSIC] speech detected (peak=%.0f), listening\n", confirm_peak);
    }

    // ---- 录一段指令 ----
    fountain::set_label("LISTEN");
    size_t captured = 0;
    if (preroll != nullptr && preroll_count > 0) {
      for (size_t block = 0; block < preroll_count; ++block) {
        if (captured + kBlockFrames > kMaxCaptureFrames) break;
        const size_t index =
            (preroll_head + kPrerollBlocks - preroll_count + block) % kPrerollBlocks;
        memcpy(capture + captured, preroll + index * kBlockFrames,
               kBlockFrames * sizeof(int16_t));
        captured += kBlockFrames;
      }
    }
    preroll_count = 0;
    size_t silent_run = 0;
    while (captured + kBlockFrames <= kMaxCaptureFrames) {
      poll_serial();
      float peak = 0.0f;
      read_mic_level(&peak);
      write_silence(kBlockFrames);
      for (size_t index = 0; index < kBlockFrames; ++index) {
        capture[captured + index] = mic_block[index * 2];
      }
      captured += kBlockFrames;
      if (peak < kSilencePeak) {
        ++silent_run;
        if (silent_run >= kSilenceEndBlocks) {
          break;
        }
      } else {
        silent_run = 0;
      }
    }
    fountain::set_label("MUSIC");
    Serial.printf("[MUSIC] captured %.1fs for control\n",
                  static_cast<double>(captured) / kSampleRate);

    ControlCommand command = {nullptr, nullptr, nullptr};
    if (control_handler != nullptr && captured > 0) {
      command = control_handler(capture, captured);
    }
    const char* action = command.action;
    Serial.printf("[MUSIC] control action=%s\n", action != nullptr ? action : "(none)");

    if (action != nullptr && strcmp(action, "music_stop") == 0) {
      Serial.println("[MUSIC] stopped by voice");
      break;
    }
    if (action != nullptr &&
        (strcmp(action, "music_volume_up") == 0 ||
         strcmp(action, "music_volume_down") == 0)) {
      step_volume(strcmp(action, "music_volume_up") == 0 ? 1 : -1);
      continue;
    }
    if (action != nullptr && strcmp(action, "music_pause") == 0) {
      paused = true;
      Serial.println("[MUSIC] paused");
      continue;
    }
    if (action != nullptr && strcmp(action, "music_resume") == 0) {
      paused = false;
      Serial.println("[MUSIC] resumed");
      continue;
    }
    if (action != nullptr &&
        (strcmp(action, "music_replay") == 0 || strcmp(action, "music_next") == 0)) {
      const char* next_path =
          (command.stream_path != nullptr && command.stream_path[0] != '\0')
              ? command.stream_path
              : stream_path;
      close_stream(stream);
      delay(60);  // 给 lwIP 一点时间回收上一个 socket
      bool reopened = open_stream(stream, next_path, 0);
      if (!reopened) {
        delay(200);
        reopened = open_stream(stream, next_path, 0);
      }
      if (!reopened) {
        Serial.println("[MUSIC] reopen failed, keep playing current stream");
        paused = true;  // 不打断整个播放，停在暂停态等用户再说一次
        continue;
      }
      paused = false;
      offset = 0;
      continue;
    }
    // 没听懂 / music_ignore：直接接着放
    if (paused) {
      continue;
    }
    if (!stream.open) {
      finished = true;
      break;
    }
  }

  close_stream(stream);
  Serial.printf("[MUSIC] playback ended, played=%lu bytes (~%.1fs)\n",
                static_cast<unsigned long>(played_bytes),
                static_cast<double>(played_bytes) / (kSampleRate * 2));
  i2s_zero_dma_buffer(kI2sPort);
  i2s_stop_duplex();
  fountain::set_active(false);
  face::resume();
  if (preroll != nullptr) free(preroll);
  free(capture);
  playing_flag = false;
  return finished;
}

}  // namespace music_player

#endif  // PET_FOUNTAIN
