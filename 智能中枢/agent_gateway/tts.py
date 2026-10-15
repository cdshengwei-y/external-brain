from __future__ import annotations

import base64
import binascii
import mimetypes
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urljoin
import urllib.request


class TextToSpeechError(RuntimeError):
    pass


class RK3588TtsClient:
    """Persist audio returned by the RK3588 TTS endpoint for local playback."""

    def __init__(
        self,
        rk3588_client: Any,
        output_dir: str | Path | None = None,
    ) -> None:
        self.rk3588_client = rk3588_client
        self.output_dir = Path(output_dir or tempfile.gettempdir()) / "jarvis-rk3588-tts"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path:
        normalized = str(text or "").strip()
        if not normalized:
            raise TextToSpeechError("没有可合成的文字")

        response = self.rk3588_client.synthesize(normalized)
        if not isinstance(response, dict):
            raise TextToSpeechError("RK3588 TTS 返回的数据格式不正确")
        audio_bytes, audio_format = self._extract_audio(response)
        output_path = self.output_dir / f"jarvis-{uuid.uuid4()}{self._suffix(audio_format)}"
        try:
            output_path.write_bytes(audio_bytes)
        except OSError as error:
            raise TextToSpeechError(f"无法保存 RK3588 TTS 音频：{error}") from error
        if output_path.stat().st_size == 0:
            raise TextToSpeechError("RK3588 TTS 返回了空音频")
        return output_path

    def _extract_audio(self, response: Dict[str, Any]) -> tuple[bytes, str]:
        audio_format = str(
            response.get("audio_format")
            or response.get("format")
            or response.get("mime_type")
            or "wav"
        )
        candidates = [
            response.get("audio_base64"),
            response.get("audio"),
            response.get("audio_data"),
            response.get("data"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                audio_format = str(
                    candidate.get("audio_format")
                    or candidate.get("format")
                    or candidate.get("mime_type")
                    or audio_format
                )
                candidate = (
                    candidate.get("base64")
                    or candidate.get("audio_base64")
                    or candidate.get("data")
                )
            if isinstance(candidate, str) and candidate.strip():
                if candidate.startswith(("http://", "https://")):
                    return self._download_audio(candidate), audio_format
                if candidate.startswith("data:"):
                    audio_format = candidate.split(",", 1)[0].split(";", 1)[0].removeprefix("data:")
                return self._decode_audio_value(candidate), audio_format

        audio_url = response.get("audio_url") or response.get("url")
        if isinstance(audio_url, str) and audio_url.strip():
            return self._download_audio(audio_url), audio_format

        raise TextToSpeechError(
            "RK3588 TTS 未返回可播放音频；需要 audio_base64、audio_url 或 data 字段"
        )

    @staticmethod
    def _decode_audio_value(value: str) -> bytes:
        normalized = "".join(value.strip().split())
        if normalized.startswith("data:"):
            try:
                _, encoded = normalized.split(",", 1)
            except ValueError as error:
                raise TextToSpeechError("RK3588 TTS 音频 data URL 格式不正确") from error
        else:
            encoded = normalized
        try:
            audio_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise TextToSpeechError("RK3588 TTS 音频不是有效的 Base64") from error
        if not audio_bytes:
            raise TextToSpeechError("RK3588 TTS 返回了空音频")
        return audio_bytes

    def _download_audio(self, audio_url: str) -> bytes:
        absolute_url = urljoin(
            f"{self.rk3588_client.base_url}/",
            audio_url,
        )
        headers = {"Accept": "audio/*,application/octet-stream"}
        api_key = getattr(self.rk3588_client, "api_key", None)
        if api_key:
            headers["X-Hub-Key"] = api_key
        request = urllib.request.Request(absolute_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.rk3588_client.timeout_seconds,
            ) as response:
                audio_bytes = response.read()
        except Exception as error:
            raise TextToSpeechError(f"无法下载 RK3588 TTS 音频：{error}") from error
        if not audio_bytes:
            raise TextToSpeechError("RK3588 TTS 音频地址返回了空内容")
        return audio_bytes

    @staticmethod
    def _suffix(audio_format: str) -> str:
        normalized = audio_format.lower().strip()
        if "/" in normalized:
            normalized = mimetypes.guess_extension(normalized) or normalized.rsplit("/", 1)[-1]
        normalized = normalized.removeprefix(".")
        if normalized in {"mpeg", "mpga"}:
            normalized = "mp3"
        if normalized not in {"wav", "mp3", "ogg", "aac", "m4a", "aiff", "flac"}:
            normalized = "wav"
        return f".{normalized}"

    @staticmethod
    def cleanup(audio_path: str | Path | None) -> None:
        if not audio_path:
            return
        path = Path(audio_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class MacOsTtsClient:
    """Generate local speech files with the macOS system voice."""

    def __init__(
        self,
        voice: str = "Tingting",
        output_dir: str | Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.voice = voice
        self.timeout_seconds = timeout_seconds
        self.say_executable = shutil.which("say") or "/usr/bin/say"
        self.output_dir = Path(output_dir or tempfile.gettempdir()) / "jarvis-tts"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path:
        normalized = str(text or "").strip()
        if not normalized:
            raise TextToSpeechError("没有可合成的文字")
        if not Path(self.say_executable).is_file():
            raise TextToSpeechError("当前系统没有可用的 macOS 语音合成命令")

        output_path = self.output_dir / f"jarvis-{uuid.uuid4()}.aiff"
        try:
            subprocess.run(
                [
                    self.say_executable,
                    "-v",
                    self.voice,
                    "-o",
                    str(output_path),
                    normalized,
                ],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise TextToSpeechError("语音合成超时") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode("utf-8", errors="ignore").strip()
            raise TextToSpeechError(detail or "语音合成失败") from error

        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise TextToSpeechError("语音合成没有生成音频文件")
        return output_path

    @staticmethod
    def cleanup(audio_path: str | Path | None) -> None:
        if not audio_path:
            return
        path = Path(audio_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
