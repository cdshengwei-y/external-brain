import tempfile
import time
import unittest
import base64
import io
import struct
import wave
from unittest import mock

from agent_gateway.portable import PortableRequestService
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport


class FakePlanProvider:
    def __init__(self):
        self.plan_calls = []

    def plan_hub_command(
        self,
        request_id,
        conversation_id,
        input_text,
        target_device_id,
        source,
    ):
        self.plan_calls.append(
            {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "input_text": input_text,
                "target_device_id": target_device_id,
                "source": source,
            }
        )
        return {
            "status": "planned",
            "plan_id": f"plan-{request_id}",
            "reply": "准备开始录音，请确认。",
            "command": {
                "device_id": target_device_id,
                "capability": "record.start",
                "params": {},
            },
        }

    def dispatch_hub_command(
        self,
        request_id,
        conversation_id,
        plan_id,
        target_device_id,
        source,
    ):
        return {
            "ok": True,
            "status": "succeeded",
            "state": "succeeded",
            "command_id": "command-001",
            "message": "已经开始录音。",
            "result": {"recording": True},
        }


class FakeTranscriptionProvider:
    def __init__(self, response):
        self.response = response
        self.audio_data_urls = []

    def transcribe(self, audio_data_url):
        self.audio_data_urls.append(audio_data_url)
        return self.response


class LengthAwareTranscriptionProvider:
    """按送进来的音频长度返回不同转写，用来验证唤醒有没有先剪静音。

    唤醒前处理会把音频剪短，所以「比原文件短」就等于「这是去静音版本」。
    """

    def __init__(self, raw_len, trimmed_text, raw_text):
        self.raw_len = raw_len
        self.trimmed_text = trimmed_text
        self.raw_text = raw_text
        self.sizes = []

    def transcribe(self, audio_data_url):
        payload = base64.b64decode(audio_data_url.split(",", 1)[1])
        self.sizes.append(len(payload))
        if len(payload) < self.raw_len:
            return {"text": self.trimmed_text}
        return {"text": self.raw_text}


class FakeSpeechProvider:
    def __init__(self):
        self.texts = []
        # 和 LocalNeuralSpeechProvider 对齐：语速是可运行时修改的属性。
        self.speed = 1.0

    def synthesize(self, text):
        self.texts.append(text)
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\x00\x00" * 160)
        return output.getvalue(), "wav", "audio/wav"


class FakeRouteProvider:
    def __init__(self):
        self.route_calls = []

    def route_message(
        self,
        request_id,
        conversation_id,
        input_text,
        target_device_id,
        source,
    ):
        self.route_calls.append(
            {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "input_text": input_text,
                "target_device_id": target_device_id,
                "source": source,
            }
        )
        return {
            "route": "command",
            "status": "planned",
            "plan_id": f"route-plan-{request_id}",
            "reply": "准备开始录音，请确认。",
            "command": {
                "device_id": target_device_id or "bean_ye_1",
                "capability": "record.start",
                "params": {},
            },
        }


class PortableRequestServiceTest(unittest.TestCase):
    def setUp(self):
        # 本机装了 SenseVoice 模型时 local_asr 会抢在假远程识别前面，
        # 测试断言的是假远程的转写结果——这里强制关掉本地识别。
        mock.patch(
            "agent_gateway.local_asr.is_available", return_value=False
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = HubStore(self.database.name)
        self.transport = MemoryTransport()
        self.hub = HubService(self.store, self.transport)
        self.hub.register_device(
            {
                "device_id": "bean_ye_1",
                "device_type": "recording_beam",
                "protocol_version": "1.0",
                "firmware_version": "test",
                "capabilities": ["record.start", "record.stop", "status.read"],
            }
        )
        self.planner = FakePlanProvider()
        self.requests = PortableRequestService(self.hub, self.planner)

    def tearDown(self):
        self.requests.close()
        self.store.close()
        self.database.close()

    def _payload(self, request_id="request-001", text="查看设备"):
        return {
            "portable_device_id": "portable_001",
            "conversation_id": "conversation_001",
            "request_id": request_id,
            "protocol_version": "1.0",
            "source": "portable",
            "input_type": "text",
            "text": text,
            "target_device_id": "bean_ye_1",
        }

    def _wait_for_state(self, request_id, expected):
        deadline = time.time() + 1
        while time.time() < deadline:
            request = self.requests.get(request_id)
            if request["state"] == expected:
                return request
            time.sleep(0.01)
        return self.requests.get(request_id)

    def test_text_request_is_processed_and_persisted(self):
        created = self.requests.submit(self._payload())
        self.assertEqual(created["state"], "received")

        request = self._wait_for_state("request-001", "completed")

        self.assertEqual(request["state"], "completed")
        self.assertIn("当前设备", request["response_text"])
        self.assertEqual(request["conversation_id"], "conversation_001")

    def test_same_request_id_is_idempotent(self):
        first = self.requests.submit(self._payload())
        second = self.requests.submit(self._payload())

        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(
            self.store.get_portable_request("request-001")["request_id"],
            "request-001",
        )

    def test_same_request_id_with_different_payload_is_rejected(self):
        self.requests.submit(self._payload())

        with self.assertRaises(Exception) as context:
            self.requests.submit(self._payload(text="开始录音"))

        self.assertIn("请求内容不一致", str(context.exception))

    def test_command_request_keeps_request_and_command_ids_separate(self):
        self.requests.submit(self._payload(text="开始录音"))
        request = self._wait_for_state("request-001", "planned")

        self.assertEqual(request["state"], "planned")
        self.assertIsNotNone(request["plan_id"])
        self.assertIsNone(request["command_id"])
        self.assertEqual(len(self.transport.commands), 0)

        confirmed = self.requests.confirm("request-001", request["plan_id"])
        self.assertEqual(confirmed["state"], "confirming")
        deadline = time.time() + 1
        request = self.requests.get("request-001")
        while time.time() < deadline and request["command_id"] is None:
            time.sleep(0.01)
            request = self.requests.get("request-001")

        self.assertIn(request["state"], {"confirming", "succeeded"})
        self.assertIsNotNone(request["command_id"])
        self.assertNotEqual(request["command_id"], request["request_id"])
        self.assertEqual(len(self.transport.commands), 0)

        request = self._wait_for_state("request-001", "succeeded")
        self.assertEqual(request["state"], "succeeded")
        self.assertEqual(request["command_id"], "command-001")

    def test_successful_execution_does_not_reuse_planning_reply(self):
        self.requests.submit(self._payload(text="开始录音"))
        request = self._wait_for_state("request-001", "planned")
        self.planner.dispatch_hub_command = lambda **kwargs: {
            "ok": True,
            "status": "succeeded",
            "state": "succeeded",
            "command_id": "command-002",
            "reply": "准备对录音豆执行 record.start。",
        }

        self.requests.confirm("request-001", request["plan_id"])
        request = self._wait_for_state("request-001", "succeeded")

        self.assertEqual(request["response_text"], "已经开始录音。")

    def test_audio_upload_is_persisted_without_exposing_binary_data(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )

        self.assertTrue(media["media_id"].startswith("media-"))
        self.assertEqual(media["size_bytes"], len(b"RIFF-test-audio"))
        self.assertNotIn("data", media)
        stored = self.store.get_portable_media(media["media_id"])
        self.assertEqual(stored["data"], b"RIFF-test-audio")

    def test_audio_request_reports_missing_transcription_without_dispatching(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )
        created = self.requests.submit(
            {
                **self._payload(request_id="request-audio-001"),
                "input_type": "audio",
                "text": None,
                "media_id": media["media_id"],
            }
        )

        self.assertEqual(created["state"], "received")
        request = self._wait_for_state("request-audio-001", "failed")
        self.assertEqual(
            request["error_code"],
            "AUDIO_UNDERSTANDING_NOT_CONFIGURED",
        )
        self.assertEqual(len(self.transport.commands), 0)

    def test_audio_request_is_transcribed_then_planned(self):
        media_data = b"RIFF-test-audio"
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=media_data,
        )
        transcriber = FakeTranscriptionProvider({"result": {"text": "开始录音"}})
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=transcriber,
        )
        try:
            created = requests.submit(
                {
                    **self._payload(request_id="request-audio-002"),
                    "input_type": "audio",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            self.assertEqual(created["state"], "received")
            request = self._wait_for_state_with_service(
                requests,
                "request-audio-002",
                "planned",
            )
            expected_data_url = (
                "data:audio/wav;base64,"
                + base64.b64encode(media_data).decode("ascii")
            )
            self.assertEqual(transcriber.audio_data_urls, [expected_data_url])
            self.assertEqual(request["transcript"], "开始录音")
            self.assertEqual(self.planner.plan_calls[-1]["input_text"], "开始录音")
            self.assertEqual(request["state"], "planned")
        finally:
            requests.close()

    def test_audio_request_uses_auto_route_after_transcription(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )
        transcriber = FakeTranscriptionProvider({"text": "开始录音"})
        router = FakeRouteProvider()
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=transcriber,
            route_provider=router,
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="request-audio-route-001"),
                    "input_type": "audio",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(
                requests,
                "request-audio-route-001",
                "planned",
            )
            self.assertEqual(request["transcript"], "开始录音")
            self.assertEqual(router.route_calls[0]["input_text"], "开始录音")
            self.assertEqual(router.route_calls[0]["source"], "portable")
            self.assertEqual(self.planner.plan_calls, [])
        finally:
            requests.close()

    def test_audio_request_reports_empty_transcription(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )
        transcriber = FakeTranscriptionProvider({"result": {}})
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=transcriber,
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="request-audio-003"),
                    "input_type": "audio",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(
                requests,
                "request-audio-003",
                "failed",
            )
            self.assertEqual(request["error_code"], "AUDIO_TRANSCRIPTION_EMPTY")
            self.assertEqual(len(self.planner.plan_calls), 0)
        finally:
            requests.close()

    def test_wake_request_returns_awake_session_action(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=FakeTranscriptionProvider({"text": "贾维斯"}),
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wake-001"),
                    "input_type": "audio",
                    "session_mode": "wake",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(requests, "wake-001", "completed")
            self.assertEqual(request["session_action"], "awake")
            self.assertEqual(request["response_text"], "我在")
        finally:
            requests.close()

    def test_wake_request_accepts_common_asr_variants(self):
        for index, transcript in enumerate(
            (
                "贾文思",
                "假维基",
                "家维斯",
                # 实测 16:18:56 真机喊「贾维斯」被 Qwen ASR 写成「查维斯」，
                # 首字错一个字就唤不醒，所以尾音对上必须认。
                "查维斯。",
                "嘉伟斯。",
                "杰维斯！",
                "雅维斯",
                "加威斯",
                "贾维",
            )
        ):
            self.assertTrue(
                PortableRequestService._is_wake_phrase(transcript),
                transcript,
            )

    def test_xiaozhi_wake_words_are_removed(self):
        """2026-11-02 起只认「贾维斯」，「小智」一族不再唤醒。"""
        for transcript in (
            "你好小智",
            "小智",
            "小智同学",
            "小智小智",
            "晓智",
            "小志",
            "xiaozhi",
            "你好",
            "在吗",
        ):
            self.assertFalse(
                PortableRequestService._is_wake_phrase(transcript),
                transcript,
            )

    def test_wake_phrase_rejects_room_noise(self):
        # 待机时被房间噪声触发的上传，不能当成唤醒（设备会亮屏乱应）。
        for transcript in ("嗯。", "啊。", "长得。", "这个工资高了。", "维他命", "周末去哪"):
            self.assertFalse(
                PortableRequestService._is_wake_phrase(transcript),
                transcript,
            )

    def test_wake_phrase_supports_custom_word_from_env(self):
        with mock.patch.dict("os.environ", {"PET_WAKE_WORDS": "小豆, 豆豆"}):
            self.assertTrue(PortableRequestService._is_wake_phrase("小豆"))
            self.assertTrue(PortableRequestService._is_wake_phrase("豆豆。"))
        self.assertFalse(PortableRequestService._is_wake_phrase("小豆"))

    def test_wake_phrase_in_face_state_replies_i_am_here(self):
        """表情态（亮屏闲聊）喊唤醒词也要应一声，不能被语音门控吞掉。"""
        speech = FakeSpeechProvider()
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=FakeTranscriptionProvider({"text": "贾维斯"}),
            speech_provider=speech,
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wake-face-001"),
                    "input_type": "text",
                    "session_mode": "conversation",
                    "text": "贾维斯",
                }
            )
            request = self._wait_for_state_with_service(
                requests, "wake-face-001", "completed"
            )
            self.assertEqual(request["session_action"], "awake")
            self.assertEqual(request["response_text"], "我在")
        finally:
            requests.close()

    def test_strip_wake_phrase_variants(self):
        """连锁指令剥离：唤醒词后面的部分才是指令；纯唤醒剥完为空。"""
        strip = PortableRequestService._strip_wake_phrase
        self.assertEqual(strip("贾维斯"), "")
        self.assertEqual(strip("贾维斯。"), "")
        # ASR 常见近音写法也要剥干净，不能把「斯」留在指令里
        self.assertEqual(strip("查维斯。"), "")
        self.assertEqual(strip("贾维斯放首歌"), "放首歌")
        self.assertEqual(strip("查维斯放一首稻香"), "放一首稻香")
        self.assertEqual(strip("加维斯讲个笑话"), "讲个笑话")
        self.assertEqual(strip("贾维斯，今天天气怎么样"), "今天天气怎么样")
        self.assertEqual(strip("贾维放首歌"), "放首歌")
        self.assertEqual(strip("哎贾维斯，几点了"), "几点了")
        # 唤醒词不在句首的闲聊不能剥
        self.assertEqual(strip("我喜欢贾维斯的歌"), "")

    def test_wake_request_with_chained_command_executes_it(self):
        """「贾维斯几点了」要一次到位：既唤醒又直接回答，不让用户复述。"""
        speech = FakeSpeechProvider()
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=FakeTranscriptionProvider({"text": "贾维斯"}),
            speech_provider=speech,
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wake-chain-001"),
                    "source": "wearable",
                    "input_type": "text",
                    "session_mode": "wake",
                    "text": "贾维斯几点了",
                }
            )
            request = self._wait_for_state_with_service(
                requests, "wake-chain-001", "completed"
            )
            # 走了本地时间快通道，回答「现在是 X 点 Y 分。」
            self.assertIn("点", str(request["response_text"]))
            self.assertEqual(request["session_action"], "continue")
        finally:
            requests.close()

    def test_wake_transcription_uses_silence_trimmed_audio_first(self):
        """唤醒要先送「剪过首尾静音」的版本，这样才能把「贾」字认对。"""
        wav = self._make_wav(peak=3000)
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=wav,
        )
        provider = LengthAwareTranscriptionProvider(
            raw_len=len(wav), trimmed_text="贾维斯。", raw_text="查维斯。"
        )
        requests = PortableRequestService(
            self.hub, self.planner, transcription_provider=provider
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wake-trim-001"),
                    "input_type": "audio",
                    "session_mode": "wake",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(
                requests, "wake-trim-001", "completed"
            )
            self.assertEqual(request["session_action"], "awake")
            self.assertEqual(request["transcript"], "贾维斯。")
            self.assertEqual(len(provider.sizes), 1, provider.sizes)
            self.assertLess(provider.sizes[0], len(wav))
        finally:
            requests.close()

    def test_wake_transcription_retries_raw_audio_when_trimmed_misses(self):
        """剪得太狠把字头削掉时，要用原音频补一次。"""
        wav = self._make_wav(peak=3000)
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=wav,
        )
        provider = LengthAwareTranscriptionProvider(
            raw_len=len(wav), trimmed_text="嗯。", raw_text="贾维斯。"
        )
        requests = PortableRequestService(
            self.hub, self.planner, transcription_provider=provider
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wake-retry-001"),
                    "input_type": "audio",
                    "session_mode": "wake",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(
                requests, "wake-retry-001", "completed"
            )
            self.assertEqual(request["session_action"], "awake")
            # 紧裁先单独试一次；没命中之后「宽裁」和「原音频」是**并发**补的，
            # 所以只断言「三个版本都试过、原音频在里面」，不依赖完成顺序。
            self.assertEqual(len(provider.sizes), 3, provider.sizes)
            self.assertIn(len(wav), provider.sizes)
            self.assertLess(min(provider.sizes), len(wav))  # 至少有一版是裁过的
        finally:
            requests.close()

    def test_wake_transcription_does_not_retry_for_quiet_noise(self):
        """房间里 800 左右的底噪不该触发第二次 ASR，白花时间。"""
        wav = self._make_wav(peak=800)
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=wav,
        )
        provider = LengthAwareTranscriptionProvider(
            raw_len=len(wav), trimmed_text="嗯。", raw_text="贾维斯。"
        )
        requests = PortableRequestService(
            self.hub, self.planner, transcription_provider=provider
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wake-noise-001"),
                    "input_type": "audio",
                    "session_mode": "wake",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(
                requests, "wake-noise-001", "completed"
            )
            self.assertEqual(request["session_action"], "sleep")
            self.assertEqual(len(provider.sizes), 1, provider.sizes)
        finally:
            requests.close()

    @staticmethod
    def _make_wav(peak: int, seconds: float = 0.6, rate: int = 16000) -> bytes:
        """静音 0.4s + 方波 0.6s + 静音 0.6s，模拟设备上传的唤醒录音。"""
        frames = []
        for _ in range(int(rate * 0.4)):
            frames.append(0)
        for index in range(int(rate * seconds)):
            frames.append(peak if index % 2 == 0 else -peak)
        for _ in range(int(rate * 0.6)):
            frames.append(0)
        buffer = io.BytesIO()
        writer = wave.open(buffer, "wb")
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack("<%dh" % len(frames), *frames))
        writer.close()
        return buffer.getvalue()


    def test_exit_phrase_returns_sleep_action_and_audio(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )
        speech = FakeSpeechProvider()
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=FakeTranscriptionProvider({"text": "退下吧"}),
            speech_provider=speech,
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="sleep-001"),
                    "input_type": "audio",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(requests, "sleep-001", "completed")
            self.assertEqual(request["session_action"], "sleep")
            self.assertEqual(request["response_text"], "好的，我先待命。")
            stored = self.store.get_portable_request("sleep-001")
            self.assertTrue(stored["response_audio"])
            self.assertEqual(speech.texts, ["好的，我先待命。"])
        finally:
            requests.close()

    def test_wearable_command_auto_executes_without_confirmation(self):
        media = self.requests.upload_media(
            portable_device_id="portable_001",
            media_type="audio",
            audio_format="wav",
            content_type="audio/wav",
            data=b"RIFF-test-audio",
        )
        speech = FakeSpeechProvider()
        requests = PortableRequestService(
            self.hub,
            self.planner,
            transcription_provider=FakeTranscriptionProvider({"text": "开始录音"}),
            speech_provider=speech,
        )
        try:
            requests.submit(
                {
                    **self._payload(request_id="wearable-command-001"),
                    "source": "wearable",
                    "input_type": "audio",
                    "text": None,
                    "media_id": media["media_id"],
                }
            )
            request = self._wait_for_state_with_service(
                requests,
                "wearable-command-001",
                "succeeded",
            )
            self.assertEqual(request["state"], "succeeded")
            self.assertEqual(request["response_text"], "已经开始录音。")
            self.assertTrue(self.store.get_portable_request("wearable-command-001")["response_audio"])
        finally:
            requests.close()

    def test_wrong_plan_id_cannot_execute_request(self):
        self.requests.submit(self._payload(text="开始录音"))
        request = self._wait_for_state("request-001", "planned")

        with self.assertRaises(Exception) as context:
            self.requests.confirm("request-001", "plan-wrong")

        self.assertIn("plan_id", str(context.exception))
        self.assertEqual(len(self.transport.commands), 0)

    @staticmethod
    def _wait_for_state_with_service(service, request_id, expected):
        deadline = time.time() + 1
        while time.time() < deadline:
            request = service.get(request_id)
            if request["state"] == expected:
                return request
            time.sleep(0.01)
        return service.get(request_id)


class SpeechRateCommandTest(unittest.TestCase):
    """「说慢一点」改的是中枢 TTS 的语速，不是设备音量。

    2026-11-06 现场：用户连说两遍「你讲话讲慢一点」，模型判成 device_set，
    回了「音量调到 1 档，很小」。语速本来就是 provider.speed 一个浮点数，
    放进本地快通道，几毫秒就能答对。
    """

    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = HubStore(self.database.name)
        self.transport = MemoryTransport()
        self.hub = HubService(self.store, self.transport)
        self.speech = FakeSpeechProvider()
        self.requests = PortableRequestService(
            self.hub, FakePlanProvider(), speech_provider=self.speech
        )

    def tearDown(self):
        self.requests.close()
        self.store.close()
        self.database.close()

    def _ask(self, request_id, text):
        self.requests.submit(
            {
                "portable_device_id": "portable_001",
                "conversation_id": "conversation_001",
                "request_id": request_id,
                "protocol_version": "1.0",
                "source": "wearable",
                "input_type": "text",
                "text": text,
            }
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            request = self.requests.get(request_id)
            if request["state"] in ("completed", "failed"):
                return request
            time.sleep(0.01)
        return self.requests.get(request_id)

    def test_slower_lowers_speed(self):
        request = self._ask("rate-slow", "你说话能慢一点吗")

        self.assertAlmostEqual(self.speech.speed, 0.88, places=2)
        self.assertIn("慢", request["response_text"])
        self.assertEqual(request["result"]["intent"]["intent"], "speech_rate")

    def test_complaint_about_fast_also_slows_down(self):
        # 「你讲得太快了」是抱怨，本体也是让我放慢，不能判成「快一点」。
        self._ask("rate-fast-complaint", "你讲得太快了")

        self.assertAlmostEqual(self.speech.speed, 0.88, places=2)

    def test_faster_raises_speed(self):
        self._ask("rate-quick", "说快点")

        self.assertAlmostEqual(self.speech.speed, 1.12, places=2)

    def test_lower_bound_stops_and_says_so(self):
        last = None
        for index in range(6):
            last = self._ask(f"rate-floor-{index}", "再慢一点")
            time.sleep(0.02)

        self.assertAlmostEqual(self.speech.speed, PortableRequestService.SPEECH_RATE_MIN, places=2)
        self.assertIn("最慢", last["response_text"])

    def test_volume_request_is_not_hijacked(self):
        self._ask("rate-volume", "大点声")

        # 语速一点没动，说明它没被这条快通道抢走。
        self.assertAlmostEqual(self.speech.speed, 1.0, places=2)

    def test_provider_without_speed_is_skipped(self):
        class NoSpeedProvider:
            def synthesize(self, text):
                return FakeSpeechProvider().synthesize(text)

        service = PortableRequestService(
            self.hub, FakePlanProvider(), speech_provider=NoSpeedProvider()
        )
        self.addCleanup(service.close)

        self.assertFalse(service._try_local_speech_rate("rate-nospeed", "说慢一点"))


if __name__ == "__main__":
    unittest.main()
