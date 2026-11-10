#!/usr/bin/env python3
"""符合 hardware-agent-mcp 契约的本地 mock 插件。

用途：真实插件是 Windows 上的 ``hardware-agent-mcp.exe``，本机（macOS）
跑不起来，但协议、字段、错误码必须能联调，所以这里按交接文档 v0.1.1
实现一份行为等价的 stdio JSON-RPC 服务。

    python3 tools/mock_hardware_mcp.py

调试开关：

    --timeout-device <device_id>   让该设备的执行返回 COMMAND_TIMEOUT
    --unverified <device_id>       让该设备返回 DEVICE_UNVERIFIED
    --noisy-stderr                 持续往 stderr 写日志，验证日志不串流

注意：mock 只用于开发和测试，不连接任何真实设备。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "mock-hardware-agent"
SERVER_VERSION = "0.1.1"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "hardware_agent_list_devices",
        "description": "查询当前用户已注册的智能硬件设备，返回 device_id、名称、在线状态和能力清单。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "hardware_agent_get_device_status",
        "description": "查询单个设备的当前状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"device_id": {"type": "string", "description": "设备 ID"}},
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hardware_agent_verify_device",
        "description": "重新探测设备在线状态，并更新 connectivity_status。",
        "inputSchema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hardware_agent_register_device",
        "description": "注册设备包并探测设备，仅供管理员/设置流程使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"package_path": {"type": "string"}},
            "required": ["package_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hardware_agent_unregister_device",
        "description": "注销一个已注册设备，仅供设置页使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hardware_agent_execute_command",
        "description": "对单个设备执行一个硬件命令。会改变设备状态，需要用户确认。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "本次逻辑执行的唯一 ID"},
                "conversation_id": {"type": "string"},
                "user_request": {"type": "string"},
                "device_id": {"type": "string"},
                "capability": {"type": "string"},
                "params": {"type": "object"},
                "plan_id": {"type": "string"},
            },
            "required": [
                "request_id",
                "conversation_id",
                "user_request",
                "device_id",
                "capability",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "hardware_agent_execute_commands",
        "description": "对多个设备执行一组命令（最多 8 个）。会改变设备状态，需要用户确认。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "conversation_id": {"type": "string"},
                "user_request": {"type": "string"},
                "commands": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "device_id": {"type": "string"},
                            "capability": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        "required": ["device_id", "capability"],
                    },
                },
                "plan_id": {"type": "string"},
            },
            "required": ["request_id", "conversation_id", "user_request", "commands"],
            "additionalProperties": False,
        },
    },
]


def build_devices() -> Dict[str, Dict[str, Any]]:
    def capability(name: str, label: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        return {"name": name, "label": label, "params_schema": schema}

    return {
        "living_room_light_1": {
            "device_id": "living_room_light_1",
            "name": "客厅灯",
            "connectivity_status": "online",
            "capabilities": [
                capability(
                    "light.power.set",
                    "开关灯",
                    {
                        "type": "object",
                        "properties": {"power": {"type": "string", "enum": ["on", "off"]}},
                        "required": ["power"],
                        "additionalProperties": False,
                    },
                ),
                capability(
                    "light.brightness.set",
                    "调亮度",
                    {
                        "type": "object",
                        "properties": {
                            "brightness": {"type": "integer", "minimum": 1, "maximum": 100}
                        },
                        "required": ["brightness"],
                        "additionalProperties": False,
                    },
                ),
            ],
            "state": {"power": "off", "brightness": 80},
        },
        "bean_ye_1": {
            "device_id": "bean_ye_1",
            "name": "录音豆",
            "connectivity_status": "online",
            "capabilities": [
                capability("record.start", "开始录音", {"type": "object", "properties": {}}),
                capability("record.stop", "停止录音", {"type": "object", "properties": {}}),
                capability("status.read", "查询状态", {"type": "object", "properties": {}}),
            ],
            "state": {"recording": False, "battery_percent": 76},
        },
        "bedroom_plug_1": {
            "device_id": "bedroom_plug_1",
            "name": "卧室插座",
            "connectivity_status": "offline",
            "capabilities": [
                capability(
                    "plug.power.set",
                    "开关插座",
                    {
                        "type": "object",
                        "properties": {"power": {"type": "string", "enum": ["on", "off"]}},
                        "required": ["power"],
                        "additionalProperties": False,
                    },
                )
            ],
            "state": {"power": "off"},
        },
        "kitchen_sensor_1": {
            "device_id": "kitchen_sensor_1",
            "name": "厨房温湿度计",
            "connectivity_status": "online",
            "capabilities": [
                capability("sensor.read", "读取温湿度", {"type": "object", "properties": {}})
            ],
            "state": {"temperature_c": 24.5, "humidity": 51},
        },
    }


class MockPlugin:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.devices = build_devices()
        self.executions: Dict[str, Dict[str, Any]] = {}
        self.calls = 0
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    def serve(self) -> None:
        if self.options.noisy_stderr:
            threading.Thread(target=self._noise, name="noise", daemon=True).start()
        self._log("mock hardware agent ready, protocol=%s", PROTOCOL_VERSION)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
                continue
            self._handle(message)

    def _noise(self) -> None:
        while True:
            self._log("heartbeat %d", int(time.time()))
            time.sleep(0.2)

    # ------------------------------------------------------------------
    def _handle(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                        "capabilities": {"tools": {"listChanged": False}},
                    },
                }
            )
            return
        if method == "notifications/initialized":
            self._log("client initialized")
            return
        if method == "ping":
            # 与真实插件一致：返回空 result，替代 HTTP /health。
            self._write({"jsonrpc": "2.0", "id": message_id, "result": {}})
            return
        if method == "tools/list":
            self._write(
                {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}
            )
            return
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                # 与真实插件 0.1.1 一致：参数不是对象走 JSON-RPC 层错误，
                # 业务错误码放在 error.data.error_code 里。
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {
                            "code": -32000,
                            "message": "tool arguments must be an object",
                            "data": {"error_code": "INVALID_PARAMS"},
                        },
                    }
                )
                return
            if name in self.options.hang_tool:
                # 模拟"请求已写入但没有响应"，用于验证宿主不会擅自重发。
                self._log("hang on tool %s (no response)", name)
                return
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {"code": -32601, "message": f"未知工具: {name}"},
                    }
                )
                return
            try:
                payload = handler(arguments)
            except Exception as error:  # pragma: no cover - 防御
                payload = _failed("INTERNAL_ERROR", str(error))
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                        ],
                        "isError": payload.get("status") != "succeeded",
                    },
                }
            )
            return
        if message_id is not None:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"未知方法: {method}"},
                }
            )

    # ------------------------------------------------------------------
    # 工具实现
    # ------------------------------------------------------------------
    def _tool_hardware_agent_list_devices(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "succeeded",
            "devices": [
                {
                    "device_id": device["device_id"],
                    "name": device["name"],
                    "connectivity_status": device["connectivity_status"],
                    "capabilities": [
                        {
                            "name": capability["name"],
                            "label": capability["label"],
                            "params_schema": capability["params_schema"],
                        }
                        for capability in device["capabilities"]
                    ],
                }
                for device in self.devices.values()
            ],
        }

    def _tool_hardware_agent_get_device_status(
        self, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        device, error = self._require_device(arguments.get("device_id"))
        if error:
            return error
        assert device is not None
        return {
            "status": "succeeded",
            "device_id": device["device_id"],
            "connectivity_status": device["connectivity_status"],
            "state": device.get("state") or {},
        }

    def _tool_hardware_agent_verify_device(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        device, error = self._require_device(arguments.get("device_id"))
        if error:
            return error
        assert device is not None
        if device["device_id"] != "bedroom_plug_1":
            device["connectivity_status"] = "online"
        return {
            "status": "succeeded",
            "device_id": device["device_id"],
            "connectivity_status": device["connectivity_status"],
        }

    def _tool_hardware_agent_register_device(
        self, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按 device-package-v1 注册：唯一入参是 device_package 对象本身。"""
        package = arguments.get("device_package")
        if not isinstance(package, dict):
            return _failed("INVALID_PARAMS", "device_package 必须是对象")
        device = package.get("device")
        if not isinstance(device, dict):
            device = package
        device_id = str(device.get("device_id") or "").strip()
        if not device_id:
            return _failed("INVALID_PARAMS", "device_package 缺少 device.device_id")

        raw_caps = device.get("capabilities") or []
        if not isinstance(raw_caps, list):
            return _failed("INVALID_PARAMS", "capabilities 必须是数组")
        capabilities: List[Dict[str, Any]] = []
        for item in raw_caps:
            if isinstance(item, str):
                capabilities.append(
                    {
                        "name": item,
                        "label": item,
                        "params_schema": {"type": "object", "properties": {}},
                    }
                )
            elif isinstance(item, dict) and item.get("name"):
                capabilities.append(
                    {
                        "name": str(item["name"]),
                        "label": str(item.get("label") or item["name"]),
                        "params_schema": item.get("params_schema")
                        or {"type": "object", "properties": {}},
                    }
                )
            else:
                return _failed("INVALID_PARAMS", "capabilities 元素格式不对")

        with self.lock:
            replaced = device_id in self.devices
            self.devices[device_id] = {
                "device_id": device_id,
                "name": str(device.get("name") or device_id),
                # 注册只落本地记录，在线状态要等 verify 拿到真实 ACK
                "connectivity_status": "unknown",
                "capabilities": capabilities,
                "state": {},
            }
        self._log("registered %s (replaced=%s)", device_id, replaced)
        return {
            "status": "succeeded",
            "device_id": device_id,
            "connectivity_status": "unknown",
            "already_registered": replaced,
        }

    def _tool_hardware_agent_unregister_device(
        self, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        device_id = str(arguments.get("device_id") or "").strip()
        if not device_id:
            return _failed("INVALID_PARAMS", "device_id 必须是非空字符串")
        with self.lock:
            existed = self.devices.pop(device_id, None) is not None
        if not existed:
            return _failed("DEVICE_NOT_FOUND", f"未找到设备 {device_id}")
        self._log("unregistered %s", device_id)
        return {"status": "succeeded", "device_id": device_id}

    def _tool_hardware_agent_execute_command(
        self, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        for key in ("request_id", "conversation_id", "user_request", "device_id", "capability"):
            if not str(arguments.get(key) or "").strip():
                return _failed("INVALID_PARAMS", f"缺少必填字段 {key}")
        request_id = str(arguments["request_id"])
        with self.lock:
            cached = self.executions.get(request_id)
        if cached is not None:
            duplicate = dict(cached)
            duplicate["duplicate"] = True
            duplicate["status"] = cached.get("status") or "succeeded"
            return duplicate

        result = self._execute_one(
            str(arguments["device_id"]),
            str(arguments["capability"]),
            arguments.get("params") or {},
        )
        result.update(
            {
                "request_id": request_id,
                "conversation_id": arguments.get("conversation_id"),
                "device_id": arguments.get("device_id"),
                "capability": arguments.get("capability"),
            }
        )
        with self.lock:
            self.executions[request_id] = result
        return result

    def _tool_hardware_agent_execute_commands(
        self, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        commands = arguments.get("commands")
        if not isinstance(commands, list) or not commands:
            return _failed("INVALID_PARAMS", "commands 必须是非空数组")
        if len(commands) > 8:
            return _failed("INVALID_PARAMS", "一次最多执行 8 个命令")
        for key in ("request_id", "conversation_id", "user_request"):
            if not str(arguments.get(key) or "").strip():
                return _failed("INVALID_PARAMS", f"缺少必填字段 {key}")
        request_id = str(arguments["request_id"])
        with self.lock:
            cached = self.executions.get(request_id)
        if cached is not None:
            duplicate = dict(cached)
            duplicate["duplicate"] = True
            return duplicate

        results = [
            self._execute_one(
                str(command.get("device_id")),
                str(command.get("capability")),
                command.get("params") or {},
            )
            for command in commands
        ]
        failed = [item for item in results if item.get("status") != "succeeded"]
        payload: Dict[str, Any] = {
            "status": "failed" if failed else "succeeded",
            "request_id": request_id,
            "conversation_id": arguments.get("conversation_id"),
            "results": results,
        }
        if failed:
            payload["error_code"] = failed[0].get("error_code") or "INTERNAL_ERROR"
            payload["message"] = failed[0].get("message") or "存在执行失败的命令"
        with self.lock:
            self.executions[request_id] = payload
        return payload

    # ------------------------------------------------------------------
    def _execute_one(
        self, device_id: str, capability: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.calls += 1
        device, error = self._require_device(device_id)
        if error:
            return error
        assert device is not None
        if device_id in self.options.timeout_device:
            time.sleep(0.05)
            return _failed("COMMAND_TIMEOUT", "设备未在时限内返回")
        if device_id in self.options.unverified:
            return _failed("DEVICE_UNVERIFIED", "设备尚未确认在线")
        if device["connectivity_status"] != "online":
            return _failed("DEVICE_OFFLINE", "设备当前离线")

        matched = None
        for item in device["capabilities"]:
            if item["name"] == capability:
                matched = item
                break
        if matched is None:
            return _failed(
                "CAPABILITY_NOT_SUPPORTED",
                f"{device['name']} 不支持 {capability}",
            )
        validation_error = _validate_schema(matched["params_schema"], params)
        if validation_error:
            return _failed("INVALID_PARAMS", validation_error)

        state = device.setdefault("state", {})
        state.update(params)
        self._log("execute %s %s %s", device_id, capability, params)
        return {
            "status": "succeeded",
            "result": {"state": dict(state)},
        }

    def _require_device(
        self, device_id: Any
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if not isinstance(device_id, str) or not device_id.strip():
            return None, _failed("INVALID_PARAMS", "device_id 必须是非空字符串")
        device = self.devices.get(device_id.strip())
        if device is None:
            return None, _failed("DEVICE_NOT_FOUND", f"未找到设备 {device_id}")
        return device, None

    # ------------------------------------------------------------------
    def _write(self, payload: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _log(self, template: str, *args: Any) -> None:
        sys.stderr.write("[mock-hardware-mcp] " + (template % args if args else template) + "\n")
        sys.stderr.flush()


def _failed(code: str, message: str) -> Dict[str, Any]:
    return {"status": "failed", "error_code": code, "message": message}


def _validate_schema(schema: Dict[str, Any], value: Any) -> Optional[str]:
    """够用的子集校验：object / array / string / integer / number / boolean。"""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return "参数必须是对象"
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                return f"缺少必填参数 {key}"
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    return f"不支持参数 {key}"
        for key, sub_schema in properties.items():
            if key in value:
                error = _validate_schema(sub_schema, value[key])
                if error:
                    return f"{key}: {error}"
        return None
    if expected == "array":
        if not isinstance(value, list):
            return "参数必须是数组"
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            return f"最多 {max_items} 项"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for item in value:
                error = _validate_schema(item_schema, item)
                if error:
                    return error
        return None
    if expected == "string":
        if not isinstance(value, str):
            return "参数必须是字符串"
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            return f"取值必须是 {'/'.join(map(str, enum))}"
        return None
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "参数必须是整数"
        return _validate_range(schema, value)
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "参数必须是数字"
        return _validate_range(schema, value)
    if expected == "boolean":
        return None if isinstance(value, bool) else "参数必须是布尔值"
    return None


def _validate_range(schema: Dict[str, Any], value: float) -> Optional[str]:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        return f"不能小于 {minimum}"
    if isinstance(maximum, (int, float)) and value > maximum:
        return f"不能大于 {maximum}"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mock hardware-agent MCP 服务")
    parser.add_argument(
        "--timeout-device",
        action="append",
        default=[],
        help="让该设备的执行返回 COMMAND_TIMEOUT，可重复",
    )
    parser.add_argument(
        "--unverified",
        action="append",
        default=[],
        help="让该设备返回 DEVICE_UNVERIFIED，可重复",
    )
    parser.add_argument(
        "--noisy-stderr",
        action="store_true",
        help="持续写 stderr，用于验证日志不混进协议流",
    )
    parser.add_argument(
        "--hang-tool",
        action="append",
        default=[],
        help="对这些工具不返回响应，用于验证调用超时行为，可重复",
    )
    return parser


def main() -> None:
    options = build_parser().parse_args()
    MockPlugin(options).serve()


if __name__ == "__main__":
    main()
