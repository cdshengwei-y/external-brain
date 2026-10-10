from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


COMMAND_STATES = {
    "created",
    "accepted",
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
    "expired",
}

ACTIVE_COMMAND_STATES = {"accepted", "queued", "running", "cancel_requested"}
TERMINAL_COMMAND_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
    "expired",
}
COMMAND_POLICIES = {"queue", "replace_active", "reject_if_busy"}


@dataclass(frozen=True)
class Device:
    device_id: str
    device_type: str
    protocol_version: str
    firmware_version: Optional[str]
    capabilities: List[str]
    status: str
    last_seen: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class Command:
    command_id: str
    conversation_id: Optional[str]
    device_id: str
    capability: str
    params: Dict[str, Any]
    policy: str
    state: str
    created_at: str
    updated_at: str
    error: Optional[str]
    result: Optional[Dict[str, Any]]
    parent_command_id: Optional[str]
