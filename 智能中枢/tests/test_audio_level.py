"""录音电平诊断的单元测试。

2026-11-06 排查「说了没反应」时，设备上传的 6 秒录音里人声只占 1 秒、峰值 -29 dBFS，
Qwen ASR 在低信噪比下幻听出「朝鲜族女性。」这类无关句子，再被语音门控当成环境噪声丢弃。
日志里只有一句 ``[GATE] 忽略环境语音``，看不出根因在上游录音，所以把电平检测加进来。
"""

from __future__ import annotations

import io
import math
import struct
import unittest
import wave

from agent_gateway.portable import (
    SILENT_RMS_DBFS,
    audio_silence_note,
    describe_audio_level,
)


def _wav(samples, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def _tone(peak: int, seconds: float = 1.0, rate: int = 16000):
    total = int(seconds * rate)
    return [
        int(peak * math.sin(2 * math.pi * 220 * index / rate))
        for index in range(total)
    ]


class AudioLevelTest(unittest.TestCase):
    def test_normal_speech_level_is_silent(self) -> None:
        # 正常说话峰值接近满量程，不该打日志
        self.assertIsNone(describe_audio_level(_wav(_tone(20000))))

    def test_quiet_recording_is_reported(self) -> None:
        note = describe_audio_level(_wav(_tone(1121)))
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("录音电平偏低", note)
        self.assertIn("peak=-29.3dBFS", note)

    def test_fully_silent_recording_is_reported(self) -> None:
        note = describe_audio_level(_wav([0] * 16000))
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("全静音", note)

    def test_threshold_boundary(self) -> None:
        # -20 dBFS 约等于峰值 3277，刚好在阈值上的应放行
        self.assertIsNone(describe_audio_level(_wav(_tone(3400))))
        self.assertIsNotNone(describe_audio_level(_wav(_tone(3100))))

    def test_garbage_input_is_ignored(self) -> None:
        self.assertIsNone(describe_audio_level(b"not a wav at all"))

    def test_empty_input_is_ignored(self) -> None:
        self.assertIsNone(describe_audio_level(b""))


class AudioSilenceTest(unittest.TestCase):
    """只有底噪的录音不送 ASR。

    设备没有唤醒词，VAD 门限低，咳嗽/翻身都会触发上传。实测这些录音
    ASR 不会返回空，而是「嗯。」「咳。」或直接幻听。送进去既花 0.7 秒
    和一次调用，又给语音门控制造假信号。
    """

    def test_speech_is_not_silent(self) -> None:
        self.assertIsNone(audio_silence_note(_wav(_tone(20000))))
        # 正常说话 RMS 在 -14 ~ -25 dBFS，对应正弦峰值 5000 上下
        self.assertIsNone(audio_silence_note(_wav(_tone(3000))))

    def test_noise_floor_is_skipped(self) -> None:
        # 峰值归一化后底噪也能到 -3 dBFS，但整体 RMS 只有 -39 dBFS 左右
        note = audio_silence_note(_wav(_tone(500)))
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("只有底噪", note)

    def test_threshold_boundary(self) -> None:
        # 正弦的 RMS 比峰值低 3 dB：阈值 -31 dBFS 对应峰值约 930
        threshold_peak = int(32768 * 10 ** ((SILENT_RMS_DBFS + 3.0) / 20))
        self.assertIsNone(audio_silence_note(_wav(_tone(threshold_peak + 200))))
        self.assertIsNotNone(audio_silence_note(_wav(_tone(threshold_peak - 200))))

    def test_absolute_silence_is_skipped(self) -> None:
        note = audio_silence_note(_wav([0] * 16000))
        self.assertIsNotNone(note)

    def test_garbage_and_empty_are_ignored(self) -> None:
        self.assertIsNone(audio_silence_note(b"not a wav at all"))
        self.assertIsNone(audio_silence_note(b""))


class RepeatRateTest(unittest.TestCase):
    """零填充开头不该被当成「采样重复 → 时钟不匹配」。

    2026-11-06 第四轮实测：设备上传的 0.70 秒唤醒片段前面有 0.173 秒全 0
    （采集刚起时缓冲区是零填充），重复率算到 25.8%，日志于是喊
    「ES8311 时钟不匹配，请检查 pre_div」—— 而同一批其余 5 条录音都只有
    1.1%~2.8%。假的硬件告警比没有告警更贵：会把排查引到错误的寄存器上。
    """

    def test_zero_padding_prefix_is_not_a_clock_problem(self) -> None:
        samples = [0] * 2762 + _tone(12000, 0.5)
        note = describe_audio_level(_wav(samples))
        self.assertIsNone(note, note)

    def test_real_zero_order_hold_still_reported(self) -> None:
        # pre_div 多除了 3：ADC 只出 5.33 kHz，I2S 按 16 kHz 读走，每个采样重复 3 次
        held = [value for value in _tone(12000, 1.0) for _ in range(3)]
        note = describe_audio_level(_wav(held))
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("采样重复", note)


if __name__ == "__main__":
    unittest.main()