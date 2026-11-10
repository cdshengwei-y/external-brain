#ifdef LCD_TEST

#include <Arduino.h>
#include <TFT_eSPI.h>

TFT_eSPI tft = TFT_eSPI();

// GPIO46 同时是屏幕背光和功放(NS4150B)使能。点亮屏幕必须拉高；
// 自检阶段不播放音频，功放使能无输入，只有轻微底噪，无碍。
constexpr int kBacklightPin = 46;

void fill_and_report(uint16_t color, const char* name) {
  tft.fillScreen(color);
  Serial.printf("[LCD] fill %s (0x%04X)\n", name, color);
  delay(900);
}

void draw_spectrum_preview() {
  // 静态色带预览：为后续「音乐喷泉」验证多彩竖条显示。
  tft.fillScreen(TFT_BLACK);
  const int kBarCount = 8;
  const int kBarWidth = 24;
  const int kGap = 4;
  const uint16_t palette[kBarCount] = {
      TFT_RED, TFT_ORANGE, TFT_YELLOW, TFT_GREEN,
      TFT_CYAN, TFT_BLUE, TFT_MAGENTA, TFT_WHITE,
  };
  for (int i = 0; i < kBarCount; ++i) {
    const int x = 8 + i * (kBarWidth + kGap);
    const int h = 40 + (i % 4) * 45;
    const int y = 240 - h;
    tft.fillRect(x, y, kBarWidth, h, palette[i]);
  }
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("spectrum preview", 10, 8, 2);
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(kBacklightPin, OUTPUT);
  digitalWrite(kBacklightPin, HIGH);

  Serial.println();
  Serial.println("========================================");
  Serial.println(" SparkBot LCD self-test (ST7789 240x240)");
  Serial.println(" MOSI=47 SCLK=21 DC=43 CS=44 RST=NC BL=46");
  Serial.println("========================================");

  tft.init();
  tft.setRotation(0);
  tft.fillScreen(TFT_BLACK);
  Serial.println("[LCD] init OK, cycling colors...");

  fill_and_report(TFT_RED, "RED");
  fill_and_report(TFT_GREEN, "GREEN");
  fill_and_report(TFT_BLUE, "BLUE");
  fill_and_report(TFT_WHITE, "WHITE");
  fill_and_report(TFT_BLACK, "BLACK");

  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("SPARKBOT", 60, 70, 4);
  tft.setTextColor(TFT_GREEN, TFT_BLACK);
  tft.drawString("LCD OK", 76, 130, 4);
  tft.setTextColor(TFT_YELLOW, TFT_BLACK);
  tft.drawString("240 x 240", 84, 196, 2);
  Serial.println("[LCD] text shown; if you can read 'SPARKBOT LCD OK', wiring is correct");
  delay(2500);

  draw_spectrum_preview();
  Serial.println("[LCD] spectrum preview shown");
}

void loop() {
  delay(1000);
}

#endif
