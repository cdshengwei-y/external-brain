from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from typing import Any, Dict, Optional

from .errors import HubError, ValidationError
from .models import TERMINAL_COMMAND_STATES
from agent_gateway.portable import PortableRequestService
from .service import HubService


class HubRequestHandler(BaseHTTPRequestHandler):
    server_version = "SmartHub/0.1"

    @property
    def service(self) -> HubService:
        return self.server.hub_service  # type: ignore[attr-defined]

    @property
    def portable_service(self) -> PortableRequestService:
        return self.server.portable_service  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/v1/health":
                self._send_json(200, self.service.health())
                return
            if parsed.path.startswith("/api/v1/portable/v1/requests/"):
                request_id = parsed.path.rstrip("/").split("/")[-1]
                self._send_json(200, self.portable_service.get(request_id))
                return
            if parsed.path == "/api/v1/devices":
                self._send_json(200, {"devices": self.service.list_devices()})
                return
            if parsed.path.startswith("/api/v1/devices/") and parsed.path.endswith("/state"):
                device_id = parsed.path.strip("/").split("/")[-2]
                self._send_json(200, self.service.get_device_state(device_id))
                return
            if parsed.path.startswith("/api/v1/devices/"):
                device_id = parsed.path.split("/")[-1]
                self._send_json(200, self.service.get_device(device_id))
                return
            if parsed.path.startswith("/api/v1/commands/") and parsed.path.endswith("/wait"):
                parts = parsed.path.strip("/").split("/")
                command_id = parts[-2]
                query = parse_qs(parsed.query)
                timeout_ms = self._query_int(query, "timeout_ms", 30000)
                if timeout_ms < 0 or timeout_ms > 60000:
                    raise ValidationError("timeout_ms 必须在 0 到 60000 之间")
                command = self.service.wait_for_command(command_id, timeout_ms / 1000)
                completed = command["state"] in TERMINAL_COMMAND_STATES
                self._send_json(
                    200,
                    {
                        "command": command,
                        "completed": completed,
                        "timed_out": not completed,
                    },
                )
                return
            if parsed.path.startswith("/api/v1/commands/"):
                command_id = parsed.path.split("/")[-1]
                self._send_json(200, self.service.get_command(command_id))
                return
            if parsed.path == "/api/v1/events":
                query = parse_qs(parsed.query)
                since_id = self._query_int(query, "since_id", 0)
                limit = self._query_int(query, "limit", 100)
                if "text/event-stream" in self.headers.get("Accept", ""):
                    self._stream_events(since_id)
                else:
                    self._send_json(
                        200,
                        {"events": self.service.list_events(since_id, limit)},
                    )
                return
            self._send_error_json(404, "接口不存在")
        except HubError as error:
            self._send_error_json(error.status_code, error.message)
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._send_error_json(500, f"中枢内部错误: {error}")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/v1/portable/v1/media":
                data = self._read_binary_body()
                media = self.portable_service.upload_media(
                    portable_device_id=self.headers.get("X-Portable-Device-Id", ""),
                    media_type=self.headers.get("X-Media-Type", ""),
                    audio_format=self.headers.get("X-Audio-Format"),
                    content_type=self.headers.get("Content-Type"),
                    data=data,
                )
                self._send_json(201, media)
                return
            payload = self._read_json_body()
            if parsed.path == "/api/v1/portable/v1/requests":
                request = self.portable_service.submit(payload)
                self._send_json(202, request)
                return
            if (
                parsed.path.startswith("/api/v1/portable/v1/requests/")
                and parsed.path.endswith("/confirm")
            ):
                parts = parsed.path.strip("/").split("/")
                request_id = parts[-2]
                plan_id = payload.get("plan_id")
                if not isinstance(plan_id, str) or not plan_id.strip():
                    raise ValidationError("plan_id 必须是非空字符串")
                request = self.portable_service.confirm(request_id, plan_id.strip())
                self._send_json(202, request)
                return
            if parsed.path == "/internal/v1/devices/register":
                self._send_json(201, self.service.register_device(payload))
                return
            if parsed.path.startswith("/internal/v1/devices/"):
                self._handle_internal_device_post(parsed.path, payload)
                return
            if parsed.path.startswith("/api/v1/devices/") and parsed.path.endswith("/commands"):
                device_id = parsed.path.split("/")[-2]
                command = self.service.submit_command(device_id, payload)
                self._send_json(202, command)
                return
            if parsed.path.startswith("/api/v1/commands/") and parsed.path.endswith("/cancel"):
                command_id = parsed.path.split("/")[-2]
                self._send_json(202, self.service.cancel_command(command_id))
                return
            self._send_error_json(404, "接口不存在")
        except HubError as error:
            self._send_error_json(error.status_code, error.message)
        except json.JSONDecodeError:
            self._send_error_json(400, "请求体必须是有效 JSON")
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._send_error_json(500, f"中枢内部错误: {error}")

    def _handle_internal_device_post(self, path: str, payload: Dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 5:
            self._send_error_json(404, "接口不存在")
            return
        device_id = parts[3]
        action = parts[4]
        if action == "availability":
            self._send_json(200, self.service.update_availability(device_id, payload))
        elif action == "state":
            self._send_json(202, self.service.ingest_state(device_id, payload))
        elif action == "events":
            self._send_json(202, self.service.ingest_device_event(device_id, payload))
        elif action == "command-acks":
            command_id = payload.get("command_id")
            if not command_id:
                raise ValidationError("command-acks 缺少 command_id")
            self._send_json(
                200,
                self.service.acknowledge_command(device_id, command_id, payload),
            )
        else:
            self._send_error_json(404, "接口不存在")

    def _stream_events(self, since_id: int) -> None:
        subscription = self.service.subscribe_events()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for event in self.service.list_events(since_id, 500):
                self._write_sse(event)
            while True:
                try:
                    event = subscription.queue.get(timeout=15)
                    self._write_sse(event)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.service.unsubscribe_events(subscription)

    def _write_sse(self, event: Dict[str, Any]) -> None:
        body = json.dumps(event, ensure_ascii=False)
        message = f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {body}\n\n"
        self.wfile.write(message.encode("utf-8"))
        self.wfile.flush()

    def _read_json_body(self) -> Dict[str, Any]:
        raw = self._read_body(1024 * 1024)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _read_binary_body(self) -> bytes:
        return self._read_body(10 * 1024 * 1024)

    def _read_body(self, max_length: int) -> bytes:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValidationError("缺少 Content-Length")
        try:
            length = int(length_header)
        except ValueError as error:
            raise ValidationError("Content-Length 必须是整数") from error
        if length < 0:
            raise ValidationError("Content-Length 不能为负数")
        if length > max_length:
            raise ValidationError(f"请求体不能超过 {max_length // (1024 * 1024)} MB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValidationError("请求体长度与 Content-Length 不一致")
        return raw

    @staticmethod
    def _query_int(query: Dict[str, Any], key: str, default: int) -> int:
        try:
            return int(query.get(key, [default])[0])
        except (TypeError, ValueError):
            raise ValidationError(f"{key} 必须是整数")

    def _send_json(self, status_code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status_code: int, message: str) -> None:
        self._send_json(status_code, {"error": message})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[hub] {format % args}")


class HubHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Any,
        service: HubService,
        portable_service: Optional[PortableRequestService] = None,
    ):
        super().__init__(server_address, HubRequestHandler)
        self.hub_service = service
        self.portable_service = portable_service or PortableRequestService(service)


def serve(
    service: HubService,
    host: str = "127.0.0.1",
    port: int = 8787,
    portable_service: Optional[PortableRequestService] = None,
) -> None:
    owns_portable_service = portable_service is None
    server = HubHttpServer((host, port), service, portable_service)
    print(f"智能中枢 API 已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止智能中枢...")
    finally:
        server.server_close()
        if owns_portable_service:
            server.portable_service.close()
