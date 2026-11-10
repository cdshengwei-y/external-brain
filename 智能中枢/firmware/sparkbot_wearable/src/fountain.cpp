#ifdef FOUNTAIN

// 音乐电子喷泉 · 独立演示固件（不跑语音链路时用这个）
//
//   MIC  模式：ES8311 ADC → I2S RX → fountain::feed_stereo → 屏幕随环境声律动
//   PLAY 模式：内置旋律合成 → I2S TX 出声 + 同一段 PCM 渲染 → 屏幕随自己的音乐律动
//
// 渲染逻辑全部在 fountain_display.cpp（与语音固件共用），本文件只管 I2S / codec / 命令。

#include <Arduino.h>
#include <Wire.h>
#include <driver/i2s.h>

#include "fountain_display.h"

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
constexpr int kBlockFrames = 512;

// ES8311 DAC REG32：0xBF ≈ 0dB，每档 0.5dB
constexpr int kVolumeDb[4] = {-30, -18, -10, 0};
int volume_step = 1;

int16_t tx_buf[2 * kBlockFrames];
int16_t mono_buf[kBlockFrames];

enum class Source { Mic, SelfPlay };
Source source = Source::Mic;  // 上电静音，发 p 才自播放
unsigned long frame_count = 0;
unsigned long last_stat_ms = 0;

bool codec_write(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kCodecAddress);
  Wire.write(reg);
  Wire.write(value);
  if (Wire.endTransmission(true) != 0) {
    Serial.printf("[ES8311] write fail reg=0x%02X val=0x%02X\n", reg, value);
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

int dac_volume_register(int db) {
  int reg = 191 + db * 2;
  if (reg < 0) reg = 0;
  if (reg > 255) reg = 255;
  return reg;
}

bool es8311_init() {
  Wire.begin(kCodecSdaPin, kCodecSclPin, 100000);
  Wire.setTimeOut(50);

  uint8_t probe = 0;
  if (!codec_read(0x00, &probe)) {
    Serial.println("[ES8311] no response (check mic board / I2C)");
    return false;
  }

  const uint8_t open_seq[][2] = {
      {0x0D, 0xFA}, {0x44, 0x08}, {0x44, 0x08}, {0x01, 0x30},
      {0x02, 0x00}, {0x03, 0x10}, {0x16, kMicGainReg}, {0x04, 0x10},
      {0x05, 0x00}, {0x0B, 0x00}, {0x0C, 0x00}, {0x10, 0x1F},
      {0x11, 0x7F}, {0x00, 0x80},
  };
  for (const auto& e : open_seq) {
    if (!codec_write(e[0], e[1])) return false;
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

  // 4.096MHz MCLK / 16kHz 系数
  const uint8_t fmt_seq[][2] = {
      {0x13, 0x10}, {0x1B, 0x0A}, {0x1C, 0x6A}, {0x44, 0x08},
      {0x02, 0x00}, {0x05, 0x00}, {0x03, 0x10}, {0x04, 0x20},
      {0x07, 0x00}, {0x08, 0xFF}, {0x06, 0x03},
  };
  for (const auto& e : fmt_seq) {
    if (!codec_write(e[0], e[1])) return false;
  }

  uint8_t dac_fmt = 0, adc_fmt = 0;
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
  for (const auto& e : start_seq) {
    if (!codec_write(e[0], e[1])) return false;
  }

  Serial.printf("[ES8311] ADC ready @ %luHz, MCLK=%luHz, gain reg=0x%02X\n",
                static_cast<unsigned long>(kSampleRate),
                static_cast<unsigned long>(kMclkFrequency),
                kMicGainReg);
  return true;
}

bool es8311_enable_dac(int db) {
  const int reg = dac_volume_register(db);
  bool ok = true;
  ok = codec_write(0x31, 0x00) && ok;  // 解除 DAC 静音
  ok = codec_write(0x12, 0x00) && ok;  // DAC 上电
  ok = codec_write(0x13, 0x10) && ok;  // 输出驱动
  ok = codec_write(0x37, 0x08) && ok;  // 旁路 EQ
  ok = codec_write(0x32, static_cast<uint8_t>(reg)) && ok;
  Serial.printf("[ES8311] DAC on, volume=%ddB reg=0x%02X\n", db, reg);
  return ok;
}

void apply_volume() {
  codec_write(0x32, static_cast<uint8_t>(dac_volume_register(kVolumeDb[volume_step])));
  Serial.printf("[PLAY] volume=%ddB step %d/3\n", kVolumeDb[volume_step], volume_step);
}

bool i2s_init_duplex() {
  const i2s_config_t config = {
      .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX),
      .sample_rate = kSampleRate,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 256,
      .use_apll = true,
      .tx_desc_auto_clear = true,
      .fixed_mclk = static_cast<int>(kMclkFrequency),
      .mclk_multiple = I2S_MCLK_MULTIPLE_256,
      .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
  };
  const esp_err_t err = i2s_driver_install(kI2sPort, &config, 0, nullptr);
  if (err != ESP_OK) {
    Serial.printf("[I2S] install fail: %s\n", esp_err_to_name(err));
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
    Serial.println("[I2S] pin setup fail");
    i2s_driver_uninstall(kI2sPort);
    return false;
  }
  i2s_zero_dma_buffer(kI2sPort);
  Serial.println("[I2S] duplex (TX+RX) started");
  return true;
}

void print_help() {
  Serial.println("[CMD] p = play/pause self-play   s = self-play   m = mic");
  Serial.println("[CMD] v = volume (-30/-18/-10/0 dB)");
  Serial.println("[CMD] d = dump band levels   r = reset peaks   h = help");
}

void handle_commands() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == 'd' || c == 'D') {
      fountain::dump_levels();
    } else if (c == 'r' || c == 'R') {
      fountain::reset_peaks();
      Serial.println("[CMD] peaks reset");
    } else if (c == 'h' || c == 'H') {
      print_help();
    } else if (c == 'p' || c == 'P') {
      if (source != Source::SelfPlay) {
        source = Source::SelfPlay;
        fountain::selfplay_set_playing(true);
      } else {
        fountain::selfplay_set_playing(!fountain::selfplay_playing());
      }
      Serial.printf("[PLAY] %s\n", fountain::selfplay_playing() ? "playing" : "paused");
      fountain::set_label(fountain::selfplay_playing() ? "PLAY" : "PAUSE");
    } else if (c == 's' || c == 'S') {
      source = Source::SelfPlay;
      fountain::selfplay_set_playing(true);
      fountain::set_label("PLAY");
      Serial.println("[PLAY] self-play mode");
    } else if (c == 'm' || c == 'M') {
      source = Source::Mic;
      fountain::set_label("MIC");
      Serial.println("[PLAY] mic mode");
    } else if (c == 'v' || c == 'V') {
      volume_step = (volume_step + 1) % 4;
      apply_volume();
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);

  Serial.println();
  Serial.println("========================================");
  Serial.println(" SparkBot music fountain (standalone demo)");
  Serial.println(" MOSI=47 SCLK=21 DC=43 CS=44 RST=NC BL/PA=46");
  Serial.println("========================================");

  fountain::begin();
  fountain::set_active(true);
  fountain::set_label("MIC");
  fountain::selfplay_init();

  if (!i2s_init_duplex() || !es8311_init()) {
    Serial.println("[FOUNTAIN] audio init failed; screen only");
  } else {
    es8311_enable_dac(kVolumeDb[volume_step]);
  }
  print_help();
  Serial.println("[PLAY] boot in MIC mode (silent); 'p' for self-play");
}

void loop() {
  handle_commands();

  if (source == Source::SelfPlay) {
    fountain::selfplay_render(tx_buf, mono_buf, kBlockFrames);
    size_t written = 0;
    i2s_write(kI2sPort, tx_buf, sizeof(tx_buf), &written, pdMS_TO_TICKS(200));
  } else {
    size_t bytes_read = 0;
    const esp_err_t err = i2s_read(kI2sPort, tx_buf, sizeof(tx_buf), &bytes_read,
                                   pdMS_TO_TICKS(200));
    if (err != ESP_OK || bytes_read < sizeof(tx_buf)) {
      delay(2);
      return;
    }
    fountain::feed_stereo(tx_buf, kBlockFrames);
  }

  ++frame_count;
  const unsigned long now = millis();
  if (now - last_stat_ms >= 2000) {
    last_stat_ms = now;
    Serial.printf("[FOUNTAIN] %s frames=%lu fps~%.1f\n",
                  source == Source::SelfPlay ? "PLAY" : "MIC",
                  frame_count, frame_count / 2.0f);
    frame_count = 0;
  }
}

#endif
