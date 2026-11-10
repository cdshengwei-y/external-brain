#pragma once

// 桌宠表情首页（待机 / 唤醒 / 倾听 / 思考 / 说话 / 开心 / 睡意）
//
// 只往 ST7789 上画「一张脸」，不碰音频、不碰网络。语音链路在状态切换时
// 调用 set_expression()，在采集 / 播放循环里周期性调用 tick() 推动动画。
//
// 与音乐喷泉共用同一块屏：喷泉要占屏时先 suspend()，回来后 resume()。

namespace face {

enum Expression {
  kIdle = 0,      // 待机首页：缓慢呼吸 + 偶尔眨眼 + 瞳孔漂移
  kListening,     // 正在听你说话：眼睛睁大、瞳孔放大
  kThinking,      // 上传 / 等中枢回复：眼睛向上看 + 三个点
  kSpeaking,      // 播放 TTS：嘴巴跟着音量开合
  kHappy,         // 唤醒成功：^ ^ 眼 + 大笑
  kSleepy,        // 刚退下 / 待命：闭眼 + z z
};

// 初始化屏幕并显示待机表情（幂等）。返回是否可用。
bool begin();

// 屏幕是否已初始化
bool ready();

// 切换表情（立即重绘）
void set_expression(Expression expression);
Expression expression();

// 推进动画并绘制（内部有脏检测 + 节流，可高频调用）
void tick();

// 播放 TTS 时喂入当前音量（0.0 ~ 1.0），驱动口型
void feed_amp(float level);
void reset_amp();

// 喷泉占用屏幕时挂起表情；恢复后重新点亮背光并重绘
void suspend();
void resume();
bool suspended();
bool visible();

}  // namespace face
