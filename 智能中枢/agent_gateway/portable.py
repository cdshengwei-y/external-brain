from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional

from hub_core.errors import ConflictError, HubError, ValidationError
from hub_core.service import HubService

from .service import AgentGatewayError, TextAgent


class LocalHubApiClient:
    """Adapts HubService to the interface consumed by TextAgent."""

    def __init__(self, service: HubService) -> None:
        self.service = service

    def list_devices(self) -> list:
        return self.service.list_devices()

    def get_device_state(self, device_id: str) -> Dict[str, Any]:
        return self.service.get_device_state(device_id)

    def submit_command(
        self,
        device_id: str,
        capability: str,
        params: Optional[Dict[str, Any]],
        conversation_id: str,
        policy: str,
    ) -> Dict[str, Any]:
        return self.service.submit_command(
            device_id,
            {
                "conversation_id": conversation_id,
                "capability": capability,
                "params": params or {},
                "policy": policy,
            },
        )

    def wait_for_command(
        self,
        command_id: str,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        command = self.service.wait_for_command(command_id, timeout_ms / 1000)
        completed = command["state"] in {
            "succeeded",
            "failed",
            "cancelled",
            "superseded",
            "expired",
        }
        return {
            "command": command,
            "completed": completed,
            "timed_out": not completed,
        }


class PortableRequestService:
    """Accepts portable-device requests and processes them asynchronously."""

    MAX_MEDIA_BYTES = 10 * 1024 * 1024
    SUPPORTED_INPUT_TYPES = {"text", "audio"}

    def __init__(self, hub: HubService, max_workers: int = 4) -> None:
        self.hub = hub
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

    def _process_request(self, request_id: str) -> None:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            return
        try:
            if request["input_type"] == "audio":
                self._update(request_id, state="transcribing")
                self._update(
                    request_id,
                    state="failed",
                    error_code="AUDIO_UNDERSTANDING_NOT_CONFIGURED",
                    error_message="音频已收到，但固定式中枢尚未配置音频理解能力。",
                    response_text="音频已收到，但固定式中枢尚未配置音频理解能力。",
                    retryable=True,
                )
                return
            self._update(request_id, state="understanding")
            text = request.get("text") or ""
            capability = TextAgent._match_capability(text)
            intent = None
            if capability is not None:
                device_id = request.get("target_device_id")
                if not device_id:
                    device_id = self._resolve_device_id()
                intent = {
                    "capability": capability,
                    "device_id": device_id,
                    "params": {},
                }
            self._update(request_id, state="planned", intent=intent)

            agent = TextAgent(
                LocalHubApiClient(self.hub),
                device_id=request.get("target_device_id"),
                conversation_id=request["conversation_id"],
                on_command_submitted=lambda command: self._mark_command_submitted(
                    request_id,
                    command,
                ),
            )
            response_text = agent.respond(text)
            request_state = self._request_state(agent)
            result = None
            if agent.command_id:
                command = self.hub.get_command(agent.command_id)
                result = command.get("result")
            self._update(
                request_id,
                state=request_state,
                command_id=agent.command_id,
                response_text=response_text,
                result=result,
                error_code=("COMMAND_FAILED" if request_state == "failed" else None),
                error_message=(response_text if request_state == "failed" else None),
                retryable=request_state in {"queued", "timed_out"},
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

    def _mark_command_submitted(
        self,
        request_id: str,
        command: Dict[str, Any],
    ) -> None:
        command_state = command.get("state")
        request_state = "queued" if command_state == "queued" else "executing"
        self._update(
            request_id,
            state=request_state,
            command_id=command.get("command_id"),
        )

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
    def _request_state(agent: TextAgent) -> str:
        if agent.command_id is None:
            return "completed"
        return {
            "success": "succeeded",
            "queued": "queued",
            "timeout": "timed_out",
            "failure": "failed",
        }.get(agent.response_status, "failed")

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
            "input_type",
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
            if key not in {"media_id"}
        }

    @staticmethod
    def _public_media(media: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in media.items()
            if key != "data"
        }
