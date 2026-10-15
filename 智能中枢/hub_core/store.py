from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_loads(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


class HubStore:
    """SQLite-backed state store for devices, commands and events."""

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_type TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    firmware_version TEXT,
                    capabilities TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    metadata TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS device_states (
                    device_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );

                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    device_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    params TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    result TEXT,
                    parent_command_id TEXT,
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );

                CREATE INDEX IF NOT EXISTS idx_commands_device_state
                    ON commands(device_id, state, created_at);

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    device_id TEXT,
                    command_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON events(event_id);

                CREATE TABLE IF NOT EXISTS portable_requests (
                    request_id TEXT PRIMARY KEY,
                    portable_device_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL DEFAULT '1.0',
                    source TEXT NOT NULL DEFAULT 'portable',
                    input_type TEXT NOT NULL,
                    text TEXT,
                    transcript TEXT,
                    media_id TEXT,
                    target_device_id TEXT,
                    state TEXT NOT NULL,
                    intent TEXT,
                    plan_id TEXT,
                    command_id TEXT,
                    response_text TEXT,
                    result TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_portable_requests_conversation
                    ON portable_requests(conversation_id, created_at);

                CREATE TABLE IF NOT EXISTS portable_media (
                    media_id TEXT PRIMARY KEY,
                    portable_device_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    audio_format TEXT,
                    content_type TEXT,
                    size_bytes INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_portable_media_device
                    ON portable_media(portable_device_id, created_at);
                """
            )
            self._ensure_column("portable_requests", "plan_id", "TEXT")
            self._ensure_column(
                "portable_requests", "protocol_version", "TEXT NOT NULL DEFAULT '1.0'"
            )
            self._ensure_column(
                "portable_requests", "source", "TEXT NOT NULL DEFAULT 'portable'"
            )
            self._ensure_column("portable_requests", "transcript", "TEXT")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row[1]
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def register_device(
        self,
        device_id: str,
        device_type: str,
        protocol_version: str,
        firmware_version: Optional[str],
        capabilities: Iterable[str],
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "online",
    ) -> Dict[str, Any]:
        now = utc_now()
        capabilities_json = json.dumps(sorted(set(capabilities)), ensure_ascii=False)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO devices (
                    device_id, device_type, protocol_version, firmware_version,
                    capabilities, status, last_seen, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_type=excluded.device_type,
                    protocol_version=excluded.protocol_version,
                    firmware_version=excluded.firmware_version,
                    capabilities=excluded.capabilities,
                    status=excluded.status,
                    last_seen=excluded.last_seen,
                    metadata=excluded.metadata
                """,
                (
                    device_id,
                    device_type,
                    protocol_version,
                    firmware_version,
                    capabilities_json,
                    status,
                    now,
                    metadata_json,
                ),
            )
            return self.get_device(device_id)

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._device_from_row(row) if row else None

    def list_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM devices ORDER BY device_id"
            ).fetchall()
        return [self._device_from_row(row) for row in rows]

    def upsert_device_state(
        self,
        device_id: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        updated_at = utc_now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO device_states (device_id, state, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (device_id, json.dumps(state, ensure_ascii=False), updated_at),
            )
        return self.get_device_state(device_id)

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM device_states WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "device_id": row["device_id"],
            "state": _json_loads(row["state"], {}),
            "updated_at": row["updated_at"],
        }

    def update_device_status(self, device_id: str, status: str) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                "UPDATE devices SET status = ?, last_seen = ? WHERE device_id = ?",
                (status, now, device_id),
            )
            return self.get_device(device_id)

    def touch_device(self, device_id: str, status: str = "online") -> Optional[Dict[str, Any]]:
        return self.update_device_status(device_id, status)

    def mark_all_devices_offline(self) -> int:
        """Invalidate persisted online flags until MQTT confirms liveness again."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE devices SET status = 'offline' WHERE status = 'online'"
            )
            return cursor.rowcount

    def create_command(
        self,
        command_id: str,
        conversation_id: Optional[str],
        device_id: str,
        capability: str,
        params: Dict[str, Any],
        policy: str,
        state: str,
        parent_command_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO commands (
                    command_id, conversation_id, device_id, capability, params,
                    policy, state, created_at, updated_at, error, result,
                    parent_command_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    command_id,
                    conversation_id,
                    device_id,
                    capability,
                    json.dumps(params, ensure_ascii=False),
                    policy,
                    state,
                    now,
                    now,
                    parent_command_id,
                ),
            )
            return self.get_command(command_id)

    def get_command(self, command_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return self._command_from_row(row) if row else None

    def list_active_commands(self, device_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM commands
                WHERE device_id = ?
                  AND state IN ('accepted', 'queued', 'running', 'cancel_requested')
                ORDER BY created_at, command_id
                """,
                (device_id,),
            ).fetchall()
        return [self._command_from_row(row) for row in rows]

    def list_queued_commands(self, device_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM commands
                WHERE device_id = ? AND state = 'queued'
                ORDER BY created_at, command_id
                """,
                (device_id,),
            ).fetchall()
        return [self._command_from_row(row) for row in rows]

    def update_command(
        self,
        command_id: str,
        state: Optional[str] = None,
        error: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        command = self.get_command(command_id)
        if command is None:
            return None
        next_state = state if state is not None else command["state"]
        next_error = error if error is not None else command["error"]
        next_result = result if result is not None else command["result"]
        with self._lock:
            self._connection.execute(
                """
                UPDATE commands
                SET state = ?, updated_at = ?, error = ?, result = ?
                WHERE command_id = ?
                """,
                (
                    next_state,
                    utc_now(),
                    next_error,
                    json.dumps(next_result, ensure_ascii=False)
                    if next_result is not None
                    else None,
                    command_id,
                ),
            )
            return self.get_command(command_id)

    def add_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        device_id: Optional[str] = None,
        command_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        created_at = utc_now()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO events (
                    event_type, device_id, command_id, payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    device_id,
                    command_id,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                ),
            )
            event_id = cursor.lastrowid
        return {
            "event_id": event_id,
            "event_type": event_type,
            "device_id": device_id,
            "command_id": command_id,
            "payload": payload,
            "created_at": created_at,
        }

    def list_events(self, since_id: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM events
                WHERE event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (since_id, safe_limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def create_portable_request(
        self,
        request_id: str,
        portable_device_id: str,
        conversation_id: str,
        input_type: str,
        text: Optional[str],
        media_id: Optional[str],
        target_device_id: Optional[str],
        transcript: Optional[str] = None,
        state: str = "received",
        protocol_version: str = "1.0",
        source: str = "portable",
    ) -> Dict[str, Any]:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO portable_requests (
                    request_id, portable_device_id, conversation_id, protocol_version,
                    source, input_type,
                    text, transcript, media_id, target_device_id, state, intent, plan_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    request_id,
                    portable_device_id,
                    conversation_id,
                    protocol_version,
                    source,
                    input_type,
                    text,
                    transcript,
                    media_id,
                    target_device_id,
                    state,
                    now,
                    now,
                ),
            )
            return self.get_portable_request(request_id)  # type: ignore[return-value]

    def get_portable_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM portable_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._portable_request_from_row(row) if row else None

    def update_portable_request(
        self,
        request_id: str,
        state: Optional[str] = None,
        intent: Optional[Dict[str, Any]] = None,
        transcript: Optional[str] = None,
        plan_id: Optional[str] = None,
        command_id: Optional[str] = None,
        response_text: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        retryable: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        request = self.get_portable_request(request_id)
        if request is None:
            return None
        next_state = state if state is not None else request["state"]
        next_intent = intent if intent is not None else request["intent"]
        next_transcript = transcript if transcript is not None else request["transcript"]
        next_plan_id = plan_id if plan_id is not None else request["plan_id"]
        next_command_id = command_id if command_id is not None else request["command_id"]
        next_response_text = (
            response_text if response_text is not None else request["response_text"]
        )
        next_result = result if result is not None else request["result"]
        next_error_code = error_code if error_code is not None else request["error_code"]
        next_error_message = (
            error_message if error_message is not None else request["error_message"]
        )
        next_retryable = retryable if retryable is not None else request["retryable"]
        with self._lock:
            self._connection.execute(
                """
                UPDATE portable_requests
                SET state = ?, intent = ?, transcript = ?, plan_id = ?, command_id = ?, response_text = ?,
                    result = ?, error_code = ?, error_message = ?, retryable = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    next_state,
                    json.dumps(next_intent, ensure_ascii=False)
                    if next_intent is not None
                    else None,
                    next_transcript,
                    next_plan_id,
                    next_command_id,
                    next_response_text,
                    json.dumps(next_result, ensure_ascii=False)
                    if next_result is not None
                    else None,
                    next_error_code,
                    next_error_message,
                    1 if next_retryable else 0,
                    utc_now(),
                    request_id,
                ),
            )
            return self.get_portable_request(request_id)

    def create_portable_media(
        self,
        media_id: str,
        portable_device_id: str,
        media_type: str,
        audio_format: Optional[str],
        content_type: Optional[str],
        data: bytes,
        state: str = "stored",
    ) -> Dict[str, Any]:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO portable_media (
                    media_id, portable_device_id, media_type, audio_format,
                    content_type, size_bytes, data, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_id,
                    portable_device_id,
                    media_type,
                    audio_format,
                    content_type,
                    len(data),
                    sqlite3.Binary(data),
                    state,
                    now,
                    now,
                ),
            )
            return self.get_portable_media(media_id)  # type: ignore[return-value]

    def get_portable_media(self, media_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM portable_media WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        return self._portable_media_from_row(row) if row else None

    @staticmethod
    def _device_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "device_id": row["device_id"],
            "device_type": row["device_type"],
            "protocol_version": row["protocol_version"],
            "firmware_version": row["firmware_version"],
            "capabilities": _json_loads(row["capabilities"], []),
            "status": row["status"],
            "last_seen": row["last_seen"],
            "metadata": _json_loads(row["metadata"], {}),
        }

    @staticmethod
    def _command_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "command_id": row["command_id"],
            "conversation_id": row["conversation_id"],
            "device_id": row["device_id"],
            "capability": row["capability"],
            "params": _json_loads(row["params"], {}),
            "policy": row["policy"],
            "state": row["state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "result": _json_loads(row["result"], None),
            "parent_command_id": row["parent_command_id"],
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "device_id": row["device_id"],
            "command_id": row["command_id"],
            "payload": _json_loads(row["payload"], {}),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _portable_request_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "portable_device_id": row["portable_device_id"],
            "conversation_id": row["conversation_id"],
            "protocol_version": row["protocol_version"],
            "source": row["source"],
            "input_type": row["input_type"],
            "text": row["text"],
            "transcript": row["transcript"],
            "media_id": row["media_id"],
            "target_device_id": row["target_device_id"],
            "state": row["state"],
            "intent": _json_loads(row["intent"], None),
            "plan_id": row["plan_id"],
            "command_id": row["command_id"],
            "response_text": row["response_text"],
            "result": _json_loads(row["result"], None),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "retryable": bool(row["retryable"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _portable_media_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "media_id": row["media_id"],
            "portable_device_id": row["portable_device_id"],
            "media_type": row["media_type"],
            "audio_format": row["audio_format"],
            "content_type": row["content_type"],
            "size_bytes": row["size_bytes"],
            "data": bytes(row["data"]),
            "state": row["state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
