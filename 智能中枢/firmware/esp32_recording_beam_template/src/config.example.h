#pragma once

// 复制为 config.h 后填写真实参数；config.h 不要提交到版本库。
#define HUB_WIFI_SSID "替换为 Wi-Fi 名称"
#define HUB_WIFI_PASSWORD "替换为 Wi-Fi 密码"
#define HUB_MQTT_HOST "替换为运行智能中枢电脑的局域网 IP"
#define HUB_MQTT_PORT 1883
#define HUB_DEVICE_ID "recording-beam-001"

// 真实硬件引脚必须根据录音豆原理图或现有固件填写，不能直接使用占位值。
#define HUB_STATUS_LED_PIN 2
#define HUB_BUTTON_PIN 0
