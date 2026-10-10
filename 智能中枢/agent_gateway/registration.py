from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .rk3588 import RK3588AgentClient


@dataclass(frozen=True)
class RK3588DeviceRegistration:
    device_id: str
    name: str
    base_url: str
    capabilities: tuple[str, ...]

    def payload(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "base_url": self.base_url.rstrip("/"),
            "capabilities": list(self.capabilities),
        }


class RK3588DeviceRegistrar:
    """Keep the local hardware gateway's device directory synchronized.

    The registrar only publishes device metadata to RK3588. It never sends a
    hardware command, so restarting RK3588 or the desktop app is safe.
    """

    def __init__(
        self,
        client: RK3588AgentClient,
        registration: RK3588DeviceRegistration,
        check_interval_seconds: float = 30.0,
        initial_retry_seconds: float = 2.0,
        max_retry_seconds: float = 60.0,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if check_interval_seconds <= 0:
            raise ValueError("check_interval_seconds 必须大于 0")
        if initial_retry_seconds <= 0:
            raise ValueError("initial_retry_seconds 必须大于 0")
        if max_retry_seconds < initial_retry_seconds:
            raise ValueError("max_retry_seconds 不能小于 initial_retry_seconds")

        self.client = client
        self.registration = registration
        self.check_interval_seconds = check_interval_seconds
        self.initial_retry_seconds = initial_retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.status_callback = status_callback
        self.last_status = "idle"
        self.last_error: Optional[str] = None
        self.last_sync_at: Optional[float] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._last_registered_payload: Optional[Dict[str, Any]] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rk3588-device-registrar",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_seconds)
        self._set_status("stopped")

    def sync_once(self) -> str:
        """Synchronize once and return ``registered`` or ``already_registered``."""
        response = self.client.list_hub_devices()
        devices = self._extract_devices(response)
        current = next(
            (
                device
                for device in devices
                if device.get("device_id") == self.registration.device_id
            ),
            None,
        )
        if current is not None and not self._needs_update(current):
            self._set_status("already_registered")
            return "already_registered"

        self.client.register_hub_device(
            device_id=self.registration.device_id,
            name=self.registration.name,
            base_url=self.registration.base_url.rstrip("/"),
            capabilities=list(self.registration.capabilities),
        )
        self._last_registered_payload = self.registration.payload()
        self._set_status("registered")
        return "registered"

    def _run(self) -> None:
        retry_delay = self.initial_retry_seconds
        while not self._stop_event.is_set():
            try:
                self.sync_once()
                retry_delay = self.initial_retry_seconds
                if self._stop_event.wait(self.check_interval_seconds):
                    break
            except Exception as error:
                self._set_status("retrying", str(error))
                if self._stop_event.wait(retry_delay):
                    break
                retry_delay = min(retry_delay * 2, self.max_retry_seconds)

    def _needs_update(self, device: Dict[str, Any]) -> bool:
        expected = self.registration.payload()
        if "name" in device and device.get("name") != expected["name"]:
            return True
        if "base_url" in device:
            actual_base_url = str(device.get("base_url") or "").rstrip("/")
            if actual_base_url != expected["base_url"]:
                return True
        if "capabilities" in device:
            actual_capabilities = self._normalize_capabilities(device["capabilities"])
            if actual_capabilities != set(expected["capabilities"]):
                return True
        fields_are_complete = all(
            field in device for field in ("name", "base_url", "capabilities")
        )
        return not fields_are_complete and self._last_registered_payload != expected

    @staticmethod
    def _extract_devices(response: Any) -> List[Dict[str, Any]]:
        devices: Any
        if isinstance(response, dict):
            devices = response.get("devices")
        else:
            devices = response
        if not isinstance(devices, list):
            raise ValueError("RK3588 设备目录返回格式不正确")
        return [device for device in devices if isinstance(device, dict)]

    @staticmethod
    def _normalize_capabilities(capabilities: Any) -> set[str]:
        if not isinstance(capabilities, (list, tuple, set)):
            return set()
        return {str(capability) for capability in capabilities}

    def _set_status(self, status: str, error: Optional[str] = None) -> None:
        with self._state_lock:
            self.last_status = status
            self.last_error = error
            if status in {"registered", "already_registered"}:
                self.last_sync_at = time.time()
        if self.status_callback is not None:
            try:
                self.status_callback(status)
            except Exception:
                pass
