#pragma once

// 屏幕总线：全工程唯一的 TFT_eSPI 实例 + 背光控制
//
// 为什么要单独抽出来：表情首页（face）和音乐喷泉（fountain）都要往同一块
// ST7789 上画。如果各自 new 一个 TFT_eSPI，就会重复 tft.init()、重复配置
// SPI 引脚，屏幕会被重置、状态互相覆盖。所有模块统一拿这里的实例。
//
// 注意：GPIO46 同时是屏幕背光和功放使能（乐鑫 ESP-SparkBot 设计），
// 所以「点亮背光」= 「打开功放」，无法单独控制，见 set_backlight 注释。

#include <TFT_eSPI.h>

namespace screen_bus {

constexpr int kBacklightPin = 46;  // 背光 + 功放使能共用
constexpr int kWidth = 240;
constexpr int kHeight = 240;

// 唯一的屏幕实例（函数内静态，避免静态初始化顺序问题）
TFT_eSPI& tft();

// 初始化屏幕（幂等）。只做一次 tft.init() + 旋转，并点亮背光。
bool begin();

// 屏幕是否已经可用
bool ready();

// 背光 / 功放使能（同一个脚）。on=false 时屏幕全黑，功放也关闭。
void set_backlight(bool on);
bool backlight_on();

}  // namespace screen_bus
