#pragma once

// 音乐电子喷泉显示模块（供语音固件与独立演示共用）
//
// 只在定义了 FOUNTAIN（独立演示）或 PET_FOUNTAIN（语音固件）时参与编译。
// 模块只负责「频谱 → ST7789 彩色竖条」，不含 I2S / codec 逻辑，谁用谁喂 PCM。

#include <stddef.h>
#include <stdint.h>

namespace fountain {

// 初始化屏幕（幂等）。成功返回 true。
bool begin();
bool ready();

// 开/关喷泉显示。开时点亮背光（GPIO46 与功放使能共用），关时清屏并熄背光。
void set_active(bool on);
bool active();

// 右上角小标记，例如 "MIC" / "PLAY"
void set_label(const char* text);

// 喂入麦克风数据：立体声交织（每帧 2 个 int16），取左声道
void feed_stereo(const int16_t* interleaved, size_t frames);
// 喂入单声道数据
void feed_mono(const int16_t* mono, size_t frames);

void reset_peaks();
void dump_levels();

// ---- 内置旋律自播放 ----
void selfplay_init();
// 生成 frames 帧（写入 stereo_out 交织缓冲与 mono_out），并用同一段数据渲染
void selfplay_render(int16_t* stereo_out, int16_t* mono_out, size_t frames);
void selfplay_set_playing(bool playing);
bool selfplay_playing();

}  // namespace fountain
