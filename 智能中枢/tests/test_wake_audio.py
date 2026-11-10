import io
import struct
import unittest
import wave

from agent_gateway.wake_audio import loudest_span, prepare_wake_wav, wav_peak


def make_wav(segments, sample_rate=16000, channels=1):
    """segments: [(振幅, 秒数), ...]，振幅 0 就是静音。"""
    frames = []
    for amplitude, seconds in segments:
        for index in range(int(sample_rate * seconds)):
            value = amplitude if index % 2 == 0 else -amplitude
            frames.append(value)
    pcm = struct.pack("<%dh" % len(frames), *frames)
    if channels == 2:
        doubled = []
        for value in frames:
            doubled.extend((value, value))
        pcm = struct.pack("<%dh" % len(doubled), *doubled)
    buffer = io.BytesIO()
    writer = wave.open(buffer, "wb")
    writer.setnchannels(channels)
    writer.setsampwidth(2)
    writer.setframerate(sample_rate)
    writer.writeframes(pcm)
    writer.close()
    return buffer.getvalue()


def read_wav(data):
    reader = wave.open(io.BytesIO(data), "rb")
    try:
        frames = reader.getnframes()
        pcm = reader.readframes(frames)
        rate = reader.getframerate()
    finally:
        reader.close()
    samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    return rate, samples


class WakeAudioTests(unittest.TestCase):
    def test_wav_peak_reads_amplitude(self):
        data = make_wav([(0, 0.2), (3000, 0.3)])
        self.assertEqual(wav_peak(data), 3000)

    def test_wav_peak_on_garbage_returns_zero(self):
        self.assertEqual(wav_peak(b"not a wav"), 0)
        self.assertEqual(wav_peak(b""), 0)

    def test_prepare_trims_leading_and_trailing_silence(self):
        # 0.6s 静音 + 0.4s 语音 + 0.6s 静音
        data = make_wav([(0, 0.6), (3000, 0.4), (0, 0.6)])
        prepared = prepare_wake_wav(data)
        self.assertIsNotNone(prepared)
        rate, samples = read_wav(prepared)
        self.assertEqual(rate, 16000)
        # 只留 0.4s 语音 + 前 0.2s / 后 0.3s 气口（窗口对齐会有半窗误差）
        self.assertLess(len(samples) / rate, 1.1)
        self.assertGreater(len(samples) / rate, 0.6)
        # 语音本身没被剪掉
        self.assertGreater(max(abs(value) for value in samples), 20000)

    def test_prepare_normalizes_peak(self):
        # 说话音量偏小（peak 900）时要拉到 0.7 满量程附近，否则 ASR 听不清
        data = make_wav([(0, 0.3), (900, 0.5), (0, 0.3)])
        prepared = prepare_wake_wav(data)
        self.assertIsNotNone(prepared)
        _, samples = read_wav(prepared)
        peak = max(abs(value) for value in samples)
        self.assertGreater(peak, 20000)
        self.assertLessEqual(peak, 32767)

    def test_prepare_normalizes_loud_audio_without_clipping(self):
        # 本来就很大声的录音统一拉到 0.7 满量程：ASR 对输入电平的一致性更敏感，
        # 但只要不削波就不会丢字。
        data = make_wav([(0, 0.3), (30000, 0.5), (0, 0.3)])
        prepared = prepare_wake_wav(data)
        self.assertIsNotNone(prepared)
        _, samples = read_wav(prepared)
        peak = max(abs(value) for value in samples)
        self.assertEqual(peak, int(0.7 * 32767))
        self.assertLess(peak, 32767)

    def test_prepare_keeps_only_loudest_cluster(self):
        # 弱的一段（1200，可能是远处人声）+ 间隔 + 强的一段（5000，用户凑近喊）
        data = make_wav([(0, 0.3), (1200, 0.3), (0, 0.5), (5000, 0.4), (0, 0.3)])
        prepared = prepare_wake_wav(data)
        self.assertIsNotNone(prepared)
        rate, samples = read_wav(prepared)
        # 只保留强的那一簇（0.4s + 气口），弱段不该被框进来
        self.assertLess(len(samples) / rate, 1.2)
        self.assertEqual(max(abs(value) for value in samples), int(0.7 * 32767))

    def test_loudest_span_returns_none_for_flat_audio(self):
        samples = tuple(100 if index % 2 else -100 for index in range(16000))
        self.assertIsNone(loudest_span(samples, 16000, crop_floor=200, crop_ratio=0.35))

    def test_prepare_returns_none_for_pure_silence(self):
        self.assertIsNone(prepare_wake_wav(make_wav([(0, 1.5)])))

    def test_prepare_returns_none_for_too_short_clip(self):
        self.assertIsNone(prepare_wake_wav(make_wav([(0, 0.1), (3000, 0.2)])))

    def test_prepare_returns_none_for_non_wav(self):
        self.assertIsNone(prepare_wake_wav(b"RIFF-not-really-a-wave-file"))

    def test_prepare_handles_stereo(self):
        data = make_wav([(0, 0.3), (2500, 0.4), (0, 0.3)], channels=2)
        prepared = prepare_wake_wav(data)
        self.assertIsNotNone(prepared)
        reader = wave.open(io.BytesIO(prepared), "rb")
        try:
            self.assertEqual(reader.getnchannels(), 2)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
