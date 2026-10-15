import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_gateway.tts import MacOsTtsClient, RK3588TtsClient, TextToSpeechError


class MacOsTtsClientTest(unittest.TestCase):
    def test_synthesize_creates_audio_file_with_chinese_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MacOsTtsClient(output_dir=directory)

            def fake_run(command, **_kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"AIFF")

            with patch("agent_gateway.tts.subprocess.run", side_effect=fake_run) as run:
                audio_path = client.synthesize("你好，贾维斯。")

            self.assertTrue(audio_path.is_file())
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[:3], [client.say_executable, "-v", "Tingting"])
            self.assertEqual(command[-1], "你好，贾维斯。")
            client.cleanup(audio_path)
            self.assertFalse(audio_path.exists())

    def test_empty_text_is_rejected(self):
        client = MacOsTtsClient()

        with self.assertRaises(TextToSpeechError):
            client.synthesize("  ")


class RK3588TtsClientTest(unittest.TestCase):
    def test_synthesize_decodes_audio_base64_response(self):
        with tempfile.TemporaryDirectory() as directory:
            rk3588 = Mock()
            rk3588.synthesize.return_value = {
                "audio_base64": "UklGRg==",
                "audio_format": "wav",
            }
            client = RK3588TtsClient(rk3588, output_dir=directory)

            audio_path = client.synthesize("你好，贾维斯。")

            self.assertEqual(audio_path.suffix, ".wav")
            self.assertEqual(audio_path.read_bytes(), b"RIFF")
            rk3588.synthesize.assert_called_once_with("你好，贾维斯。")
            client.cleanup(audio_path)
            self.assertFalse(audio_path.exists())

    def test_synthesize_supports_data_url_and_mp3_format(self):
        with tempfile.TemporaryDirectory() as directory:
            rk3588 = Mock()
            rk3588.synthesize.return_value = {
                "audio": "data:audio/mpeg;base64,SUQz",
            }
            client = RK3588TtsClient(rk3588, output_dir=directory)

            audio_path = client.synthesize("测试")

            self.assertEqual(audio_path.suffix, ".mp3")
            self.assertEqual(audio_path.read_bytes(), b"ID3")

    def test_synthesize_rejects_response_without_audio(self):
        rk3588 = Mock()
        rk3588.synthesize.return_value = {"status": "ok"}
        client = RK3588TtsClient(rk3588)

        with self.assertRaisesRegex(TextToSpeechError, "未返回可播放音频"):
            client.synthesize("测试")


if __name__ == "__main__":
    unittest.main()
