import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import wave
from unittest.mock import Mock, patch

from agent_gateway.omni_voice_tts import (
    DEFAULT_VOICE,
    FallbackSpeechProvider,
    OmniVoiceError,
    OmniVoiceSpeechProvider,
    normalize_speed,
    normalize_voice,
)


def build_wav(
    seconds: float = 1.0,
    rate: int = 24000,
    channels: int = 1,
    width: int = 2,
) -> bytes:
    """造一段和 OmniVoice 实返一致的 WAV（24 kHz / 16 bit / 单声道）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(b"\x00" * int(rate * seconds * channels * width))
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "audio/wav") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def capture_rate(audio: bytes) -> int:
    with wave.open(io.BytesIO(audio), "rb") as source:
        return source.getframerate()


class OmniVoiceSpeechProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OmniVoiceSpeechProvider(
            api_key="rc-test",
            voice="female",
            timeout_seconds=5,
        )

    def test_synthesize_returns_16k_wav_for_device(self):
        remote = build_wav(seconds=1.0, rate=24000)

        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(remote),
        ) as urlopen:
            audio, audio_format, mime = self.provider.synthesize("你好，贾维斯。")

        self.assertEqual(audio_format, "wav")
        self.assertEqual(mime, "audio/wav")
        # 云端 24 kHz，设备端只认 16 kHz：必须重采样
        self.assertEqual(capture_rate(audio), 16000)
        self.assertGreater(len(audio), 0)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "OmniVoice")
        self.assertEqual(payload["input"], "你好，贾维斯。")
        self.assertEqual(payload["voice"], "female")
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(
            request.headers["Authorization"],
            "Bearer rc-test",
        )

    def test_request_hits_audio_speech_endpoint(self):
        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(build_wav()),
        ) as urlopen:
            self.provider.synthesize("测试")

        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "https://developer.amd.com.cn/radeon/api/v1/audio/speech",
        )

    def test_speed_is_clamped_and_optional(self):
        provider = OmniVoiceSpeechProvider(api_key="rc-test", speed=9)

        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(build_wav()),
        ) as urlopen:
            provider.synthesize("测试")

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["speed"], 2.0)

    def test_speed_omitted_when_not_configured(self):
        provider = OmniVoiceSpeechProvider(api_key="rc-test", speed=None)

        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(build_wav()),
        ) as urlopen:
            provider.synthesize("测试")

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("speed", payload)

    def test_json_body_with_200_is_treated_as_error(self):
        body = json.dumps(
            {"error": {"message": "Unsupported voice 'alloy'"}}
        ).encode("utf-8")

        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(body, "application/json"),
        ):
            with self.assertRaises(OmniVoiceError) as raised:
                self.provider.synthesize("测试")

        self.assertIn("Unsupported voice", str(raised.exception))

    def test_empty_text_is_rejected_without_request(self):
        with patch("agent_gateway.omni_voice_tts.urllib.request.urlopen") as urlopen:
            with self.assertRaises(OmniVoiceError):
                self.provider.synthesize("   ")

        urlopen.assert_not_called()

    def test_missing_api_key_reports_unavailable(self):
        provider = OmniVoiceSpeechProvider(api_key="")
        provider.api_key = ""

        self.assertFalse(provider.available)
        with self.assertRaises(OmniVoiceError):
            provider.synthesize("测试")

    def test_env_api_key_enables_provider(self):
        with patch.dict("os.environ", {"AMD_RADEON_API_KEY": "rc-from-env"}, clear=False):
            provider = OmniVoiceSpeechProvider()

        self.assertTrue(provider.available)
        self.assertEqual(provider.api_key, "rc-from-env")


class CachingAndSerializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OmniVoiceSpeechProvider(api_key="rc-test", timeout_seconds=5)

    def test_repeated_text_hits_cache_without_network(self):
        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(build_wav()),
        ) as urlopen:
            first = self.provider.synthesize("我在")
            second = self.provider.synthesize("我在")

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first[0], second[0])

    def test_warmup_fills_cache_so_first_utterance_is_instant(self):
        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(build_wav()),
        ) as urlopen:
            warmed = self.provider.warmup(["我在", "好的。"])
            audio, _fmt, _mime = self.provider.synthesize("我在")

        self.assertEqual(warmed, 2)
        self.assertEqual(urlopen.call_count, 2)  # 预热两次，正式那次全走缓存
        self.assertGreater(len(audio), 0)

    def test_warmup_swallows_failures(self):
        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            side_effect=urllib_error(),
        ):
            self.assertEqual(self.provider.warmup(["我在"]), 0)

    def test_requests_are_serialized_under_concurrency(self):
        """平台串行排队，两路并发会被放大到 26 秒，所以必须排队发。"""
        in_flight = []
        peak = []

        def slow_urlopen(*_args, **_kwargs):
            in_flight.append(1)
            peak.append(len(in_flight))
            time.sleep(0.05)
            in_flight.pop()
            return FakeResponse(build_wav())

        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            side_effect=slow_urlopen,
        ):
            threads = [
                threading.Thread(target=self.provider.synthesize, args=(f"第{i}句",))
                for i in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(max(peak), 1, "并发请求没有被串行化")

    def test_cache_does_not_block_on_request_lock(self):
        """缓存命中不能进请求锁，否则预热过的话术会被正在合成的话堵住。"""
        with patch(
            "agent_gateway.omni_voice_tts.urllib.request.urlopen",
            return_value=FakeResponse(build_wav()),
        ):
            self.provider.warmup(["我在"])

        with self.provider._request_lock:  # 独占锁，模拟另一条请求还在合成
            started = time.perf_counter()
            self.provider.synthesize("我在")
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.5)


def urllib_error():
    return urllib.error.URLError("boom")


class FallbackSpeechProviderTest(unittest.TestCase):
    def test_primary_result_is_used_when_cloud_works(self):
        primary = Mock()
        primary.synthesize.return_value = (b"cloud", "wav", "audio/wav")
        fallback = Mock()

        provider = FallbackSpeechProvider(primary, fallback)

        self.assertEqual(provider.synthesize("你好")[0], b"cloud")
        fallback.synthesize.assert_not_called()
        self.assertEqual(provider.fallback_count, 0)

    def test_falls_back_to_local_when_cloud_raises(self):
        primary = Mock()
        primary.synthesize.side_effect = OmniVoiceError("timeout")
        fallback = Mock()
        fallback.synthesize.return_value = (b"local", "wav", "audio/wav")

        provider = FallbackSpeechProvider(primary, fallback)

        self.assertEqual(provider.synthesize("你好")[0], b"local")
        self.assertEqual(provider.fallback_count, 1)

    def test_raises_when_no_fallback_configured(self):
        primary = Mock()
        primary.synthesize.side_effect = OmniVoiceError("timeout")

        provider = FallbackSpeechProvider(primary, None)

        with self.assertRaises(OmniVoiceError):
            provider.synthesize("你好")


class VoiceNormalizationTest(unittest.TestCase):
    def test_platform_voice_names_pass_through(self):
        self.assertEqual(normalize_voice("male"), "male")
        self.assertEqual(normalize_voice("FEMALE-BRITISH"), "female-british")

    def test_local_and_openai_voice_names_fall_back_to_female(self):
        # 系统语音名和 OpenAI 音色名云端都不认，必须收敛掉，否则 400
        for value in ("Tingting", "alloy", "zh-CN-XiaoxiaoNeural", "", None):
            self.assertEqual(normalize_voice(value), DEFAULT_VOICE)

    def test_speed_parsing(self):
        self.assertEqual(normalize_speed("1.2"), 1.2)
        self.assertEqual(normalize_speed(0.1), 0.5)
        self.assertIsNone(normalize_speed(None))
        self.assertIsNone(normalize_speed("fast"))


if __name__ == "__main__":
    unittest.main()
