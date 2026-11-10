#include "screen_bus.h"

#if defined(FOUNTAIN) || defined(PET_FOUNTAIN) || defined(FACE_TEST)

#include <Arduino.h>

namespace screen_bus {
namespace {
bool initialized = false;
bool bl_on = false;
}  // namespace

TFT_eSPI& tft() {
  static TFT_eSPI instance;
  return instance;
}

bool begin() {
  if (initialized) return true;
  tft().init();
  tft().setRotation(0);
  tft().fillScreen(TFT_BLACK);
  initialized = true;
  // 不在这里点亮背光（2026-11-10 改）：上电必须保持黑屏，等唤醒词才亮。
  // 所有需要亮屏的路径都自己显式开背光：
  //   fountain::set_active(true) / face::resume() / set_display_and_amp(true)
  Serial.println("[SCREEN] ST7789 ready (240x240), backlight off");
  return true;
}

bool ready() { return initialized; }

void set_backlight(bool on) {
  pinMode(kBacklightPin, OUTPUT);
  digitalWrite(kBacklightPin, on ? HIGH : LOW);
  bl_on = on;
}

bool backlight_on() { return bl_on; }

}  // namespace screen_bus

#endif  // FOUNTAIN || PET_FOUNTAIN || FACE_TEST
