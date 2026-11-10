"""设备影子的单元测试。

覆盖三件最容易出错的事：

1. 问「现在音量多少」不能被当成「调大音量」；
2. 回答里的数字必须来自 DeviceRegistry，不能让模型自由发挥；
3. 说话音量要真的作用在音频上（``scale_wav_pcm``），而不是只改一个字段。
"""

from __future__ import annotations

import array
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from agent_gateway.device import (
    VOLUME_GAINS,
    DeviceRegistry,
    clamp_volume_step,
    scale_wav_pcm,
    wav_peak,
)
from agent_gateway.intent import IntentResult, IntentRouter, PluginContext
from agent_gateway.portable import (
    DevicePlugin,
    _device_volume_slots,
    _is_volume_question,
    _requested_volume_step,
)


def make_wav(samples: int = 1600, amplitude: int = 10000, rate: int = 16000) -> bytes:
    """造一段 16k 单声道 16bit 的静音/方波，够测增益。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array.array("h", [amplitude] * samples).tobytes())
    return buffer.getvalue()


def wav_pcm(data: bytes) -> array.array:
    """取出 data chunk 里的样本，从 chunk 头找，别硬编码 44 字节。"""
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if chunk_id == b"data":
            out = array.array("h")
            out.frombytes(data[pos + 8 : pos + 8 + size])
            return out
        pos += 8 + size + (size & 1)
    raise AssertionError("没有 data chunk")


class ClampTest(unittest.TestCase):
    def test_clamps_into_range(self) -> None:
        self.assertEqual(clamp_volume_step(-3), 0)
        self.assertEqual(clamp_volume_step(99), len(VOLUME_GAINS) - 1)
        self.assertEqual(clamp_volume_step("3"), 3)
        self.assertEqual(clamp_volume_step("三", default=1), 1)  # 非数字走默认值

    def test_bad_value_falls_back_to_default(self) -> None:
        self.assertEqual(clamp_volume_step(None), 3)
        self.assertEqual(clamp_volume_step("很大声", default=2), 2)


class ScaleWavTest(unittest.TestCase):
    def test_halves_amplitude_and_keeps_header(self) -> None:
        original = make_wav(amplitude=10000)
        scaled = scale_wav_pcm(original, 0.5)
        self.assertEqual(len(scaled), len(original))
        self.assertEqual(scaled[:44], original[:44])
        self.assertEqual(wav_pcm(scaled)[0], 5000)

    def test_unit_gain_returns_identical_bytes(self) -> None:
        original = make_wav()
        self.assertIs(scale_wav_pcm(original, 1.0), original)

    def test_non_wav_is_returned_untouched(self) -> None:
        payload = b"not a wav file at all"
        self.assertIs(scale_wav_pcm(payload, 0.5), payload)

    def test_clips_instead_of_wrapping(self) -> None:
        original = make_wav(amplitude=30000)
        scaled = wav_pcm(scale_wav_pcm(original, 1.0))
        self.assertEqual(scaled[0], 30000)
        # 即使 gain 略大于 1 也不该出现负值（那是溢出回绕）
        boosted = wav_pcm(scale_wav_pcm(original, 2.0))
        self.assertEqual(boosted[0], 30000)


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "devices.json"
        self.registry = DeviceRegistry(self.path)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_default_step(self) -> None:
        profile = self.registry.get("wearable-1")
        self.assertEqual(profile.volume_step, 3)
        self.assertIn("说话音量 4/5", profile.summary())

    def test_persists_across_instances(self) -> None:
        self.registry.set_volume("wearable-1", 1)
        reloaded = DeviceRegistry(self.path)
        self.assertEqual(reloaded.get("wearable-1").volume_step, 1)

    def test_device_report_does_not_override_volume(self) -> None:
        self.registry.set_volume("wearable-1", 0)
        self.registry.update_from_state(
            "wearable-1",
            {"volume_step": 4, "music_step": 2, "screen": "off", "fountain": True},
        )
        profile = self.registry.get("wearable-1")
        # 说话音量以中枢为准，设备上报只能改音乐档位等
        self.assertEqual(profile.volume_step, 0)
        self.assertEqual(profile.music_step, 2)
        self.assertEqual(profile.screen, "off")
        self.assertTrue(profile.fountain)

    def test_music_step_clamped(self) -> None:
        self.registry.update_from_state("wearable-1", {"music_step": 99})
        self.assertEqual(self.registry.get("wearable-1").music_step, 3)

    def test_apply_volume_uses_current_step(self) -> None:
        wav = make_wav(amplitude=10000)
        self.registry.set_volume("wearable-1", 1)  # gain 0.5
        self.assertEqual(wav_pcm(self.registry.apply_volume("wearable-1", wav))[0], 5000)
        self.registry.set_volume("wearable-1", len(VOLUME_GAINS) - 1)  # gain 1.0
        self.assertEqual(wav_pcm(self.registry.apply_volume("wearable-1", wav))[0], 10000)


class VolumeSlotsTest(unittest.TestCase):
    def test_question_is_not_a_command(self) -> None:
        for text in ("现在音量多少", "音量调到几档了", "声音多大"):
            self.assertIsNone(_device_volume_slots(text), text)

    def test_question_detected_for_local_answer(self) -> None:
        # 问句要走本地快通道回答，不能排进模型队列
        for text in ("现在音量多少", "音量调到几档了", "声音多大"):
            self.assertTrue(_is_volume_question(text), text)
        self.assertFalse(_is_volume_question("大点声"))
        self.assertFalse(_is_volume_question("今天天气不错"))

    def test_chitchat_is_not_a_command(self) -> None:
        for text in ("你声音好听", "今天天气不错", "给我讲个笑话"):
            self.assertIsNone(_device_volume_slots(text), text)

    def test_absolute_target(self) -> None:
        self.assertEqual(
            _device_volume_slots("音量调到4"), {"capability": "volume", "value": 4}
        )
        self.assertEqual(
            _device_volume_slots("声音调到三档"), {"capability": "volume", "value": 3}
        )

    def test_relative_direction(self) -> None:
        self.assertEqual(_device_volume_slots("大点声"), {"capability": "volume", "delta": 1})
        self.assertEqual(_device_volume_slots("声音小一点"), {"capability": "volume", "delta": -1})
        self.assertEqual(_device_volume_slots("太吵了"), {"capability": "volume", "delta": -1})

    def test_max_min(self) -> None:
        self.assertEqual(
            _device_volume_slots("音量调到最大"),
            {"capability": "volume", "value": len(VOLUME_GAINS)},
        )
        self.assertEqual(_device_volume_slots("音量最小"), {"capability": "volume", "value": 1})

    def test_requested_step_from_slots(self) -> None:
        self.assertEqual(_requested_volume_step({"value": 5}, "", 0), 4)
        self.assertEqual(_requested_volume_step({"delta": 1}, "", 2), 3)
        self.assertEqual(_requested_volume_step({}, "声音小一点", 2), 1)
        # 越界收敛到边界而不是崩
        self.assertEqual(_requested_volume_step({"delta": -5}, "", 1), 0)


class DevicePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.registry = DeviceRegistry(Path(self._dir.name) / "devices.json")
        self.plugin = DevicePlugin(self.registry)
        self.ctx = PluginContext(text="", request_id="r1", device_id="wearable-1")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _handle(self, intent: str, slots=None, text: str = "") -> IntentResult:
        self.ctx.text = text
        return self.plugin.handle(
            IntentResult(intent=intent, slots=dict(slots or {}), source="test"),
            self.ctx,
        )

    def test_query_uses_registry_value(self) -> None:
        self.registry.set_volume("wearable-1", 1)
        result = self._handle("device_query")
        self.assertIn("2 档", result.reply)
        self.assertIn("一共 5 档", result.reply)
        self.assertEqual(result.session_action, "continue")

    def test_query_ignores_model_reply(self) -> None:
        # 模型可能瞎写「现在是最大声」，查询结果必须用真实值覆盖
        self.registry.set_volume("wearable-1", 0)
        self.ctx.text = "现在音量多少"
        result = self.plugin.handle(
            IntentResult(intent="device_query", reply="现在是最大声", source="test"),
            self.ctx,
        )
        self.assertIn("1 档", result.reply)

    def test_query_mentions_music_when_asked(self) -> None:
        self.registry.update_from_state("wearable-1", {"music_step": 2, "music": "playing"})
        result = self._handle("device_query", text="音乐音量多少")
        self.assertIn("音乐音量", result.reply)
        self.assertIn("分贝", result.reply)

    def test_set_by_value(self) -> None:
        result = self._handle("device_set", {"capability": "volume", "value": 5})
        self.assertEqual(self.registry.get("wearable-1").volume_step, 4)
        self.assertIn("5 档", result.reply)
        self.assertEqual(result.device_action["type"], "volume_set")
        self.assertAlmostEqual(result.device_action["gain"], 1.0)

    def test_set_by_delta(self) -> None:
        self.registry.set_volume("wearable-1", 1)
        self._handle("device_set", {"capability": "volume", "delta": 1})
        self.assertEqual(self.registry.get("wearable-1").volume_step, 2)

    def test_set_without_numbers_uses_text(self) -> None:
        self.registry.set_volume("wearable-1", 3)
        self._handle("device_set", {"capability": "volume"}, text="声音小一点")
        self.assertEqual(self.registry.get("wearable-1").volume_step, 2)

    def test_set_reports_boundary(self) -> None:
        self.registry.set_volume("wearable-1", len(VOLUME_GAINS) - 1)
        result = self._handle("device_set", {"capability": "volume", "delta": 1})
        self.assertIn("最大", result.reply)
        self.assertEqual(self.registry.get("wearable-1").volume_step, len(VOLUME_GAINS) - 1)

    def test_unknown_capability_is_refused(self) -> None:
        result = self._handle("device_set", {"capability": "brightness", "value": 2})
        self.assertIn("改不了", result.reply)
        self.assertIsNone(result.device_action)


class DeviceContextInjectionTest(unittest.TestCase):
    def test_context_reaches_classifier(self) -> None:
        seen: list[str] = []

        class RecordingClassifier:
            def classify(self, text, catalog, device_context=""):
                seen.append(device_context)
                return IntentResult(intent="chat", reply="好", source="test")

        router = IntentRouter([DevicePlugin(DeviceRegistry(Path(tempfile.mkdtemp()) / "d.json"))],
                              RecordingClassifier())
        ctx = PluginContext(text="现在音量多少", request_id="r", device_context="说话音量 3/5")
        router.resolve("现在音量多少", ctx)
        self.assertEqual(seen, ["说话音量 3/5"])

    def test_legacy_two_arg_classifier_still_works(self) -> None:
        class LegacyClassifier:
            def classify(self, text, catalog):
                return IntentResult(intent="chat", reply="好", source="test")

        router = IntentRouter([], LegacyClassifier())
        ctx = PluginContext(text="你好", request_id="r", device_context="说话音量 3/5")
        result = router.resolve("你好", ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, "chat")


class DefaultVolumeTest(unittest.TestCase):
    """出厂默认档位和「增益真的作用到 PCM 上」。"""

    def test_env_default_is_zero_based(self) -> None:
        from agent_gateway import device as device_module

        with mock.patch.dict(os.environ, {"PET_DEFAULT_VOLUME": "2"}):
            self.assertEqual(device_module._default_step_from_env(3), 1)
        with mock.patch.dict(os.environ, {"PET_DEFAULT_VOLUME": "9"}):
            self.assertEqual(device_module._default_step_from_env(3), 3)
        with mock.patch.dict(os.environ, {"PET_DEFAULT_VOLUME": ""}):
            self.assertEqual(device_module._default_step_from_env(3), 3)

    def test_apply_volume_scales_and_skips_at_max(self) -> None:
        registry = DeviceRegistry(Path(tempfile.mkdtemp()) / "devices.json")
        wav = make_wav(amplitude=20000)
        registry.set_volume("dev", 1)  # 用户视角 2 档 = 增益 0.5
        scaled = registry.apply_volume("dev", wav)
        self.assertEqual(len(scaled), len(wav))
        self.assertAlmostEqual(wav_peak(scaled) / wav_peak(wav), 0.5, places=2)
        registry.set_volume("dev", len(VOLUME_GAINS) - 1)  # 5 档不缩放
        self.assertIs(registry.apply_volume("dev", wav), wav)


if __name__ == "__main__":
    unittest.main()
