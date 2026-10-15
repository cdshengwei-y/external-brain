import tempfile
import time
import unittest
import base64

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


if __name__ == "__main__":
    unittest.main()
