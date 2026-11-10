#!/usr/bin/env python3
"""验收录音豆真实 MQTT 在线状态和三条硬件命令闭环。

脚本不会把历史 state 当作在线证明，也不会在计划缺少 plan_id 时绕过 RK3588
直接调用桥。只有桥返回真实设备的 ``succeeded`` ACK，命令才算通过。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


PROTOCOL_VERSION = "1.0"
CAPABILITIES = (
    ("查询录音豆状态", "status.read", False),
    ("开始录音", "record.start", True),
    ("停止录音", "record.stop", True),
)


@dataclass
class HttpResult:
    status_code: Optional[int]
    body: Dict[str, Any]
    error: Optional[str] = None


class ClosedLoopVerifier:
    def __init__(
        self,
        rk_url: str,
        device_id: str,
        timeout: float,
        conversation_id: str,
        mqtt_host: Optional[str],
        mqtt_port: int,
        availability_wait: float,
    ):
        self.rk_url = rk_url.rstrip("/")
        self.device_id = device_id
        self.timeout = timeout
        self.conversation_id = conversation_id
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.availability_wait = availability_wait
        self.results: list[tuple[str, bool, str]] = []

    def run(self, execute_actions: bool) -> int:
        if self.mqtt_host:
            self._check_availability()
        self._check_gateway()
        for label, capability, mutating in CAPABILITIES:
            plan = self._plan(label, capability)
            if plan is None:
                continue
            if capability == "status.read" or execute_actions:
                self._dispatch(label, plan)
            elif mutating:
                print(f"[SKIP] {label}执行：未指定 --execute-actions，出于安全原因未执行")
        self._print_summary()
        return 0 if all(passed for _, passed, _ in self.results) else 1

    def _check_availability(self) -> None:
        topic = f"hub/v1/devices/{self.device_id}/availability"
        try:
            import paho.mqtt.client as mqtt
            from threading import Event

            received = Event()
            holder: Dict[str, Any] = {}

            def on_message(_client: Any, _userdata: Any, message: Any) -> None:
                try:
                    holder["payload"] = json.loads(message.payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    holder["payload"] = {"raw": message.payload.decode("utf-8", "replace")}
                received.set()

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"verify-{uuid.uuid4().hex[:12]}")
            client.on_message = on_message
            client.connect(self.mqtt_host, self.mqtt_port, keepalive=15)
            client.subscribe(topic, qos=1)
            client.loop_start()
            received.wait(self.availability_wait)
            client.loop_stop()
            client.disconnect()
            payload = holder.get("payload")
            passed = isinstance(payload, dict) and payload.get("status") == "online"
            self._record("MQTT availability: online", passed, json.dumps(payload or {}, ensure_ascii=False))
        except Exception as error:
            self._record("MQTT availability: online", False, f"无法读取 {self.mqtt_host}:{self.mqtt_port}: {error}")

    def _check_gateway(self) -> None:
        result = self._request("GET", "/api/hub/v1/health")
        devices = result.body.get("devices")
        passed = (
            result.status_code == 200
            and result.body.get("status") == "ok"
            and isinstance(devices, list)
            and any(
                isinstance(device, dict)
                and device.get("device_id") == self.device_id
                and {"record.start", "record.stop", "status.read"}.issubset(
                    set(device.get("capabilities", []))
                )
                for device in devices
            )
        )
        self._record("RK3588 设备目录和能力", passed, self._describe(result))

    def _plan(self, label: str, capability: str) -> Optional[Dict[str, Any]]:
        request_id = self._new_id("plan")
        result = self._request(
            "POST",
            "/api/route",
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "conversation_id": self.conversation_id,
                "source": "verification",
                "task": "auto",
                "input": label,
                "target_device_id": self.device_id,
            },
        )
        command = result.body.get("command")
        if not isinstance(command, dict):
            command = {}
        plan_id = result.body.get("plan_id")
        passed = (
            result.status_code == 200
            and result.body.get("status") == "planned"
            and isinstance(plan_id, str)
            and bool(plan_id.strip())
            and command.get("device_id") == self.device_id
            and command.get("capability") == capability
            and isinstance(command.get("params"), dict)
        )
        self._record(label + "规划", passed, self._describe(result))
        if not passed:
            return None
        return {
            "plan_id": plan_id,
            "request_id": request_id,
            "command": command,
        }

    def _dispatch(self, label: str, plan: Dict[str, Any]) -> None:
        result = self._request(
            "POST",
            "/api/hub/v1/commands",
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": self._new_id("execute"),
                "conversation_id": self.conversation_id,
                "source": "verification",
                "plan_id": plan["plan_id"],
                "target_device_id": self.device_id,
                "dispatch": True,
            },
        )
        dispatch = result.body.get("dispatch")
        passed = (
            result.status_code == 200
            and result.body.get("status") == "succeeded"
            and isinstance(dispatch, dict)
            and dispatch.get("ok") is True
            and dispatch.get("state") == "succeeded"
        )
        self._record(label + "执行（真实 ACK）", passed, self._describe(result))

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> HttpResult:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.rk_url + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
                return HttpResult(response.status, parsed if isinstance(parsed, dict) else {})
        except urllib.error.HTTPError as error:
            try:
                parsed = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {}
            detail = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else str(error)
            return HttpResult(error.code, parsed if isinstance(parsed, dict) else {}, detail)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            return HttpResult(None, {}, str(error))

    def _record(self, name: str, passed: bool, detail: str) -> None:
        self.results.append((name, passed, detail))

    def _print_summary(self) -> None:
        print("=== 录音豆真实在线控制闭环验收 ===")
        for name, passed, detail in self.results:
            print(f"[{ 'PASS' if passed else 'FAIL' }] {name}：{detail}")
        print(f"通过 {sum(p for _, p, _ in self.results)}/{len(self.results)} 项")

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4()}"

    @staticmethod
    def _describe(result: HttpResult) -> str:
        if result.error:
            return f"HTTP {result.status_code or '无响应'}，{result.error}"
        compact = json.dumps(result.body, ensure_ascii=False, separators=(",", ":"))
        return compact if len(compact) <= 360 else compact[:357] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(description="验收录音豆真实 MQTT 控制闭环")
    parser.add_argument("--rk-url", default="http://192.168.0.140:8095")
    parser.add_argument("--device-id", default="bean_ye_1")
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--conversation-id", default=f"closed-loop-{uuid.uuid4()}")
    parser.add_argument("--mqtt-host", default="192.168.0.107")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--availability-wait", type=float, default=5.0)
    parser.add_argument(
        "--execute-actions",
        action="store_true",
        help="执行 record.start 和 record.stop；不指定时只执行 status.read",
    )
    args = parser.parse_args()
    return ClosedLoopVerifier(
        args.rk_url,
        args.device_id,
        args.timeout,
        args.conversation_id,
        args.mqtt_host,
        args.mqtt_port,
        args.availability_wait,
    ).run(
        execute_actions=args.execute_actions
    )


if __name__ == "__main__":
    sys.exit(main())
