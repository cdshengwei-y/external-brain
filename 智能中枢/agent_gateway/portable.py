from __future__ import annotations

import base64
import io
import threading
import uuid
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Tuple

from hub_core.errors import ConflictError, HubError, ValidationError
from hub_core.service import HubService

from .qwen_asr import QwenAsrClient
from .rk3588 import RK3588AgentClient
from .service import AgentGatewayError, TextAgent
from .tts import RK3588TtsClient


class HardwarePlanProvider(Protocol):
    def plan_hub_command(
        self,
        request_id: str,
        conversation_id: str,
        input_text: str,
        target_device_id: str,
        source: str,
    ) -> Dict[str, Any]:
        ...

    def dispatch_hub_command(
        self,
        request_id: str,
        conversation_id: str,
        plan_id: str,
        target_device_id: str,
        source: str,
    ) -> Dict[str, Any]:
        ...


class AudioTranscriptionProvider(Protocol):
    def transcribe(self, audio_data_url: str) -> Dict[str, Any]:
        ...


class MessageRouteProvider(Protocol):
    def route_message(
        self,
        request_id: str,
        conversation_id: str,
        input_text: str,
        target_device_id: Optional[str],
        source: str,
    ) -> Dict[str, Any]:
        ...


class SpeechSynthesisProvider(Protocol):
    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        ...


class RK3588SpeechProvider:
    def __init__(self, client: RK3588AgentClient) -> None:
        self.client = RK3588TtsClient(client)

    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        audio_path = self.client.synthesize(text)
        try:
            audio = audio_path.read_bytes()
        finally:
            self.client.cleanup(audio_path)
        normalized = _normalize_wav_for_wearable(audio)
        return normalized, "wav", "audio/wav"


class PortableAudioError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class PortableRequestService:
    PLAN_TTL_SECONDS = 300

    """Accepts portable-device requests and processes them asynchronously."""

    MAX_MEDIA_BYTES = 10 * 1024 * 1024
    SUPPORTED_INPUT_TYPES = {"text", "audio"}

    def __init__(
        self,
        hub: HubService,
        plan_provider: Optional["HardwarePlanProvider"] = None,
        transcription_provider: Optional["AudioTranscriptionProvider"] = None,
        route_provider: Optional["MessageRouteProvider"] = None,
        speech_provider: Optional["SpeechSynthesisProvider"] = None,
        max_workers: int = 4,
    ) -> None:
        self.hub = hub
        if plan_provider is None:
            rk3588_client = RK3588AgentClient(timeout_seconds=30.0)
            self.plan_provider = rk3588_client
            self.transcription_provider = transcription_provider or QwenAsrClient()
            self.route_provider = route_provider or rk3588_client
            self.speech_provider = speech_provider or RK3588SpeechProvider(
                rk3588_client
            )
        else:
            self.plan_provider = plan_provider
            self.transcription_provider = transcription_provider
            self.route_provider = route_provider or (
                plan_provider
                if callable(getattr(plan_provider, "route_message", None))
                else None
            )
            self.speech_provider = speech_provider
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="portable-request",
        )
        self._futures: Dict[str, Future[Any]] = {}

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def upload_media(
        self,
        portable_device_id: str,
        media_type: str,
        audio_format: Optional[str],
        content_type: Optional[str],
        data: bytes,
    ) -> Dict[str, Any]:
        if not isinstance(portable_device_id, str) or not portable_device_id.strip():
            raise ValidationError("X-Portable-Device-Id 必须是非空字符串")
        if media_type != "audio":
            raise ValidationError("X-Media-Type 当前必须是 audio")
        if not isinstance(audio_format, str) or not audio_format.strip():
            raise ValidationError("X-Audio-Format 必须是非空字符串")
        if not data:
            raise ValidationError("音频内容不能为空")
        if len(data) > self.MAX_MEDIA_BYTES:
            raise ValidationError(
                f"音频不能超过 {self.MAX_MEDIA_BYTES // (1024 * 1024)} MB"
            )
        media_id = f"media-{uuid.uuid4()}"
        media = self.hub.store.create_portable_media(
            media_id=media_id,
            portable_device_id=portable_device_id.strip(),
            media_type=media_type,
            audio_format=audio_format.strip().lower(),
            content_type=content_type,
            data=data,
        )
        self.hub.event_bus.publish(
            self.hub.store.add_event(
                event_type="portable.media.stored",
                payload=self._public_media(media),
            )
        )
        return self._public_media(media)

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._validate_payload(payload)
        request_id = normalized["request_id"]
        with self._lock:
            existing = self.hub.store.get_portable_request(request_id)
            if existing is not None:
                self._ensure_same_request(existing, normalized)
                return self._public_request(existing)
            request = self.hub.store.create_portable_request(**normalized)
            self._emit_request_event(request)
            self._futures[request_id] = self._executor.submit(
                self._process_request,
                request_id,
            )
            return self._public_request(request)

    def get(self, request_id: str) -> Dict[str, Any]:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        return self._public_request(request)

    def get_audio(self, request_id: str) -> Tuple[bytes, str]:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        audio = request.get("response_audio")
        content_type = request.get("response_audio_content_type") or "audio/wav"
        if not isinstance(audio, bytes) or not audio:
            raise HubError("请求还没有可播放的回复音频", status_code=404)
        return audio, str(content_type)

    def confirm(self, request_id: str, plan_id: str) -> Dict[str, Any]:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        if request.get("state") != "planned":
            raise ConflictError("当前请求不处于待确认状态")
        if request.get("plan_id") != plan_id:
            raise ConflictError("plan_id 与当前计划不匹配")
        if self._plan_expired(request):
            self._update(
                request_id,
                state="failed",
                error_code="PLAN_EXPIRED",
                error_message="计划已过期，请重新发起请求。",
                response_text="计划已过期，请重新发起请求。",
            )
            raise ConflictError("计划已过期，请重新发起请求")

        self._update(
            request_id,
            state="confirming",
            response_text="已确认，正在执行计划…",
        )
        with self._lock:
            self._futures[request_id] = self._executor.submit(
                self._execute_planned_request,
                request_id,
            )
        return self._public_request(
            self.hub.store.get_portable_request(request_id)  # type: ignore[arg-type]
        )

    def _process_request(self, request_id: str) -> None:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            return
        try:
            if request["input_type"] == "audio":
                self._update(request_id, state="transcribing")
                text = self._transcribe_request(request_id, request)
            else:
                text = request.get("text") or ""
            session_mode = request.get("session_mode") or "conversation"
            if session_mode == "wake":
                if self._is_wake_phrase(text):
                    self._update(
                        request_id,
                        state="completed",
                        response_text="我在",
                        session_action="awake",
                    )
                else:
                    self._update(
                        request_id,
                        state="completed",
                        response_text="",
                        session_action="sleep",
                    )
                return
            if self._is_exit_phrase(text):
                response_text = "好的，我先待命。"
                audio = self._synthesize(response_text)
                self._update(
                    request_id,
                    state="completed",
                    response_text=response_text,
                    session_action="sleep",
                    response_audio=audio[0] if audio else None,
                    response_audio_format=audio[1] if audio else None,
                    response_audio_content_type=audio[2] if audio else None,
                )
                return
            self._update(request_id, state="understanding")
            if TextAgent._is_list_devices_request(text):
                response_text = TextAgent._format_devices(self.hub.list_devices())
                audio = self._synthesize(response_text)
                self._update(
                    request_id,
                    state="completed",
                    response_text=response_text,
                    session_action="continue",
                    response_audio=audio[0] if audio else None,
                    response_audio_format=audio[1] if audio else None,
                    response_audio_content_type=audio[2] if audio else None,
                )
                return
            target_device_id = request.get("target_device_id")
            if self.route_provider is not None:
                response = self.route_provider.route_message(
                    request_id=request_id,
                    conversation_id=request["conversation_id"],
                    input_text=text,
                    target_device_id=target_device_id,
                    source=request["source"],
                )
                route = str(response.get("route") or "").strip().lower()
                if route in {"chat", "clarification", "assistant"}:
                    response_text = self._route_reply(
                        response,
                        "我暂时没有可用的回复。",
                    )
                    audio = self._synthesize(response_text)
                    self._update(
                        request_id,
                        state="completed",
                        response_text=response_text,
                        session_action="continue",
                        response_audio=audio[0] if audio else None,
                        response_audio_format=audio[1] if audio else None,
                        response_audio_content_type=audio[2] if audio else None,
                    )
                    return
                if not self._is_plan_response(response, route):
                    if self._route_reply(response, ""):
                        response_text = self._route_reply(response, "")
                        audio = self._synthesize(response_text)
                        self._update(
                            request_id,
                            state="completed",
                            response_text=response_text,
                            session_action="continue",
                            response_audio=audio[0] if audio else None,
                            response_audio_format=audio[1] if audio else None,
                            response_audio_content_type=audio[2] if audio else None,
                        )
                        return
                    raise HubError(
                        "RK3588 自动路由没有返回聊天回复或结构化计划",
                        status_code=502,
                    )
                device_id = target_device_id or self._resolve_device_id()
            else:
                device_id = target_device_id or self._resolve_device_id()
                if not device_id:
                    raise HubError("当前没有可用的真实设备", status_code=503)
                response = self.plan_provider.plan_hub_command(
                    request_id=request_id,
                    conversation_id=request["conversation_id"],
                    input_text=text,
                    target_device_id=device_id,
                    source="portable",
                )
            if not device_id:
                raise HubError("当前没有可用的真实设备", status_code=503)
            plan_id = response.get("plan_id")
            command = response.get("command") or {}
            if not isinstance(command, dict) and isinstance(response.get("plan"), dict):
                command = response["plan"].get("command") or {}
            if (
                not self._is_plan_response(response, str(response.get("route") or ""))
                or not isinstance(plan_id, str)
                or not isinstance(command, dict)
                or not isinstance(command.get("capability"), str)
            ):
                raise HubError("RK3588 未返回有效的结构化计划", status_code=502)
            intent = {
                "capability": command["capability"],
                "device_id": command.get("device_id") or device_id,
                "params": command.get("params") or {},
            }
            auto_execute = request["source"] == "wearable"
            self._update(
                request_id,
                state="confirming" if auto_execute else "planned",
                intent=intent,
                plan_id=plan_id,
                response_text=self._response_message(
                    response,
                    "计划已生成，请确认执行。",
                ),
                session_action="continue",
            )
            if auto_execute:
                self._execute_planned_request(request_id)
        except (ValidationError, ConflictError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="COMMAND_FAILED",
                error_message=error.message,
                response_text=error.message,
            )
        except (AgentGatewayError, HubError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="HUB_UNAVAILABLE",
                error_message=str(error),
                response_text=str(error),
                retryable=True,
            )
        except PortableAudioError as error:
            self._update(
                request_id,
                state="failed",
                error_code=error.code,
                error_message=error.message,
                response_text=error.message,
                retryable=error.retryable,
            )
        except Exception as error:  # pragma: no cover - defensive worker boundary
            self._update(
                request_id,
                state="failed",
                error_code="HUB_UNAVAILABLE",
                error_message=str(error),
                response_text="中枢处理请求时发生内部错误。",
                retryable=True,
            )
        finally:
            with self._lock:
                self._futures.pop(request_id, None)

    def _execute_planned_request(self, request_id: str) -> None:
        try:
            request = self.hub.store.get_portable_request(request_id)
            if request is None or not isinstance(request.get("intent"), dict):
                raise HubError("计划不存在或内容无效", status_code=409)
            intent = request["intent"]
            device_id = str(intent.get("device_id") or "")
            response = self.plan_provider.dispatch_hub_command(
                request_id=f"execute-{uuid.uuid4()}",
                conversation_id=request["conversation_id"],
                plan_id=str(request["plan_id"]),
                target_device_id=device_id,
                source="portable",
            )
            state = str(response.get("state") or response.get("status") or "")
            success = bool(response.get("ok")) or state == "succeeded"
            request_state = self._request_state_from_response(response, success)
            response_text = self._execution_response_message(
                response,
                intent,
                request_state,
            )
            audio = self._synthesize(response_text)
            self._update(
                request_id,
                state=request_state,
                command_id=response.get("command_id"),
                response_text=response_text,
                session_action="continue",
                response_audio=audio[0] if audio else None,
                response_audio_format=audio[1] if audio else None,
                response_audio_content_type=audio[2] if audio else None,
                result=response.get("result"),
                error_code=("COMMAND_FAILED" if not success else None),
                error_message=(
                    self._response_message(response, "设备执行失败。")
                    if not success
                    else None
                ),
                retryable=request_state in {"queued", "executing", "timed_out"},
            )
        except (ValidationError, ConflictError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="COMMAND_FAILED",
                error_message=error.message,
                response_text=error.message,
            )
        except (AgentGatewayError, HubError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="HUB_UNAVAILABLE",
                error_message=str(error),
                response_text=str(error),
                retryable=True,
            )
        finally:
            with self._lock:
                self._futures.pop(request_id, None)

    def _update(self, request_id: str, **changes: Any) -> Dict[str, Any]:
        request = self.hub.store.update_portable_request(request_id, **changes)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        self._emit_request_event(request)
        return request

    def _emit_request_event(self, request: Dict[str, Any]) -> None:
        self.hub.event_bus.publish(
            self.hub.store.add_event(
                event_type="portable.request.updated",
                payload=self._public_request(request),
            )
        )

    def _resolve_device_id(self) -> Optional[str]:
        devices = self.hub.list_devices()
        online = [device for device in devices if device.get("status") == "online"]
        if online:
            return online[0]["device_id"]
        if devices:
            return devices[0]["device_id"]
        return None

    @staticmethod
    def _request_state_from_response(
        response: Dict[str, Any],
        success: bool,
    ) -> str:
        state = str(response.get("state") or response.get("status") or "")
        if response.get("timed_out") and not success:
            return "timed_out"
        if success:
            return "succeeded"
        if state in {"queued", "accepted", "running", "pending"}:
            return "queued"
        return "failed"

    @staticmethod
    def _response_message(response: Dict[str, Any], fallback: str) -> str:
        for key in ("reply", "message", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    @classmethod
    def _execution_response_message(
        cls,
        response: Dict[str, Any],
        intent: Dict[str, Any],
        request_state: str,
    ) -> str:
        if request_state != "succeeded":
            return cls._response_message(response, "执行结果已返回。")

        for key in ("message", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        success_messages = {
            "record.start": "已经开始录音。",
            "record.stop": "已经停止录音。",
            "status.read": "设备状态查询完成。",
        }
        capability = intent.get("capability")
        if isinstance(capability, str) and capability in success_messages:
            return success_messages[capability]
        return "设备执行成功。"

    @staticmethod
    def _route_reply(response: Dict[str, Any], fallback: str) -> str:
        value = response.get("reply")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    @staticmethod
    def _is_plan_response(response: Dict[str, Any], route: str) -> bool:
        return response.get("status") == "planned" or route in {
            "command",
            "hardware",
            "tool",
        }

    def _transcribe_request(
        self,
        request_id: str,
        request: Dict[str, Any],
    ) -> str:
        provider = self.transcription_provider
        if provider is None:
            raise PortableAudioError(
                "AUDIO_UNDERSTANDING_NOT_CONFIGURED",
                "音频已收到，但固定式中枢尚未配置音频理解能力。",
                retryable=True,
            )

        media_id = request.get("media_id")
        media = self.hub.store.get_portable_media(str(media_id or ""))
        if media is None or media.get("media_type") != "audio":
            raise PortableAudioError(
                "AUDIO_MEDIA_NOT_FOUND",
                "找不到请求对应的音频内容。",
            )
        if media.get("portable_device_id") != request.get("portable_device_id"):
            raise PortableAudioError(
                "AUDIO_MEDIA_DEVICE_MISMATCH",
                "音频所属随身设备与当前请求不一致。",
            )
        data = media.get("data")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise PortableAudioError(
                "AUDIO_MEDIA_EMPTY",
                "请求对应的音频内容为空。",
            )

        content_type = str(media.get("content_type") or "").split(";", 1)[0].strip()
        content_type = content_type or self._audio_content_type(media.get("audio_format"))
        audio_data_url = (
            f"data:{content_type};base64,"
            f"{base64.b64encode(bytes(data)).decode('ascii')}"
        )
        try:
            response = provider.transcribe(audio_data_url)
        except Exception as error:
            raise PortableAudioError(
                "AUDIO_TRANSCRIPTION_FAILED",
                f"音频转写失败：{error}",
                retryable=True,
            ) from error

        transcript = self._extract_transcript(response)
        if not transcript:
            raise PortableAudioError(
                "AUDIO_TRANSCRIPTION_EMPTY",
                "音频转写未返回可用文字。",
                retryable=True,
            )
        self._update(request_id, transcript=transcript)
        return transcript

    @staticmethod
    def _audio_content_type(audio_format: Any) -> str:
        formats = {
            "wav": "audio/wav",
            "wave": "audio/wav",
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4",
            "aac": "audio/aac",
            "ogg": "audio/ogg",
            "opus": "audio/opus",
            "webm": "audio/webm",
            "pcm": "audio/pcm",
        }
        normalized = str(audio_format or "").strip().lower().lstrip(".")
        return formats.get(normalized, "application/octet-stream")

    @classmethod
    def _extract_transcript(cls, response: Any) -> str:
        if isinstance(response, str):
            return response.strip()
        if not isinstance(response, dict):
            return ""
        for key in ("transcript", "text", "recognized_text", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("result", "data", "output", "response"):
            nested = response.get(key)
            transcript = cls._extract_transcript(nested)
            if transcript:
                return transcript
        return ""

    def _plan_expired(self, request: Dict[str, Any]) -> bool:
        updated_at = request.get("updated_at")
        if not isinstance(updated_at, str):
            return True
        created = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() > self.PLAN_TTL_SECONDS

    @staticmethod
    def _validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        required = [
            "portable_device_id",
            "conversation_id",
            "request_id",
            "input_type",
        ]
        values: Dict[str, Any] = {}
        for key in required:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{key} 必须是非空字符串")
            values[key] = value.strip()
        protocol_version = payload.get("protocol_version", "1.0")
        if protocol_version != "1.0":
            raise ValidationError("protocol_version 必须是 1.0")
        source = payload.get("source", "portable")
        if not isinstance(source, str) or not source.strip():
            raise ValidationError("source 必须是非空字符串")
        values["protocol_version"] = "1.0"
        values["source"] = source.strip()
        session_mode = payload.get("session_mode", "conversation")
        if session_mode not in {"wake", "conversation"}:
            raise ValidationError("session_mode 必须是 wake 或 conversation")
        values["session_mode"] = session_mode
        text = payload.get("text")
        media_id = payload.get("media_id")
        if values["input_type"] == "text":
            if not isinstance(text, str) or not text.strip():
                raise ValidationError("text 必须是非空字符串")
        elif not isinstance(media_id, str) or not media_id.strip():
            raise ValidationError("音频请求必须提供 media_id")
        values.update(
            {
                "text": text.strip() if isinstance(text, str) else None,
                "media_id": media_id.strip() if isinstance(media_id, str) else None,
                "target_device_id": (
                    payload.get("target_device_id").strip()
                    if isinstance(payload.get("target_device_id"), str)
                    and payload.get("target_device_id").strip()
                    else None
                ),
            }
        )
        return values

    @staticmethod
    def _ensure_same_request(
        existing: Dict[str, Any],
        normalized: Dict[str, Any],
    ) -> None:
        for key in (
            "portable_device_id",
            "conversation_id",
            "protocol_version",
            "source",
            "input_type",
            "session_mode",
            "text",
            "media_id",
            "target_device_id",
        ):
            if existing.get(key) != normalized.get(key):
                raise ConflictError("request_id 已存在，但请求内容不一致")

    @staticmethod
    def _public_request(request: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in request.items()
            if key not in {"media_id", "response_audio"}
        }

    def _synthesize(self, text: str) -> Optional[Tuple[bytes, str, str]]:
        if self.speech_provider is None or not text.strip():
            return None
        try:
            return self.speech_provider.synthesize(text)
        except Exception as error:
            self.hub.event_bus.publish(
                self.hub.store.add_event(
                    event_type="portable.tts.failed",
                    payload={"message": str(error)},
                )
            )
            return None

    @staticmethod
    def _is_wake_phrase(text: str) -> bool:
        normalized = "".join(str(text).lower().split())
        aliases = (
            "贾维斯",
            "贾文思",
            "假维基",
            "假维斯",
            "家维斯",
            "加维斯",
            "家卫斯",
            "加卫斯",
            "贾威斯",
            "贾威思",
            "贾维思",
            "贾维",
            "javis",
            "jarvis",
        )
        return any(alias in normalized for alias in aliases)

    @staticmethod
    def _is_exit_phrase(text: str) -> bool:
        normalized = "".join(str(text).lower().split())
        return any(phrase in normalized for phrase in ("退下吧", "退下", "回到待机"))

    @staticmethod
    def _public_media(media: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in media.items()
            if key != "data"
        }


def _normalize_wav_for_wearable(audio: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError) as error:
        raise PortableAudioError(
            "TTS_AUDIO_UNSUPPORTED",
            "RK3588 TTS 必须返回 PCM WAV 音频。",
        ) from error
    if channels not in {1, 2} or sample_width != 2 or sample_rate <= 0:
        raise PortableAudioError(
            "TTS_AUDIO_UNSUPPORTED",
            "RK3588 TTS 只支持 16 bit 单声道或双声道 WAV。",
        )
    import array

    samples = array.array("h")
    samples.frombytes(frames)
    if channels == 2:
        samples = array.array(
            "h",
            [
                max(-32768, min(32767, (samples[index] + samples[index + 1]) // 2))
                for index in range(0, len(samples) - 1, 2)
            ],
        )
    if sample_rate != 16000 and samples:
        target_count = max(1, round(len(samples) * 16000 / sample_rate))
        source_count = len(samples)
        resampled = array.array("h")
        for index in range(target_count):
            position = index * (source_count - 1) / max(1, target_count - 1)
            left = int(position)
            right = min(left + 1, source_count - 1)
            fraction = position - left
            value = round(samples[left] + (samples[right] - samples[left]) * fraction)
            resampled.append(max(-32768, min(32767, value)))
        samples = resampled
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(samples.tobytes())
    return output.getvalue()
