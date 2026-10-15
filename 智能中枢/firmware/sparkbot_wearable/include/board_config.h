#pragma once

// 第一版只做芯片、存储器、USB 串口和 Wi-Fi 扫描自检，不使用未知硬件 GPIO。
constexpr unsigned long kSerialBaudRate = 115200;
constexpr unsigned long kHeartbeatIntervalMs = 10000;
constexpr unsigned long kWifiScanIntervalMs = 30000;
