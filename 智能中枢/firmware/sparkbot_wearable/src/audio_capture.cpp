#ifdef AUDIO_CAPTURE

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <esp_err.h>
#include <math.h>

#include "board_config.h"
#include "wozai_audio.h"

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
constexpr size_t kWavFrames = kSampleRate;
constexpr size_t kWavBytes = 44 + kWavFrames * sizeof(int16_t);

bool i2c_started = false;
bool i2s_started = false;

int16_t capture_buffer[kCaptureBufferSamples];
int16_t wav_mono_buffer[kWavFrames];
uint8_t wav_file[kWavBytes];

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
      {0x16, 0x24},
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
      // 12.288 MHz MCLK / 16 kHz sample rate coefficients.
      {0x02, 0x40},
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
      {0x16, 0x24},
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
      .dma_buf_count = 4,
      .dma_buf_len = 256,
      .use_apll = true,
      .tx_desc_auto_clear = false,
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

bool capture_mono_samples() {
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
  while (captured_frames < kWavFrames) {
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
    const size_t frames_to_copy = min(frames_read, kWavFrames - captured_frames);
    for (size_t frame = 0; frame < frames_to_copy; ++frame) {
      wav_mono_buffer[captured_frames + frame] = capture_buffer[frame * 2];
    }
    captured_frames += frames_to_copy;
  }
  stop_i2s();
  return true;
}

void play_wakeup_audio() {
  if (!start_i2s(I2S_MODE_TX)) {
    return;
  }
  if (!configure_codec_for_playback()) {
    Serial.println("[AUDIO] ES8311 playback configuration failed; stopping I2S");
    stop_i2s();
    return;
  }

  digitalWrite(kPowerAmpEnablePin, HIGH);
  Serial.println("[AUDIO] PA GPIO46 enabled");
  delay(20);

  constexpr size_t kMonoChunkSamples = 256;
  int16_t mono_buffer[kMonoChunkSamples];
  const int16_t* pcm_samples =
      reinterpret_cast<const int16_t*>(_tmp_sparkbot_wozai_20261016_pcm);
  size_t offset = 0;
  bool write_failed = false;
  while (offset < _tmp_sparkbot_wozai_20261016_pcm_len / sizeof(int16_t)) {
    const size_t remaining =
        _tmp_sparkbot_wozai_20261016_pcm_len / sizeof(int16_t) - offset;
    const size_t samples = min(remaining, kMonoChunkSamples);
    for (size_t index = 0; index < samples; ++index) {
      mono_buffer[index] = pcm_samples[offset + index];
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
    offset += samples;
  }

  i2s_zero_dma_buffer(kI2sPort);
  delay(30);
  digitalWrite(kPowerAmpEnablePin, LOW);
  Serial.println("[AUDIO] PA GPIO46 disabled");
  stop_i2s();
  if (!write_failed) {
    Serial.println("[AUDIO] playback completed");
  }
}

void play_test_tone() {
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
  digitalWrite(kPowerAmpEnablePin, LOW);
  Serial.println("[AUDIO] PA GPIO46 disabled");
  stop_i2s();
  if (!write_failed) {
    Serial.println("[AUDIO] 1kHz tone completed");
  }
}

void build_wav_file() {
  constexpr uint16_t kChannels = 1;
  constexpr uint16_t kBitsPerSample = 16;
  constexpr uint32_t kDataBytes = static_cast<uint32_t>(kWavFrames * sizeof(int16_t));
  uint8_t* header = wav_file;
  memset(wav_file, 0, sizeof(wav_file));
  memcpy(header + 0, "RIFF", 4);
  write_little_endian32(header + 4, kWavBytes - 8);
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

void capture_wav() {
  if (!capture_mono_samples()) {
    return;
  }
  build_wav_file();

  Serial.printf("WAV_BEGIN bytes=%lu rate=%lu channels=1 bits=16\n",
                static_cast<unsigned long>(kWavBytes),
                static_cast<unsigned long>(kSampleRate));
  Serial.write(wav_file, kWavBytes);
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
  Serial.printf("[HUB] POST %s bytes=%u\n", url.c_str(), static_cast<unsigned int>(kWavBytes));
  const int response_code = http.POST(wav_file, kWavBytes);
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

bool submit_audio_request(const String& media_id, String& request_id) {
  request_id = String("wearable-") + String(millis());
  const String conversation_id = String("wearable-") + String(SPARKBOT_DEVICE_ID);
  String body;
  body.reserve(420);
  body = "{\"portable_device_id\":\"";
  body += SPARKBOT_DEVICE_ID;
  body += "\",\"conversation_id\":\"";
  body += conversation_id;
  body += "\",\"request_id\":\"";
  body += request_id;
  body += "\",\"protocol_version\":\"1.0\",\"source\":\"wearable\",\"input_type\":\"audio\",\"media_id\":\"";
  body += media_id;
  body += "\"}";

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

void wait_for_audio_request(const String& request_id) {
  const String url = String(SPARKBOT_HUB_BASE_URL) +
                     "/api/v1/portable/v1/requests/" + request_id;
  const unsigned long deadline = millis() + 45000;
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

    if (state == "completed" || state == "failed" || state == "planned") {
      if (state == "planned") {
        Serial.println("[HUB] hardware plan awaits confirmation; no command was executed");
      }
      return;
    }
    delay(1000);
  }
  Serial.println("[HUB] request status polling timed out");
}

void upload_wav_to_hub() {
  if (!capture_mono_samples()) {
    return;
  }
  build_wav_file();
  if (!connect_wifi_for_upload()) {
    return;
  }

  String media_id;
  if (!post_audio_to_hub(media_id)) {
    return;
  }
  String request_id;
  if (submit_audio_request(media_id, request_id)) {
    Serial.printf("[HUB] audio request accepted: request_id=%s\n", request_id.c_str());
    wait_for_audio_request(request_id);
  }
}

void print_help() {
  Serial.println("[CMD] a = initialize ES8311 and capture a short microphone sample");
  Serial.println("[CMD] w = capture 1 second mono WAV over USB (功放保持关闭)");
  Serial.println("[CMD] u = capture WAV and upload it to the hub over Wi-Fi");
  Serial.println("[CMD] p = play fixed wake response 我在！");
  Serial.println("[CMD] t = play a 3-second 1kHz hardware test tone");
  Serial.println("[CMD] s = scan I2C bus");
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
    } else if (command == 'p' || command == 'P') {
      play_wakeup_audio();
    } else if (command == 't' || command == 'T') {
      play_test_tone();
    } else if (command == 's' || command == 'S') {
      scan_i2c();
    } else if (command == 'h' || command == 'H') {
      print_help();
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(kSerialBaudRate);
  pinMode(kPowerAmpEnablePin, OUTPUT);
  digitalWrite(kPowerAmpEnablePin, LOW);
  delay(1200);
  Serial.println();
  Serial.println("========================================");
  Serial.println(" SparkBot wearable - audio capture test");
  Serial.println(" SAFE MODE: playback is manual-only with command p; PA GPIO46 defaults off");
  Serial.println("========================================");
  print_help();
  scan_i2c();
}

void loop() {
  handle_commands();
  delay(10);
}

#endif
