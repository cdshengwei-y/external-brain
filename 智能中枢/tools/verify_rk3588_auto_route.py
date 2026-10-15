#!/usr/bin/env python3
"""Verify the real RK3588 automatic-routing and hardware-plan contract."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


PROTOCOL_VERSION = "1.0"


@dataclass
class HttpResult:
    status_code: Optional[int]
    body: Dict[str, Any]
    elapsed_ms: int
    error: Optional[str] = None


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


class RealRouteVerifier:
    def __init__(
        self,
        rk_base_url: str,
        device_id: str,
        timeout_seconds: float,
        conversation_id: str,
    ) -> None:
        self.rk_base_url = rk_base_url.rstrip("/")
        self.device_id = device_id
        self.timeout_seconds = timeout_seconds
        self.conversation_id = conversation_id
        self.results: list[CheckResult] = []
        self.status_plan: Optional[Dict[str, Any]] = None

    def run(self, execute_status: bool) -> int:
        self._check_health()
        self._check_mode()
        self._check_chat("你好")
        self._check_chat("你能做什么")
        self._check_hardware_plan(
            name="自然语言查询状态",
            text="帮我看看录音豆现在的状态",
            expected_capability="status.read",
            save_status_plan=True,
        )
        self._check_hardware_plan(
            name="自然语言开始录音规划",
            text="请帮我开始录音",
            expected_capability="record.start",
        )
        self._check_hardware_plan(
            name="自然语言停止录音规划",
            text="请帮我停止录音",
            expected_capability="record.stop",
        )
        if execute_status:
            self._execute_status_plan()
        self._print_summary()
        return 0 if all(result.passed for result in self.results) else 1

    def _check_health(self) -> None:
        result = self._request("GET", "/api/health")
        passed = (
            result.status_code == 200
            and result.body.get("status") == "ok"
            and result.body.get("protocol_version") == PROTOCOL_VERSION
        )
        detail = self._describe(result)
        self._record("RK3588 健康检查", passed, detail)

        result = self._request("GET", "/api/hub/v1/health")
        devices = result.body.get("devices")
        passed = (
            result.status_code == 200
            and result.body.get("status") == "ok"
            and isinstance(devices, list)
            and any(
                isinstance(device, dict)
                and device.get("device_id") == self.device_id
                for device in devices
            )
        )
        detail = self._describe(result)
        self._record("RK3588 硬件网关和设备目录", passed, detail)

    def _check_mode(self) -> None:
        result = self._request("GET", "/api/mode")
        passed = result.status_code == 200 and isinstance(result.body.get("mode"), str)
        self._record("RK3588 模式可读", passed, self._describe(result))

    def _check_chat(self, text: str) -> None:
        result = self._route(text)
        route = result.body.get("route")
        reply = result.body.get("reply") or result.body.get("content")
        passed = (
            result.status_code == 200
            and route == "chat"
            and isinstance(reply, str)
            and bool(reply.strip())
        )
        self._record(
            f"聊天路由：{text}",
            passed,
            self._describe(result),
        )

    def _check_hardware_plan(
        self,
        name: str,
        text: str,
        expected_capability: str,
        save_status_plan: bool = False,
    ) -> None:
        request_id = self._new_id("plan")
        result = self._route(text, request_id=request_id)
        command = result.body.get("command")
        if not isinstance(command, dict):
            command = {}
        capability = command.get("capability")
        plan_id = result.body.get("plan_id")
        route = result.body.get("route")
        passed = (
            result.status_code == 200
            and route in {"command", None}
            and result.body.get("status") == "planned"
            and isinstance(plan_id, str)
            and bool(plan_id.strip())
            and capability == expected_capability
            and command.get("device_id") == self.device_id
            and isinstance(command.get("params"), dict)
        )
        if save_status_plan and passed:
            self.status_plan = {
                "plan_id": plan_id,
                "request_id": request_id,
                "conversation_id": self.conversation_id,
            }
        detail = self._describe(result)
        if result.status_code == 200 and result.body.get("dispatch"):
            detail += "；错误：规划响应不应包含执行结果"
            passed = False
        self._record(name, passed, detail)

    def _execute_status_plan(self) -> None:
        if self.status_plan is None:
            self._record(
                "真实 status.read 执行",
                False,
                "没有可用的 status.read 计划，跳过真实执行",
            )
            return
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": self._new_id("execute"),
            "conversation_id": self.conversation_id,
            "source": "verification",
            "plan_id": self.status_plan["plan_id"],
            "target_device_id": self.device_id,
            "dispatch": True,
        }
        result = self._request(
            "POST",
            "/api/hub/v1/commands",
            payload,
        )
        dispatch = result.body.get("dispatch")
        passed = (
            result.status_code == 200
            and result.body.get("status") == "succeeded"
            and isinstance(dispatch, dict)
            and dispatch.get("ok") is True
            and dispatch.get("state") == "succeeded"
        )
        self._record("真实 status.read 执行", passed, self._describe(result))

    def _route(self, text: str, request_id: Optional[str] = None) -> HttpResult:
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id or self._new_id("chat"),
            "conversation_id": self.conversation_id,
            "source": "verification",
            "task": "auto",
            "input": text,
            "target_device_id": self.device_id,
        }
        return self._request("POST", "/api/route", payload)

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> HttpResult:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.rk_base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw_body = response.read().decode("utf-8")
                parsed = json.loads(raw_body)
                if not isinstance(parsed, dict):
                    parsed = {"raw": parsed}
                return HttpResult(
                    status_code=response.status,
                    body=parsed,
                    elapsed_ms=self._elapsed_ms(started),
                )
        except urllib.error.HTTPError as error:
            try:
                raw_body = error.read().decode("utf-8")
                parsed = json.loads(raw_body)
                if not isinstance(parsed, dict):
                    parsed = {"raw": parsed}
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {}
            return HttpResult(
                status_code=error.code,
                body=parsed,
                elapsed_ms=self._elapsed_ms(started),
                error=str(error),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return HttpResult(
                status_code=None,
                body={},
                elapsed_ms=self._elapsed_ms(started),
                error=str(error),
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            return HttpResult(
                status_code=None,
                body={},
                elapsed_ms=self._elapsed_ms(started),
                error=f"响应不是有效 JSON: {error}",
            )

    def _record(self, name: str, passed: bool, detail: str) -> None:
        self.results.append(CheckResult(name, passed, detail))

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4()}"

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _describe(result: HttpResult) -> str:
        detail = f"HTTP {result.status_code or '无响应'}，{result.elapsed_ms} ms"
        if result.error:
            return f"{detail}，{result.error}"
        compact = json.dumps(result.body, ensure_ascii=False, separators=(",", ":"))
        if len(compact) > 280:
            compact = compact[:277] + "..."
        return f"{detail}，{compact}"

    def _print_summary(self) -> None:
        print("\n=== RK3588 自动路由真实设备验收结果 ===")
        for result in self.results:
            mark = "PASS" if result.passed else "FAIL"
            print(f"[{mark}] {result.name}：{result.detail}")
        passed = sum(result.passed for result in self.results)
        print(f"\n通过 {passed}/{len(self.results)} 项")
        if any(not result.passed for result in self.results):
            print("存在未通过项；本脚本没有执行 record.start 或 record.stop。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证真实 RK3588 自动路由和录音豆硬件链路",
    )
    parser.add_argument(
        "--rk-url",
        default="http://192.168.0.140:8095",
        help="RK3588 服务地址",
    )
    parser.add_argument(
        "--device-id",
        default="bean_ye_1",
        help="真实设备 ID",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="每个请求的超时时间，默认 15 秒",
    )
    parser.add_argument(
        "--conversation-id",
        default=f"verification-{uuid.uuid4()}",
        help="验收会话 ID",
    )
    parser.add_argument(
        "--execute-status",
        action="store_true",
        help="在规划成功后执行一次真实 status.read；不会执行开始或停止录音",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    verifier = RealRouteVerifier(
        rk_base_url=args.rk_url,
        device_id=args.device_id,
        timeout_seconds=args.timeout,
        conversation_id=args.conversation_id,
    )
    return verifier.run(execute_status=args.execute_status)


if __name__ == "__main__":
    sys.exit(main())
