from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .errors import ConflictError, HubError, NotFoundError, ValidationError
from .events import EventBus
from .models import (
    ACTIVE_COMMAND_STATES,
    COMMAND_POLICIES,
    COMMAND_STATES,
    TERMINAL_COMMAND_STATES,
)
from .store import HubStore
from .transport import CommandTransport, MemoryTransport


class HubService:
    def __init__(
        self,
        store: HubStore,
        transport: Optional[CommandTransport] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.store = store
        self.transport = transport or MemoryTransport()
        self.event_bus = event_bus or EventBus()
        self._lock = threading.RLock()
        self._pending_availability: Dict[str, str] = {}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "service": "smart-hub"}

    def mark_persisted_devices_offline(self) -> int:
        """Clear stale online flags before the MQTT retained state is replayed."""
        return self.store.mark_all_devices_offline()

    def register_device(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        device_id = self._required_string(payload, "device_id")
        device_type = self._required_string(payload, "device_type")
        protocol_version = self._required_string(payload, "protocol_version")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) and item for item in capabilities
        ):
            raise ValidationError("capabilities 必须是非空字符串数组")
        status = payload.get("status", "online")
        if status not in {"online", "offline"}:
            raise ValidationError("status 必须是 online 或 offline")
        device = self.store.register_device(
            device_id=device_id,
            device_type=device_type,
            protocol_version=protocol_version,
            firmware_version=payload.get("firmware_version"),
            capabilities=capabilities,
            metadata=payload.get("metadata") or {},
            status=status,
        )
        self._emit("device.registered", device_id, None, {"device": device})
        return device

    def list_devices(self) -> list:
        return self.store.list_devices()

    def get_device(self, device_id: str) -> Dict[str, Any]:
        device = self.store.get_device(device_id)
        if device is None:
            raise NotFoundError(f"设备不存在: {device_id}")
        return device

    def get_device_state(self, device_id: str) -> Dict[str, Any]:
        self._ensure_device(device_id)
        stored_state = self.store.get_device_state(device_id)
        if stored_state is None:
            return {
                "device_id": device_id,
                "available": False,
                "updated_at": None,
            }
        return {
            **stored_state["state"],
            "device_id": device_id,
            "available": True,
            "updated_at": stored_state["updated_at"],
        }

    def update_availability(self, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        status = payload.get("status")
        if status not in {"online", "offline"}:
            raise ValidationError("availability.status 必须是 online 或 offline")
        device = self.store.update_device_status(device_id, status)
        if device is None:
            raise NotFoundError(f"设备不存在: {device_id}")
        self._emit("device.availability", device_id, None, {"status": status})
        return device

    def ingest_state(self, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_device(device_id)
        self.store.upsert_device_state(device_id, payload)
        event = self._emit("device.state", device_id, None, payload)
        return event

    def ingest_device_event(self, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_device(device_id)
        self.store.touch_device(device_id)
        event = self._emit("device.event", device_id, None, payload)
        return event

    def submit_command(
        self,
        device_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        capability = self._required_string(payload, "capability")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise ValidationError("params 必须是 JSON 对象")
        policy = payload.get("policy") or self._default_policy(capability)
        if policy not in COMMAND_POLICIES:
            raise ValidationError(
                f"policy 必须是: {', '.join(sorted(COMMAND_POLICIES))}"
            )
        conversation_id = payload.get("conversation_id")
        parent_command_id = payload.get("parent_command_id")
        command_id = payload.get("command_id") or str(uuid.uuid4())

        with self._lock:
            device = self._ensure_device(device_id)
            if capability not in device["capabilities"]:
                raise ValidationError(
                    f"设备 {device_id} 不支持能力: {capability}"
                )
            if self.store.get_command(command_id) is not None:
                raise ConflictError(f"command_id 已存在: {command_id}")

            active = self.store.list_active_commands(device_id)
            if policy == "reject_if_busy" and active:
                raise ConflictError(f"设备正在执行任务: {active[0]['command_id']}")

            if policy == "replace_active":
                for active_command in active:
                    self.store.update_command(
                        active_command["command_id"],
                        state="superseded",
                        result={"superseded_by": command_id},
                    )
                    self._emit(
                        "command.updated",
                        device_id,
                        active_command["command_id"],
                        {
                            "state": "superseded",
                            "superseded_by": command_id,
                        },
                    )
                active = []

            state = "queued" if active and policy == "queue" else "accepted"
            command = self.store.create_command(
                command_id=command_id,
                conversation_id=conversation_id,
                device_id=device_id,
                capability=capability,
                params=params,
                policy=policy,
                state=state,
                parent_command_id=parent_command_id,
            )
            self._emit("command.created", device_id, command_id, command)
            if state == "accepted":
                self._dispatch(command)
            return self.store.get_command(command_id)

    def get_command(self, command_id: str) -> Dict[str, Any]:
        command = self.store.get_command(command_id)
        if command is None:
            raise NotFoundError(f"命令不存在: {command_id}")
        return command

    def wait_for_command(
        self,
        command_id: str,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        command = self.get_command(command_id)
        if command["state"] in TERMINAL_COMMAND_STATES or timeout_seconds <= 0:
            return command

        subscription = self.subscribe_events()
        deadline = time.monotonic() + timeout_seconds
        try:
            command = self.get_command(command_id)
            if command["state"] in TERMINAL_COMMAND_STATES:
                return command
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.get_command(command_id)
                try:
                    event = subscription.queue.get(timeout=remaining)
                except queue.Empty:
                    return self.get_command(command_id)
                if event.get("command_id") != command_id:
                    continue
                command = self.get_command(command_id)
                if command["state"] in TERMINAL_COMMAND_STATES:
                    return command
        finally:
            self.unsubscribe_events(subscription)

    def cancel_command(self, command_id: str) -> Dict[str, Any]:
        with self._lock:
            command = self.get_command(command_id)
            if command["state"] in COMMAND_STATES - ACTIVE_COMMAND_STATES:
                return command
            if command["state"] == "queued":
                updated = self.store.update_command(command_id, state="cancelled")
                self._emit("command.updated", command["device_id"], command_id, updated)
                return updated

            updated = self.store.update_command(command_id, state="cancel_requested")
            self._emit("command.updated", command["device_id"], command_id, updated)
            self.transport.publish_cancel(
                command["device_id"],
                {
                    "type": "cancel",
                    "command_id": command_id,
                    "device_id": command["device_id"],
                },
            )
            return updated

    def acknowledge_command(
        self,
        device_id: str,
        command_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        state = payload.get("state")
        if state not in COMMAND_STATES - {"created", "queued", "accepted", "superseded"}:
            raise ValidationError(
                "设备确认状态必须是 running、succeeded、failed、cancel_requested、cancelled 或 expired"
            )
        with self._lock:
            command = self.get_command(command_id)
            if command["device_id"] != device_id:
                raise ValidationError("命令不属于当前设备")
            updated = self.store.update_command(
                command_id,
                state=state,
                error=payload.get("error"),
                result=payload.get("result"),
            )
            self.store.touch_device(device_id)
            self._emit("command.updated", device_id, command_id, updated)
            if state in {"succeeded", "failed", "cancelled", "expired"}:
                self._dispatch_next(device_id)
            return updated

    def list_events(self, since_id: int = 0, limit: int = 100) -> list:
        return self.store.list_events(since_id=since_id, limit=limit)

    def subscribe_events(self):
        return self.event_bus.subscribe()

    def unsubscribe_events(self, subscription) -> None:
        self.event_bus.unsubscribe(subscription)

    def handle_mqtt_message(self, topic: str, payload: Dict[str, Any]) -> None:
        parts = topic.strip("/").split("/")
        if len(parts) < 5 or parts[-3] != "devices":
            return
        device_id = parts[-2]
        message_type = parts[-1]
        if message_type == "manifest":
            existing = self.store.get_device(device_id)
            manifest = dict(payload)
            manifest["status"] = (
                self._pending_availability.pop(device_id, None)
                or (existing["status"] if existing is not None else "offline")
            )
            self.register_device(manifest)
        elif message_type == "availability":
            if self.store.get_device(device_id) is not None:
                self.update_availability(device_id, payload)
            elif payload.get("status") in {"online", "offline"}:
                self._pending_availability[device_id] = payload["status"]
        elif message_type == "state":
            self.ingest_state(device_id, payload)
        elif message_type == "events":
            self.ingest_device_event(device_id, payload)
        elif message_type == "command-acks":
            command_id = payload.get("command_id")
            if not command_id:
                raise ValidationError("command-acks 缺少 command_id")
            self.acknowledge_command(device_id, command_id, payload)

    def _dispatch(self, command: Dict[str, Any]) -> None:
        envelope = {
            "type": "command",
            "command_id": command["command_id"],
            "conversation_id": command["conversation_id"],
            "device_id": command["device_id"],
            "capability": command["capability"],
            "params": command["params"],
            "issued_at": command["created_at"],
        }
        self.transport.publish_command(command["device_id"], envelope)
        self._emit(
            "command.dispatched",
            command["device_id"],
            command["command_id"],
            envelope,
        )

    def _dispatch_next(self, device_id: str) -> None:
        active = self.store.list_active_commands(device_id)
        if any(command["state"] in {"accepted", "running", "cancel_requested"} for command in active):
            return
        queued = self.store.list_queued_commands(device_id)
        if not queued:
            return
        next_command = self.store.update_command(queued[0]["command_id"], state="accepted")
        self._emit("command.updated", device_id, next_command["command_id"], next_command)
        self._dispatch(next_command)

    def _emit(
        self,
        event_type: str,
        device_id: Optional[str],
        command_id: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        event = self.store.add_event(
            event_type=event_type,
            device_id=device_id,
            command_id=command_id,
            payload=payload,
        )
        self.event_bus.publish(event)
        return event

    def _ensure_device(self, device_id: str) -> Dict[str, Any]:
        device = self.store.get_device(device_id)
        if device is None:
            raise NotFoundError(f"设备不存在: {device_id}")
        return device

    @staticmethod
    def _required_string(payload: Dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _default_policy(capability: str) -> str:
        if capability in {"indicator.set", "audio.volume.set", "fan.speed.set"}:
            return "replace_active"
        return "reject_if_busy"
