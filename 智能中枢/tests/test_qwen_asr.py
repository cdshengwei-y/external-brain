import base64
import json
import unittest
from unittest.mock import patch

from agent_gateway.qwen_asr import QwenAsrClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class QwenAsrClientTest(unittest.TestCase):
    @patch("agent_gateway.qwen_asr.urllib.request.urlopen")
    def test_transcribe_uses_recording_bean_multipart_contract(self, urlopen):
        urlopen.return_value = FakeResponse(
            {
                "text": "开始录音。",
                "language": "Chinese",
                "model": "Qwen3-ASR-1.7B",
            }
        )
        audio_bytes = b"RIFF-test-audio"
        audio_data_url = (
            "data:audio/wav;base64,"
            + base64.b64encode(audio_bytes).decode("ascii")
        )

        response = QwenAsrClient("http://192.168.0.153:18005").transcribe(
            audio_data_url
        )

        self.assertEqual(response["text"], "开始录音。")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.168.0.153:18005/transcribe")
        self.assertEqual(request.method, "POST")
        self.assertIn(b'name="file"', request.data)
        self.assertIn(b'filename="audio.wav"', request.data)
        self.assertIn(b'name="language"', request.data)
        self.assertIn(audio_bytes, request.data)


if __name__ == "__main__":
    unittest.main()
