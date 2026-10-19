#ifdef AUDIO_PROBE

#include <Arduino.h>
#include <Wire.h>

#include "board_config.h"

namespace {

constexpr int kCodecSdaPin = 4;
constexpr int kCodecSclPin = 5;
constexpr uint32_t kI2cClockHz = 100000;
constexpr uint8_t kCodecAddressCandidates[] = {0x18, 0x19};

bool i2c_started = false;

void start_i2c() {
  if (i2c_started) {
    return;
  }

  Wire.begin(kCodecSdaPin, kCodecSclPin, kI2cClockHz);
  Wire.setTimeOut(50);
  i2c_started = true;
  Serial.printf("[I2C] started: SDA=GPIO%d SCL=GPIO%d clock=%luHz\n",
                kCodecSdaPin,
                kCodecSclPin,
                static_cast<unsigned long>(kI2cClockHz));
}

void scan_i2c() {
  start_i2c();
  Serial.println("[I2C] scanning...");

  int found = 0;
  for (uint8_t address = 0x03; address <= 0x77; ++address) {
    Wire.beginTransmission(address);
    const uint8_t error = Wire.endTransmission(true);
    if (error == 0) {
      Serial.printf("  found address: 0x%02X\n", address);
      ++found;
    }
  }
  Serial.printf("[I2C] devices: %d\n", found);
}

bool read_register(uint8_t address, uint8_t reg, uint8_t* value) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  if (Wire.requestFrom(address, static_cast<size_t>(1), true) != 1) {
    return false;
  }
  *value = Wire.read();
  return true;
}

void probe_codec_registers() {
  start_i2c();
  Serial.println("[ES8311] read-only probe");

  for (uint8_t address : kCodecAddressCandidates) {
    Wire.beginTransmission(address);
    const uint8_t presence_error = Wire.endTransmission(true);
    if (presence_error != 0) {
      Serial.printf("  address 0x%02X: not responding (error=%u)\n",
                    address,
                    presence_error);
      continue;
    }

    uint8_t value = 0;
    if (read_register(address, 0x00, &value)) {
      Serial.printf("  address 0x%02X: register 0x00 = 0x%02X\n",
                    address,
                    value);
    } else {
      Serial.printf("  address 0x%02X: responds, register read failed\n", address);
    }
  }
}

void print_help() {
  Serial.println("[CMD] s = scan I2C bus");
  Serial.println("[CMD] r = read-only codec register probe");
  Serial.println("[CMD] h = print this help");
}

void handle_commands() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 's' || command == 'S') {
      scan_i2c();
    } else if (command == 'r' || command == 'R') {
      probe_codec_registers();
    } else if (command == 'h' || command == 'H') {
      print_help();
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(kSerialBaudRate);
  delay(1200);

  Serial.println();
  Serial.println("========================================");
  Serial.println(" SparkBot wearable - ES8311 bus probe");
  Serial.println(" SAFE MODE: read-only I2C, no audio output");
  Serial.println("========================================");
  print_help();
  scan_i2c();
  probe_codec_registers();
}

void loop() {
  handle_commands();
  delay(10);
}

#endif
