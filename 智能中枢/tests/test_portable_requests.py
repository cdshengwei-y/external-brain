import tempfile
import time
import unittest

from agent_gateway.portable import PortableRequestService
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport


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
        self.requests = PortableRequestService(self.hub)

    def tearDown(self):
        self.requests.close()
        self.store.close()
        self.database.close()

    def _payload(self, request_id="request-001", text="查看设备"):
        return {
            "portable_device_id": "portable_001",
            "conversation_id": "conversation_001",
            "request_id": request_id,
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
        deadline = time.time() + 1
        request = self.requests.get("request-001")
        while time.time() < deadline and request["command_id"] is None:
            time.sleep(0.01)
            request = self.requests.get("request-001")

        self.assertEqual(request["state"], "executing")
        self.assertIsNotNone(request["command_id"])
        self.assertEqual(len(self.transport.commands), 1)

        self.hub.acknowledge_command(
            "bean_ye_1",
            request["command_id"],
            {"state": "succeeded", "result": {"recording": True}},
        )
        request = self._wait_for_state("request-001", "succeeded")
        self.assertEqual(request["state"], "succeeded")

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


if __name__ == "__main__":
    unittest.main()
