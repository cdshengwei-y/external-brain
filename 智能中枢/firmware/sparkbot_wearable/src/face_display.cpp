#include "face_display.h"

#if defined(PET_FOUNTAIN) || defined(FACE_TEST)

#include <Arduino.h>
#include <limits.h>
#include <math.h>

#include "screen_bus.h"

namespace face {
namespace {

TFT_eSPI& tft = screen_bus::tft();

// 离屏画布：表情每帧先画在内存里，再整块推上去。
// 原来直接 fillRect 擦整块 -> 再画眼睛，中间有几十毫秒空白，
// 呼吸动画 40ms 一帧，看上去就是一直闪。
TFT_eSprite canvas(&tft);
bool canvas_ready = false;
TFT_eSPI* pen = &tft;

// ---------------- 布局 ----------------
constexpr int kW = 240;
constexpr int kH = 240;
constexpr int kTop = 28;        // 重绘区域上边界
constexpr int kClearH = 190;    // 重绘区域高度
constexpr int kEyeLX = 84;
constexpr int kEyeRX = 156;
constexpr int kEyeY = 104;
constexpr int kMouthCX = 120;
constexpr int kMouthTop = 158;  // 张嘴时的上唇线

// ---------------- 状态 ----------------
bool ready_flag = false;
bool suspended_flag = false;
Expression current = kIdle;
int last_sig = INT_MAX;
uint32_t last_render_ms = 0;

bool blinking = false;
uint32_t blink_at = 0;
uint32_t blink_next = 0;

int iris_target_x = 0;
int iris_target_y = 0;
float iris_x = 0.0f;
float iris_y = 0.0f;
uint32_t iris_move_at = 0;

float amp = 0.0f;
float amp_smooth = 0.0f;

uint16_t c_bg, c_eye, c_iris, c_pupil, c_mouth, c_mouth_dim, c_cheek;

// ---------------- 表情参数 ----------------
struct Style {
  int eye_rx;
  int eye_ry;
  int iris_r;
  int eye_mode;    // 0=椭圆眼 1=闭眼线 2=开心 ^ ^
  int mouth_mode;  // 0=微笑弧 1=张嘴 2=大笑 3=平线 4=三点
  int mouth_w;     // 半宽
  int mouth_h;     // 弧深 / 张嘴高度
};

const Style kStyles[] = {
    /* kIdle      */ {26, 30, 11, 0, 0, 34, 10},
    /* kListening */ {28, 34, 14, 0, 1, 15, 10},
    /* kThinking  */ {24, 28, 10, 0, 4, 16, 0},
    /* kSpeaking  */ {26, 30, 11, 0, 1, 18, 12},
    /* kHappy     */ {26, 30, 11, 2, 2, 40, 24},
    /* kSleepy    */ {26, 30, 0, 1, 3, 20, 0},
};

// ---------------- 基础绘图 ----------------
void fill_ellipse(int cx, int cy, int rx, int ry, uint16_t color) {
  if (rx <= 0 || ry <= 0) return;
  for (int y = -ry; y <= ry; ++y) {
    const float t = 1.0f - static_cast<float>(y * y) / static_cast<float>(ry * ry);
    if (t <= 0.0f) continue;
    const int dx = static_cast<int>(rx * sqrtf(t));
    pen->drawFastHLine(cx - dx, cy + y, dx * 2 + 1, color);
  }
}

// 抛物线弧线：depth > 0 向下弯（微笑），depth < 0 向上弯（^ ^）
void draw_curve(int cx, int cy, int half_w, int depth, int thick, uint16_t color) {
  if (half_w <= 0) return;
  for (int x = -half_w; x <= half_w; ++x) {
    const float u = static_cast<float>(x) / static_cast<float>(half_w);
    const int y = cy + static_cast<int>(depth * (1.0f - u * u));
    pen->drawFastVLine(cx + x, y - thick / 2, thick, color);
  }
}

// 张嘴：上唇平、下方半椭圆收拢
void draw_mouth_open(int cx, int top, int half_w, int h, uint16_t color) {
  if (h < 2) h = 2;
  for (int y = 0; y <= h; ++y) {
    const float t = 1.0f - static_cast<float>(y * y) / static_cast<float>(h * h);
    if (t < 0.0f) continue;
    const int dx = static_cast<int>(half_w * sqrtf(t));
    pen->drawFastHLine(cx - dx, top + y, dx * 2 + 1, color);
  }
}

void draw_eye(int cx, int cy, const Style& s, float blink_k, int dx, int dy) {
  if (s.eye_mode == 1) {  // 闭眼：一条浅弧
    draw_curve(cx, cy, s.eye_rx, 7, 5, c_eye);
    return;
  }
  if (s.eye_mode == 2) {  // 开心：^ ^
    draw_curve(cx, cy + 9, s.eye_rx, -13, 6, c_eye);
    return;
  }

  const int ry = max(2, static_cast<int>(s.eye_ry * blink_k));
  fill_ellipse(cx, cy, s.eye_rx, ry, c_eye);
  if (blink_k < 0.32f || s.iris_r <= 0) return;

  const int ir = max(3, static_cast<int>(s.iris_r * blink_k));
  // 限制虹膜不要跑出眼白
  const int limit_x = max(0, s.eye_rx - ir - 2);
  const int limit_y = max(0, ry - ir - 1);
  const int ix = cx + constrain(dx, -limit_x, limit_x);
  const int iy = cy + constrain(dy, -limit_y, limit_y);

  fill_ellipse(ix, iy, ir, ir, c_iris);
  fill_ellipse(ix, iy, max(1, ir * 52 / 100), max(1, ir * 52 / 100), c_pupil);
  pen->fillCircle(ix + ir / 3, iy - ir / 3, max(1, ir / 4), c_eye);
}

void draw_three_dots(int cx, int cy, int phase) {
  for (int i = 0; i < 3; ++i) {
    const bool on = (i == phase);
    pen->fillCircle(cx - 17 + i * 17, cy, on ? 5 : 3, on ? c_mouth : c_mouth_dim);
  }
}

int make_signature(int eye_ry, int mouth_h, int dx, int dy, int breath, int phase) {
  return (static_cast<int>(current) << 26) |
         ((eye_ry & 0x3F) << 20) |
         ((mouth_h & 0x3F) << 14) |
         (((dx + 16) & 0x1F) << 9) |
         (((dy + 16) & 0x1F) << 4) |
         ((breath + 2) & 0x03) | ((phase & 0x03) << 2);
}

}  // namespace

// ---------------- 公开接口 ----------------
bool begin() {
  if (ready_flag) return true;

  c_bg = tft.color565(9, 15, 32);
  c_eye = tft.color565(238, 250, 255);
  c_iris = tft.color565(0, 198, 255);
  c_pupil = tft.color565(7, 18, 46);
  c_mouth = tft.color565(255, 198, 88);
  c_mouth_dim = tft.color565(96, 74, 34);
  c_cheek = tft.color565(28, 58, 78);

  screen_bus::begin();
  tft.fillScreen(c_bg);

  // 240×240×16bit = 115KB，ESP32-S3 内部 RAM 放得下；失败就退回直绘
  canvas.setColorDepth(16);
  if (canvas.createSprite(kW, kH) != nullptr) {
    canvas.fillSprite(c_bg);
    pen = &canvas;
    canvas_ready = true;
    Serial.println("[FACE] offscreen canvas ready (no flicker)");
  } else {
    Serial.println("[FACE] canvas alloc failed, direct draw");
  }

  ready_flag = true;
  current = kIdle;
  blink_next = millis() + 1800;
  iris_move_at = millis();
  last_sig = INT_MAX;
  last_render_ms = 0;
  return true;
}

bool ready() { return ready_flag; }

void set_expression(Expression expression) {
  if (expression == current) return;
  current = expression;
  last_sig = INT_MAX;  // 强制重绘
  Serial.printf("[FACE] expression -> %d\n", static_cast<int>(expression));
  if (expression == kHappy) {
    blink_next = millis() + 6000;  // 开心时不眨眼
  }
}

Expression expression() { return current; }

void feed_amp(float level) {
  if (level < 0.0f) level = 0.0f;
  if (level > 1.0f) level = 1.0f;
  amp = level;
}

void reset_amp() {
  amp = 0.0f;
  amp_smooth = 0.0f;
}

void suspend() {
  suspended_flag = true;
  blinking = false;
}

void resume() {
  if (!ready_flag) {
    begin();
    return;
  }
  suspended_flag = false;
  screen_bus::set_backlight(true);
  last_sig = INT_MAX;
  last_render_ms = 0;
}

bool suspended() { return suspended_flag; }
bool visible() { return ready_flag && !suspended_flag; }

void tick() {
  if (!ready_flag || suspended_flag) return;

  const uint32_t now = millis();
  const uint32_t min_interval = (current == kSpeaking) ? 90 : 40;
  if (last_render_ms != 0 && now - last_render_ms < min_interval) return;

  const Style& s = kStyles[static_cast<int>(current)];

  // ---- 眨眼 ----
  float blink_k = 1.0f;
  const bool can_blink = (s.eye_mode == 0) && current != kHappy;
  if (can_blink) {
    if (!blinking && now >= blink_next) {
      blinking = true;
      blink_at = now;
    }
    if (blinking) {
      const float p = static_cast<float>(now - blink_at) / 150.0f;
      if (p >= 1.0f) {
        blinking = false;
        blink_next = now + static_cast<uint32_t>(random(2600, 5600));
      } else {
        blink_k = fabsf(cosf(PI * p));
      }
    }
  }
  const int eye_ry = max(2, static_cast<int>(s.eye_ry * blink_k));

  // ---- 瞳孔漂移 ----
  if (now >= iris_move_at) {
    const int span_x = max(1, s.eye_rx - s.iris_r - 4);
    const int span_y = max(1, s.eye_ry - s.iris_r - 4);
    iris_target_x = random(-span_x, span_x + 1);
    iris_target_y = random(-span_y / 2, span_y / 2 + 1);
    iris_move_at = now + static_cast<uint32_t>(random(2200, 5200));
  }
  if (current == kThinking) {
    iris_target_x = 6;
    iris_target_y = -7;
  } else if (current == kListening) {
    iris_target_x = 0;
    iris_target_y = 1;
  } else if (current == kSpeaking || current == kHappy) {
    iris_target_x = 0;
    iris_target_y = 0;
  }
  iris_x += (iris_target_x - iris_x) * 0.16f;
  iris_y += (iris_target_y - iris_y) * 0.16f;
  const int iris_dx = static_cast<int>(lroundf(iris_x));
  const int iris_dy = static_cast<int>(lroundf(iris_y));

  // ---- 呼吸 ----
  const int breath = (current == kSleepy || current == kIdle)
                         ? static_cast<int>(lroundf(sinf(now / 1300.0f) * 2.0f))
                         : 0;

  // ---- 口型 ----
  amp_smooth += (amp - amp_smooth) * 0.5f;
  int mouth_h = 0;
  if (s.mouth_mode == 1) {
    mouth_h = 5 + static_cast<int>(amp_smooth * 30.0f);
  }
  // 说话时嘴巴 2px 一档，减少无意义重绘
  mouth_h = (mouth_h / 2) * 2;

  const int phase = (now / 260) % 3;
  const int sig = make_signature(eye_ry, mouth_h, iris_dx, iris_dy, breath, phase);
  if (sig == last_sig) return;
  last_sig = sig;
  last_render_ms = now;

  // 眼睛的重绘要处理眨眼：用 blink_k 驱动，render 里只传最终参数
  pen = canvas_ready ? static_cast<TFT_eSPI*>(&canvas) : &tft;
  pen->fillRect(0, kTop, kW, kClearH, c_bg);
  fill_ellipse(50, kEyeY + 42 + breath, 17, 9, c_cheek);
  fill_ellipse(190, kEyeY + 42 + breath, 17, 9, c_cheek);

  const int eye_y = kEyeY + breath;
  // 眨眼只影响竖向半径，单独走一个带 blink_k 的绘制
  draw_eye(kEyeLX, eye_y, s, blink_k, iris_dx, iris_dy);
  draw_eye(kEyeRX, eye_y, s, blink_k, iris_dx, iris_dy);

  switch (s.mouth_mode) {
    case 0:
      draw_curve(kMouthCX, kMouthTop + 8, s.mouth_w, s.mouth_h, 6, c_mouth);
      break;
    case 1:
      draw_mouth_open(kMouthCX, kMouthTop + 6, s.mouth_w, mouth_h, c_mouth);
      break;
    case 2:
      draw_mouth_open(kMouthCX, kMouthTop - 4, s.mouth_w, s.mouth_h, c_mouth);
      break;
    case 3:
      draw_curve(kMouthCX, kMouthTop + 10, s.mouth_w, 2, 5, c_mouth);
      break;
    default:
      draw_three_dots(kMouthCX, kMouthTop + 10, phase);
      break;
  }

  // 一次性把画好的这块推上屏，避免「擦->画」之间的空白帧
  if (canvas_ready) {
    canvas.pushSprite(0, kTop, 0, kTop, kW, kClearH);
  }
}

}  // namespace face

#endif  // PET_FOUNTAIN || FACE_TEST
