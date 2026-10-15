from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from .errors import ConflictError, HubError, NotFoundError, ValidationError
from .models import TERMINAL_COMMAND_STATES
from .service import HubService


class HttpMqttBridge:
    """Translate RK3588 HTTP commands into the existing HubService/MQTT flow."""

    PROTOCOL_VERSION = "1.0"

    def __init__(self, service: HubService, wait_timeout_seconds: float = 30.0):
        if wait_timeout_seconds < 0 or wait_timeout_seconds > 60:
            raise ValueError("wait_timeout_seconds 必须在 0 到 60 之间")
        self.service = service
        self.wait_timeout_seconds = wait_timeout_seconds

    def execute(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        request_id = self._required_string(payload, "request_id")
        protocol_version = payload.get("protocol_version", self.PROTOCOL_VERSION)
        if protocol_version != self.PROTOCOL_VERSION:
            raise ValidationError("protocol_version 必须是 1.0")
        source = payload.get("source") or "rk3588"
        if not isinstance(source, str) or not source.strip():
            raise ValidationError("source 必须是非空字符串")

        command_payload = payload.get("command")
        if not isinstance(command_payload, dict):
            raise ValidationError("command 必须是 JSON 对象")

        device_id = self._required_string(command_payload, "device_id")
        if "action" in command_payload and "capability" in command_payload:
            raise ValidationError("command 不能同时提供 action 和 capability")
        capability = command_payload.get("capability")
        if capability is None:
            capability = command_payload.get("action")
        if not isinstance(capability, str) or not capability.strip():
            raise ValidationError("command.capability 必须是非空字符串")
        capability = capability.strip()

        params = command_payload.get("params") or {}
        if not isinstance(params, dict):
            raise ValidationError("command.params 必须是 JSON 对象")

        policy = payload.get("policy") or "queue"
        conversation_id = payload.get("conversation_id") or f"bridge:{source}"
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValidationError("conversation_id 必须是非空字符串")

        command = self._get_or_submit_command(
            request_id=request_id,
            device_id=device_id,
            capability=capability,
            params=params,
            policy=policy,
            conversation_id=conversation_id,
        )
        final_command = self.service.wait_for_command(
            command["command_id"],
            timeout_seconds=self.wait_timeout_seconds,
        )
        return self._format_result(
            request_id=request_id,
            source=source.strip(),
            protocol_version=protocol_version,
            command=final_command,
        )

    def _get_or_submit_command(
        self,
        request_id: str,
        device_id: str,
        capability: str,
        params: Dict[str, Any],
        policy: str,
        conversation_id: str,
    ) -> Dict[str, Any]:
        try:
            existing = self.service.get_command(request_id)
        except NotFoundError:
            existing = None

        if existing is not None:
            if (
                existing["device_id"] != device_id
                or existing["capability"] != capability
                or existing["params"] != params
            ):
                raise ConflictError("request_id 已被其他命令使用")
            return existing

        device = self.service.get_device(device_id)
        if device.get("status") != "online":
            raise HubError(f"设备当前不在线: {device_id}", status_code=503)

        return self.service.submit_command(
            device_id,
            {
                "command_id": request_id,
                "conversation_id": conversation_id,
                "capability": capability,
                "params": params,
                "policy": policy,
            },
        )

    @staticmethod
    def _format_result(
        request_id: str,
        source: str,
        protocol_version: str,
        command: Dict[str, Any],
    ) -> Tuple[int, Dict[str, Any]]:
        state = command["state"]
        result = command.get("result")
        if not isinstance(result, dict):
            result = {}
        response: Dict[str, Any] = {
            "ok": state == "succeeded",
            "protocol_version": protocol_version,
            "request_id": request_id,
            "source": source,
            "command_id": command["command_id"],
            "device_id": command["device_id"],
            "capability": command["capability"],
            "state": state,
        }

        if state == "succeeded":
            response["result"] = result
            response["message"] = result.get("message", "设备已完成命令")
            return 200, response

        if state not in TERMINAL_COMMAND_STATES:
            response.update(
                {
                    "pending": True,
                    "timed_out": True,
                    "message": "命令已发送，等待真实设备返回最终结果",
                }
            )
            return 202, response

        error = command.get("error") or result.get("error") or "设备未能完成命令"
        response.update({"error": error, "message": error, "result": result})
        if state == "expired":
            return 504, response
        if state in {"cancelled", "superseded"}:
            return 409, response
        return 502, response

    @staticmethod
    def _required_string(payload: Dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} 必须是非空字符串")
        return value.strip()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "SmartHubHttpMqttBridge/0.1"

    @property
    def bridge(self) -> HttpMqttBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def service(self) -> HubService:
        return self.server.hub_service  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "http-mqtt-bridge",
                    "hub": self.service.health(),
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "接口不存在"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/command":
            self._send_json(404, {"ok": False, "error": "接口不存在"})
            return

        try:
            payload = self._read_json_body()
            status_code, response = self.bridge.execute(payload)
            self._send_json(status_code, response)
        except HubError as error:
            self._send_json(
                error.status_code,
                {"ok": False, "error": error.message},
            )
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "请求体必须是有效 JSON"})
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._send_json(500, {"ok": False, "error": f"桥接服务内部错误: {error}"})

    def _read_json_body(self) -> Dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValidationError("缺少 Content-Length")
        try:
            length = int(length_header)
        except ValueError as error:
            raise ValidationError("Content-Length 必须是整数") from error
        if length < 0 or length > 1024 * 1024:
            raise ValidationError("请求体不能超过 1 MB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValidationError("请求体长度与 Content-Length 不一致")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[http-mqtt-bridge] {format % args}")


class BridgeHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Any,
        service: HubService,
        wait_timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(server_address, BridgeRequestHandler)
        self.hub_service = service
        self.bridge = HttpMqttBridge(service, wait_timeout_seconds)


def serve_bridge(
    service: HubService,
    host: str = "0.0.0.0",
    port: int = 19700,
    wait_timeout_seconds: float = 30.0,
) -> None:
    server = BridgeHttpServer(
        (host, port),
        service,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    print(f"HTTP-MQTT 兼容桥已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 HTTP-MQTT 兼容桥...")
    finally:
        server.server_close()
