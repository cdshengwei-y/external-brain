#pragma once

// 网络音乐播放器（语音固件专用，随 PET_FOUNTAIN 一起编译）
//
// 职责：
//   1. 从中枢拉 /api/v1/portable/v1/music/<id>/stream（16k 单声道 WAV），
//      用 I2S 全双工送进 ES8311 DAC 播放；
//   2. 同一段 PCM 直接喂给喷泉显示 —— 画面和声音逐样本同步，零延迟；
//   3. 播放期间持续监听麦克风，检测到「有人说话」就压低音乐、录一小段，
//      交给上层（audio_capture）上传 ASR，拿到控制指令后执行。
//
// 这块板没有 AEC，喇叭声音会串进麦克风，所以打断靠「自动压低 + 拉高门限」：
// 说话时先把音乐压到几乎无声，识别完再恢复。

#include <stddef.h>
#include <stdint.h>

namespace music_player {

// 上层识别回来的控制指令。字段指向静态存储，调用后仍然有效。
struct ControlCommand {
  const char* action;       // music_pause / music_resume / music_replay /
                            // music_next / music_stop / nullptr
  const char* stream_path;  // replay / next 时的新流路径（相对中枢地址）
  const char* title;        // 新曲名，可为空
};

// 检测到说话时回调：传入 16k 单声道 PCM，返回要执行的动作。
using ControlHandler = ControlCommand (*)(const int16_t* pcm, size_t frames);

void set_control_handler(ControlHandler handler);

// 阻塞播放，直到播完 / 用户说停 / 出错。返回是否正常播完。
bool play(const char* stream_path, const char* title);

// 串口 / 语音可用：切换音量档位、请求停止
void cycle_volume();
void step_volume(int delta);  // +1 更响，-1 更轻
int volume_step_value();      // 当前档位 0..3，供上报给中枢
void request_stop();
bool active();

}  // namespace music_player
