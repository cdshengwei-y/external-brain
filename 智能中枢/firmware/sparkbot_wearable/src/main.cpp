#include <Arduino.h>
#include <WiFi.h>
#include <esp_chip_info.h>

#include "board_config.h"

#if !defined(AUDIO_PROBE) && !defined(AUDIO_CAPTURE)

namespace {

unsigned long last_heartbeat_ms = 0;
unsigned long last_scan_ms = 0;

void print_hex_u64(uint64_t value) {
  char buffer[17];
  snprintf(buffer, sizeof(buffer), "%08lX%08lX",
           static_cast<unsigned long>(value >> 32),
           static_cast<unsigned long>(value & 0xFFFFFFFFULL));
  Serial.print(buffer);
}

void print_chip_info() {
  esp_chip_info_t chip_info{};
  esp_chip_info(&chip_info);

  Serial.println("[INFO] chip");
  Serial.printf("  model: %s\n", ESP.getChipModel());
  Serial.printf("  revision: %u\n", chip_info.revision);
  Serial.printf("  cpu_mhz: %u\n", ESP.getCpuFreqMHz());
  Serial.printf("  cores: %u\n", chip_info.cores);
  Serial.printf("  flash_bytes: %u\n", ESP.getFlashChipSize());
  Serial.printf("  psram_found: %s\n", psramFound() ? "yes" : "no");
  Serial.printf("  psram_bytes: %u\n", ESP.getPsramSize());
  Serial.print("  efuse_mac: ");
  print_hex_u64(ESP.getEfuseMac());
  Serial.println();
}

void print_wifi_scan() {
  Serial.println("[WIFI] scanning...");
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(false, false);
  delay(100);

  const int network_count = WiFi.scanNetworks(false, true);
  if (network_count < 0) {
    Serial.printf("[WIFI] scan failed: %d\n", network_count);
    return;
  }

  Serial.printf("[WIFI] networks: %d\n", network_count);
  for (int index = 0; index < network_count; ++index) {
    Serial.printf("  %2d  %-32s  rssi=%4d  channel=%2d  auth=%d\n",
                  index + 1,
                  WiFi.SSID(index).c_str(),
                  WiFi.RSSI(index),
                  WiFi.channel(index),
                  static_cast<int>(WiFi.encryptionType(index)));
  }
  WiFi.scanDelete();
}

void print_help() {
  Serial.println("[CMD] i = print chip info");
  Serial.println("[CMD] s = scan Wi-Fi networks");
  Serial.println("[CMD] h = print this help");
}

void handle_serial_commands() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 'i' || command == 'I') {
      print_chip_info();
    } else if (command == 's' || command == 'S') {
      print_wifi_scan();
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
  Serial.println(" SparkBot wearable - hardware self-test");
  Serial.println(" SAFE MODE: no audio, motor, relay or GPIO drive");
  Serial.println("========================================");
  print_chip_info();
  print_help();
  print_wifi_scan();
  last_scan_ms = millis();
}

void loop() {
  handle_serial_commands();

  const unsigned long now = millis();
  if (now - last_heartbeat_ms >= kHeartbeatIntervalMs) {
    last_heartbeat_ms = now;
    Serial.printf("[HEARTBEAT] uptime_s=%lu free_heap=%lu free_psram=%lu wifi_status=%d\n",
                  now / 1000UL,
                  static_cast<unsigned long>(ESP.getFreeHeap()),
                  static_cast<unsigned long>(ESP.getFreePsram()),
                  static_cast<int>(WiFi.status()));
  }

  if (now - last_scan_ms >= kWifiScanIntervalMs) {
    last_scan_ms = now;
    print_wifi_scan();
  }
  delay(10);
}

#endif
