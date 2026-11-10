from __future__ import annotations

import base64
import binascii
import json
import os
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

DEFAULT_BASE_URL = "https://192.168.0.153:18005"


class QwenAsrError(RuntimeError):
    pass


class QwenAsrClient:
    """Client for the LAN Qwen3-ASR service used by the recording-bean backend.

    服务端（Qwen3-ASR-1.7B）用的是自签证书（CN=qwen3-asr-local），所以 HTTPS
    连接必须放开证书校验；局域网内服务，不做 PKI 验证。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        resolved = base_url or os.environ.get("QWEN_ASR_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = resolved.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._ssl_context: Optional[ssl.SSLContext] = None
        if self.base_url.lower().startswith("https://"):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context
        # 局域网直连：显式清空代理，否则会被 HTTP_PROXY 抢走请求。
        handlers: list[Any] = [urllib.request.ProxyHandler({})]
        if self._ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self._ssl_context))
        self._opener = urllib.request.build_opener(*handlers)

    def _open(self, request: urllib.request.Request):
        return self._opener.open(request, timeout=self.timeout_seconds)

    def health(self) -> Dict[str, Any]:
        return self._request_json("GET", "/health")

    def transcribe(self, audio_data_url: str) -> Dict[str, Any]:
        content_type, audio_bytes = self._decode_data_url(audio_data_url)
        boundary = f"----ExternalBrainQwenAsr{uuid.uuid4().hex}"
        body = self._multipart_body(boundary, content_type, audio_bytes)
        request = urllib.request.Request(
            f"{self.base_url}/transcribe",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with self._open(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = self._error_detail(error)
            raise QwenAsrError(
                f"千问 ASR 转写失败（HTTP {error.code}）：{detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise QwenAsrError(f"无法连接千问 ASR 服务：{error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise QwenAsrError("千问 ASR 返回的数据不是有效 JSON。") from error
        if not isinstance(payload, dict):
            raise QwenAsrError("千问 ASR 返回的数据格式不正确。")
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
    ) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json"},
            method=method,
        )
        try:
            with self._open(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as error:
            raise QwenAsrError(f"无法连接千问 ASR 服务：{error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise QwenAsrError("千问 ASR 返回的数据不是有效 JSON。") from error
        if not isinstance(payload, dict):
            raise QwenAsrError("千问 ASR 返回的数据格式不正确。")
        return payload

    @staticmethod
    def _decode_data_url(audio_data_url: str) -> Tuple[str, bytes]:
        if not isinstance(audio_data_url, str) or not audio_data_url.startswith("data:"):
            raise QwenAsrError("音频必须是 data URL。")
        try:
            metadata, encoded = audio_data_url.split(",", 1)
        except ValueError as error:
            raise QwenAsrError("音频 data URL 格式不正确。") from error
        if ";base64" not in metadata:
            raise QwenAsrError("音频 data URL 必须使用 base64 编码。")
        content_type = metadata[5:].split(";", 1)[0].strip() or "audio/wav"
        try:
            audio_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise QwenAsrError("音频 data URL 的 base64 内容无效。") from error
        if not audio_bytes:
            raise QwenAsrError("音频内容不能为空。")
        return content_type, audio_bytes

    @staticmethod
    def _multipart_body(
        boundary: str,
        content_type: str,
        audio_bytes: bytes,
    ) -> bytes:
        boundary_bytes = boundary.encode("ascii")
        parts = [
            b"--" + boundary_bytes + b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            audio_bytes,
            b"\r\n--" + boundary_bytes + b"\r\n",
            b'Content-Disposition: form-data; name="language"\r\n\r\n',
            "zh".encode("ascii"),
            b"\r\n--" + boundary_bytes + b"--\r\n",
        ]
        return b"".join(parts)

    @staticmethod
    def _error_detail(error: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return str(error.reason)
        if isinstance(payload, dict):
            return str(payload.get("error") or payload.get("detail") or payload)
        return str(payload)
