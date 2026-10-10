from __future__ import annotations

import threading
from typing import Any, Dict, List, Protocol


class CommandTransport(Protocol):
    def publish_command(self, device_id: str, envelope: Dict[str, Any]) -> None:
        ...

    def publish_cancel(self, device_id: str, envelope: Dict[str, Any]) -> None:
        ...


class MemoryTransport:
    """Development transport used before a real MQTT broker is connected."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.commands: List[Dict[str, Any]] = []
        self.cancellations: List[Dict[str, Any]] = []

    def publish_command(self, device_id: str, envelope: Dict[str, Any]) -> None:
        with self._lock:
            self.commands.append({"device_id": device_id, "envelope": envelope})

    def publish_cancel(self, device_id: str, envelope: Dict[str, Any]) -> None:
        with self._lock:
            self.cancellations.append({"device_id": device_id, "envelope": envelope})
