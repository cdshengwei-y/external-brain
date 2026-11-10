#include "fountain_display.h"

#if defined(FOUNTAIN) || defined(PET_FOUNTAIN)

#include <Arduino.h>
#include <math.h>
#include <TFT_eSPI.h>

#include "screen_bus.h"

namespace fountain {
namespace {

// ---------------- 显示/频谱常量 ----------------
constexpr int kScreenW = 240;
constexpr int kScreenH = 240;
constexpr int kFftSize = 512;  // 2^9，bin 宽 31.25 Hz
constexpr int kBarCount = 20;
constexpr int kBarTop = 26;
constexpr int kBarBottom = 3;
constexpr int kMaxBarH = kScreenH - kBarTop - kBarBottom;

constexpr float kDbFloor = -52.0f;
constexpr float kDbCeil = -6.0f;
constexpr float kAttack = 0.55f;
constexpr float kDecay = 0.10f;
constexpr float kPeakFall = 0.010f;

constexpr uint32_t kSampleRate = 16000;

// ---------------- 内置旋律（A 小调五声音阶，4 小节 ≈ 4.8s 循环） ----------------
struct Note {
  uint16_t freq;
  uint16_t ms;
};

constexpr Note kMelody[] = {
    {440, 300}, {523, 300}, {659, 300}, {587, 300},  // Am: A4 C5 E5 D5
    {523, 300}, {440, 300}, {392, 600},              // F : C5 A4 G4
    {330, 300}, {392, 300}, {440, 300}, {523, 300},  // C : E4 G4 A4 C5
    {587, 600}, {523, 300}, {440, 300},              // G : D5 C5 A4
};
constexpr int kNoteCount = static_cast<int>(sizeof(kMelody) / sizeof(kMelody[0]));
constexpr int kBassFreq[4] = {110, 87, 131, 98};
constexpr int kKicksPerBar = 2;

constexpr int kSinLut = 2048;

// 与表情首页共用同一个屏幕实例，不要各自 tft.init()
TFT_eSPI& tft = screen_bus::tft();
bool screen_ready = false;
bool is_active = false;

float fft_re[kFftSize];
float fft_im[kFftSize];
float hann[kFftSize];

int band_low_bin[kBarCount];
int band_high_bin[kBarCount];
uint16_t bar_color[kBarCount];

float level[kBarCount];
float peak[kBarCount];
int drawn_h[kBarCount];
int drawn_cap_y[kBarCount];

float sin_lut[kSinLut + 1];

// 麦克风分块累加
int16_t pending[kFftSize];
size_t pending_count = 0;

// ---------------- FFT ----------------
void fft_radix2(float* re, float* im, int n) {
  for (int i = 1, j = 0; i < n; ++i) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      float t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (int len = 2; len <= n; len <<= 1) {
    const float ang = -2.0f * PI / static_cast<float>(len);
    const float wb_r = cosf(ang), wb_i = sinf(ang);
    const int half = len >> 1;
    for (int i = 0; i < n; i += len) {
      float wr = 1.0f, wi = 0.0f;
      for (int k = 0; k < half; ++k) {
        const float ur = re[i + k], ui = im[i + k];
        const float xr = re[i + k + half], xi = im[i + k + half];
        const float vr = xr * wr - xi * wi;
        const float vi = xr * wi + xi * wr;
        re[i + k] = ur + vr; im[i + k] = ui + vi;
        re[i + k + half] = ur - vr; im[i + k + half] = ui - vi;
        const float nwr = wr * wb_r - wi * wb_i;
        wi = wr * wb_i + wi * wb_r;
        wr = nwr;
      }
    }
  }
}

// ---------------- 配色（红绿色盲友好：蓝→青→白→黄→橙） ----------------
uint16_t lerp565(uint8_t r0, uint8_t g0, uint8_t b0,
                 uint8_t r1, uint8_t g1, uint8_t b1, float t) {
  const uint8_t r = static_cast<uint8_t>(r0 + (r1 - r0) * t);
  const uint8_t g = static_cast<uint8_t>(g0 + (g1 - g0) * t);
  const uint8_t b = static_cast<uint8_t>(b0 + (b1 - b0) * t);
  return tft.color565(r, g, b);
}

uint16_t palette_color(float t) {
  static const uint8_t stops[6][3] = {
      {40, 90, 200}, {0, 170, 230}, {150, 230, 250},
      {255, 245, 160}, {255, 200, 60}, {255, 150, 20},
  };
  static const float edges[6] = {0.0f, 0.30f, 0.55f, 0.72f, 0.88f, 1.0f};
  if (t <= 0.0f) t = 0.0f;
  if (t >= 1.0f) t = 1.0f;
  int i = 0;
  while (i < 4 && t > edges[i + 1]) ++i;
  const float span = edges[i + 1] - edges[i];
  const float u = span > 0.0f ? (t - edges[i]) / span : 0.0f;
  return lerp565(stops[i][0], stops[i][1], stops[i][2],
                 stops[i + 1][0], stops[i + 1][1], stops[i + 1][2], u);
}

void build_window_and_bands() {
  for (int i = 0; i < kFftSize; ++i) {
    hann[i] = 0.5f * (1.0f - cosf(2.0f * PI * i / static_cast<float>(kFftSize - 1)));
  }
  const float f_min = 55.0f, f_max = 7800.0f;
  const float ratio = f_max / f_min;
  for (int b = 0; b < kBarCount; ++b) {
    const float f0 = f_min * powf(ratio, static_cast<float>(b) / kBarCount);
    const float f1 = f_min * powf(ratio, static_cast<float>(b + 1) / kBarCount);
    int lo = static_cast<int>(floorf(f0 * kFftSize / kSampleRate));
    int hi = static_cast<int>(ceilf(f1 * kFftSize / kSampleRate));
    if (lo < 1) lo = 1;
    if (hi <= lo) hi = lo + 1;
    if (hi > kFftSize / 2) hi = kFftSize / 2;
    band_low_bin[b] = lo;
    band_high_bin[b] = hi;
  }
  for (int b = 0; b < kBarCount; ++b) {
    bar_color[b] = palette_color(kBarCount > 1 ? static_cast<float>(b) / (kBarCount - 1) : 0.0f);
    level[b] = 0.0f;
    peak[b] = 0.0f;
    drawn_h[b] = 0;
    drawn_cap_y[b] = -1;
  }
}

void draw_static_frame() {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(tft.color565(150, 230, 250), TFT_BLACK);
  tft.drawString("MUSIC FOUNTAIN", 6, 5, 2);
  tft.drawFastHLine(0, kScreenH - 1, kScreenW, tft.color565(40, 60, 90));
}

void render_bars() {
  const int gap = 1;
  const int bar_w = 10;
  const int total_w = kBarCount * bar_w + (kBarCount - 1) * gap;
  const int x0 = (kScreenW - total_w) / 2;

  for (int b = 0; b < kBarCount; ++b) {
    const int x = x0 + b * (bar_w + gap);
    int h = static_cast<int>(level[b] * kMaxBarH + 0.5f);
    if (h < 0) h = 0;
    if (h > kMaxBarH) h = kMaxBarH;

    if (h > drawn_h[b]) {
      tft.fillRect(x, kScreenH - kBarBottom - h, bar_w, h - drawn_h[b], bar_color[b]);
    } else if (h < drawn_h[b]) {
      tft.fillRect(x, kScreenH - kBarBottom - drawn_h[b], bar_w, drawn_h[b] - h, TFT_BLACK);
    }
    drawn_h[b] = h;

    const int cap_h = static_cast<int>(peak[b] * kMaxBarH + 0.5f);
    const int new_cap_y = (cap_h > 4) ? (kScreenH - kBarBottom - cap_h - 2) : -1;
    if (drawn_cap_y[b] >= 0 && drawn_cap_y[b] < kScreenH - kBarBottom - h) {
      tft.fillRect(x, drawn_cap_y[b], bar_w, 2, TFT_BLACK);
    }
    if (new_cap_y >= 0) {
      tft.fillRect(x, new_cap_y, bar_w, 2, TFT_WHITE);
    }
    drawn_cap_y[b] = new_cap_y;
  }
}

void update_levels() {
  const float scale = static_cast<float>(kFftSize / 2) * 32768.0f;
  const float span_db = kDbCeil - kDbFloor;

  for (int b = 0; b < kBarCount; ++b) {
    float sum = 0.0f;
    int count = 0;
    for (int k = band_low_bin[b]; k < band_high_bin[b]; ++k) {
      sum += sqrtf(fft_re[k] * fft_re[k] + fft_im[k] * fft_im[k]);
      ++count;
    }
    const float avg = count > 0 ? sum / count : 0.0f;
    const float db = 20.0f * log10f(avg / scale + 1e-9f);
    float target = (db - kDbFloor) / span_db;
    if (target < 0.0f) target = 0.0f;
    if (target > 1.0f) target = 1.0f;
    if (target < 0.05f) {
      target = 0.0f;
    } else {
      target = (target - 0.05f) / 0.95f;
    }
    const float k = target > level[b] ? kAttack : kDecay;
    level[b] += (target - level[b]) * k;
    if (level[b] >= peak[b]) {
      peak[b] = level[b];
    } else {
      peak[b] -= kPeakFall;
      if (peak[b] < level[b]) peak[b] = level[b];
    }
  }
}

// 对一块 512 点 PCM 做 FFT + 更新 + 绘制
void process_block(const int16_t* mono) {
  for (int i = 0; i < kFftSize; ++i) {
    fft_re[i] = static_cast<float>(mono[i]) * hann[i];
    fft_im[i] = 0.0f;
  }
  fft_radix2(fft_re, fft_im, kFftSize);
  update_levels();
  if (is_active) render_bars();
}

// ---------------- 自播放合成器 ----------------
int note_start[kNoteCount + 1];
int loop_samples = 0;
int bar_samples = 0;
int hat_period = 0;
int hat_len = 0;
int kick_len = 0;

int synth_pos = 0;
int last_note = -1;
int last_bar = -1;
int last_kick = -1;
int last_hat = -1;

float melody_phase = 0.0f;
float bass_phase = 0.0f;
float kick_phase = 0.0f;
float melody_env = 0.0f;
float bass_env = 0.0f;
float kick_env = 0.0f;
float hat_env = 0.0f;

float melody_decay = 0.0f;
float bass_decay = 0.0f;
float kick_decay = 0.0f;
float hat_decay = 0.0f;

uint32_t rng_state = 0x9E3779B9u;
int32_t noise_prev = 0;
bool synth_ready = false;
bool synth_playing = true;

inline float fast_sin(float phase) {
  float x = phase * (kSinLut / (2.0f * PI));
  int i = static_cast<int>(x);
  const float f = x - static_cast<float>(i);
  i &= (kSinLut - 1);
  return sin_lut[i] + (sin_lut[i + 1] - sin_lut[i]) * f;
}

inline int16_t next_noise() {
  rng_state ^= rng_state << 13;
  rng_state ^= rng_state >> 17;
  rng_state ^= rng_state << 5;
  return static_cast<int16_t>(rng_state >> 16);
}

void build_synth_tables() {
  if (synth_ready) return;
  int acc = 0;
  for (int i = 0; i < kNoteCount; ++i) {
    note_start[i] = acc;
    acc += static_cast<int>(kMelody[i].ms * kSampleRate / 1000);
  }
  note_start[kNoteCount] = acc;
  loop_samples = acc;
  bar_samples = loop_samples / 4;
  hat_period = kSampleRate * 150 / 1000;
  hat_len = kSampleRate * 30 / 1000;
  kick_len = kSampleRate * 130 / 1000;

  melody_decay = expf(-1.0f / (0.45f * kSampleRate));
  bass_decay = expf(-1.0f / (0.90f * kSampleRate));
  kick_decay = expf(-1.0f / (0.09f * kSampleRate));
  hat_decay = expf(-1.0f / (0.015f * kSampleRate));
  synth_pos = 0;
  synth_ready = true;
}

}  // namespace

// ---------------- 公开接口 ----------------
bool begin() {
  if (screen_ready) return true;
  for (int i = 0; i <= kSinLut; ++i) {
    sin_lut[i] = sinf(2.0f * PI * static_cast<float>(i) / kSinLut);
  }
  screen_bus::begin();
  build_window_and_bands();
  pending_count = 0;
  screen_ready = true;
  return true;
}

bool ready() { return screen_ready; }

void set_active(bool on) {
  if (on) {
    if (!begin()) return;
    screen_bus::set_backlight(true);  // 背光（同时使能功放）
    delay(20);
    build_window_and_bands();
    draw_static_frame();
    pending_count = 0;
    is_active = true;
  } else {
    is_active = false;
    if (screen_ready) tft.fillScreen(TFT_BLACK);
    screen_bus::set_backlight(false);
  }
}

bool active() { return is_active; }

void set_label(const char* text) {
  if (!screen_ready || !is_active) return;
  tft.fillRect(150, 6, 88, 14, TFT_BLACK);
  tft.setTextColor(tft.color565(255, 220, 120), TFT_BLACK);
  tft.drawString(text, 158, 8, 1);
}

void feed_stereo(const int16_t* interleaved, size_t frames) {
  if (!is_active || interleaved == nullptr) return;
  for (size_t i = 0; i < frames; ++i) {
    pending[pending_count++] = interleaved[i * 2];
    if (pending_count == kFftSize) {
      process_block(pending);
      pending_count = 0;
    }
  }
}

void feed_mono(const int16_t* mono, size_t frames) {
  if (!is_active || mono == nullptr) return;
  for (size_t i = 0; i < frames; ++i) {
    pending[pending_count++] = mono[i];
    if (pending_count == kFftSize) {
      process_block(pending);
      pending_count = 0;
    }
  }
}

void reset_peaks() {
  for (int b = 0; b < kBarCount; ++b) peak[b] = 0.0f;
}

void dump_levels() {
  Serial.print("[SPEC] levels:");
  for (int b = 0; b < kBarCount; ++b) {
    Serial.printf(" %.2f", level[b]);
  }
  Serial.println();
}

void selfplay_init() {
  if (synth_ready) return;
  begin();
  build_synth_tables();
  Serial.printf("[SYNTH] loop=%d samples (%.2fs), notes=%d\n",
                loop_samples,
                loop_samples / static_cast<float>(kSampleRate),
                kNoteCount);
}

void selfplay_set_playing(bool playing) { synth_playing = playing; }
bool selfplay_playing() { return synth_playing; }

void selfplay_render(int16_t* stereo_out, int16_t* mono_out, size_t frames) {
  if (!synth_ready) selfplay_init();
  const float two_pi = 2.0f * PI;

  for (size_t i = 0; i < frames; ++i) {
    if (!synth_playing) {
      mono_out[i] = 0;
      stereo_out[2 * i] = 0;
      stereo_out[2 * i + 1] = 0;
      continue;
    }
    if (synth_pos >= loop_samples) synth_pos = 0;
    const int pos = synth_pos;

    int ni = 0;
    while (ni < kNoteCount - 1 && pos >= note_start[ni + 1]) ++ni;
    if (ni != last_note) {
      last_note = ni;
      melody_phase = 0.0f;
      melody_env = 1.0f;
    }
    const int t_n = pos - note_start[ni];
    const int dur_n = note_start[ni + 1] - note_start[ni];
    melody_env *= melody_decay;
    melody_phase += two_pi * kMelody[ni].freq / kSampleRate;
    if (melody_phase >= two_pi) melody_phase -= two_pi;

    float mel = melody_env;
    if (t_n < 128) mel *= t_n / 128.0f;
    const int rel = dur_n - t_n;
    if (rel < 320) mel *= (rel > 0 ? rel : 0) / 320.0f;
    const float mel_wave = fast_sin(melody_phase) + 0.28f * fast_sin(2.0f * melody_phase);

    int bi = pos / bar_samples;
    if (bi > 3) bi = 3;
    if (bi != last_bar) {
      last_bar = bi;
      bass_phase = 0.0f;
      bass_env = 1.0f;
    }
    bass_env *= bass_decay;
    bass_phase += two_pi * kBassFreq[bi] / kSampleRate;
    if (bass_phase >= two_pi) bass_phase -= two_pi;
    const float bass_wave = fast_sin(bass_phase) + 0.30f * fast_sin(2.0f * bass_phase);

    const int off = pos - bi * bar_samples;
    const int half_bar = bar_samples / 2;
    const int kick_idx = bi * kKicksPerBar + (off >= half_bar ? 1 : 0);
    if (kick_idx != last_kick) {
      last_kick = kick_idx;
      kick_phase = 0.0f;
      kick_env = 1.0f;
    }
    const int t_k = off - (off >= half_bar ? half_bar : 0);
    kick_env *= kick_decay;
    if (t_k < kick_len) {
      const float sweep = 45.0f + 85.0f * expf(-t_k / (0.04f * kSampleRate));
      kick_phase += two_pi * sweep / kSampleRate;
      if (kick_phase >= two_pi) kick_phase -= two_pi;
    }

    const int hat_idx = pos / hat_period;
    if (hat_idx != last_hat) {
      last_hat = hat_idx;
      hat_env = 1.0f;
    }
    hat_env *= hat_decay;
    float hat = 0.0f;
    if (pos - hat_idx * hat_period < hat_len) {
      const int32_t n = next_noise();
      hat = static_cast<float>(n - noise_prev) * (1.0f / 65536.0f) * hat_env;
      noise_prev = n;
    }

    float s = 0.30f * mel * mel_wave +
              0.34f * bass_env * bass_wave +
              0.62f * kick_env * fast_sin(kick_phase) +
              0.22f * hat;
    if (s > 0.98f) s = 0.98f;
    if (s < -0.98f) s = -0.98f;

    const int16_t v = static_cast<int16_t>(s * 32767.0f);
    mono_out[i] = v;
    stereo_out[2 * i] = v;
    stereo_out[2 * i + 1] = v;
    ++synth_pos;
  }

  process_block(mono_out);
}

}  // namespace fountain

#endif  // FOUNTAIN || PET_FOUNTAIN
