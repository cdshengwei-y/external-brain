import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from agent_gateway.local_neural_tts import (
    LocalNeuralSpeechProvider,
    LocalNeuralTtsError,
    _float_samples_to_wav,
    find_model_dir,
)


def wav_info(audio: bytes):
    with wave.open(io.BytesIO(audio), "rb") as source:
        return (
            source.getframerate(),
            source.getsampwidth(),
            source.getnchannels(),
            source.getnframes(),
        )


class FindModelDirTest(unittest.TestCase):
    def test_returns_none_when_nothing_installed(self):
        with patch.dict("os.environ", {"LOCAL_TTS_MODEL_ROOT": "/nonexistent-tts"}, clear=False):
            os_environ = {"LOCAL_TTS_MODEL_ROOT": "/nonexistent-tts"}
            with patch.dict("os.environ", os_environ, clear=False):
                self.assertIsNone(find_model_dir())

    def test_prefers_explicit_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(find_model_dir(directory), Path(directory))

    def test_ignores_explicit_path_that_is_not_a_directory(self):
        self.assertIsNone(find_model_dir("/definitely/not/here"))

    def test_scans_candidates_in_priority_order(self):
        with tempfile.TemporaryDirectory() as root:
            # 只有次优先的 Piper 模型存在时，应该选中它
            model_dir = Path(root) / "vits-piper-zh_CN-xiao_ya-medium"
            model_dir.mkdir()
            (model_dir / "zh_CN-xiao_ya-medium.onnx").write_bytes(b"onnx")
            with patch.dict("os.environ", {"LOCAL_TTS_MODEL_ROOT": root}, clear=False):
                found = find_model_dir()
            self.assertEqual(found.name, "vits-piper-zh_CN-xiao_ya-medium")

    def test_skips_directory_without_onnx(self):
        # 下载中断会留下没有 onnx 的空壳目录，不能因为它存在就选中，
        # 否则后面真正下好的模型永远用不上。
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "kokoro-int8-multi-lang-v1_1").mkdir()
            usable = Path(root) / "vits-piper-zh_CN-xiao_ya-medium"
            usable.mkdir()
            (usable / "model.onnx").write_bytes(b"onnx")
            with patch.dict("os.environ", {"LOCAL_TTS_MODEL_ROOT": root}, clear=False):
                found = find_model_dir()
            self.assertEqual(found.name, "vits-piper-zh_CN-xiao_ya-medium")


class FloatToWavTest(unittest.TestCase):
    def test_converts_float_samples_to_16bit_mono(self):
        audio = _float_samples_to_wav([0.0, 0.5, -0.5, 1.0], 24000)

        rate, width, channels, frames = wav_info(audio)
        self.assertEqual(rate, 24000)
        self.assertEqual(width, 2)
        self.assertEqual(channels, 1)
        self.assertEqual(frames, 4)

    def test_clips_out_of_range_values(self):
        # 模型偶尔会吐出略超 ±1 的值，不能让它溢出成爆音
        audio = _float_samples_to_wav([2.0, -2.0], 16000)

        with wave.open(io.BytesIO(audio), "rb") as source:
            import array

            samples = array.array("h")
            samples.frombytes(source.readframes(2))
        self.assertEqual(list(samples), [32767, -32767])


class ProviderWithoutModelTest(unittest.TestCase):
    def test_unavailable_when_model_missing(self):
        provider = LocalNeuralSpeechProvider(model_dir="/nonexistent-model-dir")

        self.assertFalse(provider.available)
        with self.assertRaises(LocalNeuralTtsError):
            provider.synthesize("你好")

    def test_empty_text_rejected(self):
        provider = LocalNeuralSpeechProvider(model_dir="/nonexistent-model-dir")

        with self.assertRaises(LocalNeuralTtsError):
            provider.synthesize("   ")

    def test_directory_without_onnx_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "tokens.txt").write_text("")
            provider = LocalNeuralSpeechProvider(model_dir=directory)

            self.assertFalse(provider.available)
            self.assertIn("onnx", provider.error or "")


class ProviderWithFakeEngineTest(unittest.TestCase):
    """用假的 sherpa-onnx 引擎验证「重采样到 16k」和说话人收敛。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        model_dir = Path(self.temp.name)
        (model_dir / "model.int8.onnx").write_bytes(b"fake")
        (model_dir / "voices.bin").write_bytes(b"fake")
        (model_dir / "tokens.txt").write_text("")
        self.model_dir = model_dir

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_synthesize_downmixes_to_16k_device_format(self):
        class FakeAudio:
            samples = [0.0] * 24000  # 1 秒 @ 24 kHz
            sample_rate = 24000

        class FakeEngine:
            num_speakers = 5

            def generate(self, text, sid=0, speed=1.0):
                self.last_sid = sid
                return FakeAudio()

        provider = LocalNeuralSpeechProvider(model_dir=str(self.model_dir), sid=99)
        provider._tts = FakeEngine()  # 跳过真实模型加载

        audio, audio_format, mime = provider.synthesize("测试")

        rate, width, channels, _frames = wav_info(audio)
        self.assertEqual((rate, width, channels), (16000, 2, 1))
        self.assertEqual(audio_format, "wav")
        self.assertEqual(mime, "audio/wav")
        # 传进来的 sid 超界，要收敛到 num_speakers-1
        self.assertEqual(provider._tts.last_sid, 4)

    def test_empty_engine_output_is_rejected(self):
        class FakeAudio:
            samples = []
            sample_rate = 24000

        class FakeEngine:
            num_speakers = 1

            def generate(self, text, sid=0, speed=1.0):
                return FakeAudio()

        provider = LocalNeuralSpeechProvider(model_dir=str(self.model_dir))
        provider._tts = FakeEngine()

        with self.assertRaises(LocalNeuralTtsError):
            provider.synthesize("测试")


if __name__ == "__main__":
    unittest.main()
