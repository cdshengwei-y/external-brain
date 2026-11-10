import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway.hardware_mcp import (
    HardwareAgentConfig,
    HardwareAgentConfigError,
    McpCallTimeout,
    McpProtocolError,
    McpStdioClient,
    WRITE_TOOL_NAMES,
    parse_tool_result,
    to_openai_tools,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK = str(PROJECT_ROOT / "tools" / "mock_hardware_mcp.py")


def make_config(extra_args=None, **overrides):
    options = {
        "command": (sys.executable, MOCK, *(extra_args or [])),
        "env": {"VHA_DATA_DIR": tempfile.mkdtemp(prefix="vha-test-")},
    }
    options.update(overrides)
    return HardwareAgentConfig(**options)


class HardwareAgentConfigTest(unittest.TestCase):
    def test_missing_identity_is_rejected(self):
        with self.assertRaises(HardwareAgentConfigError) as ctx:
            HardwareAgentConfig.from_env({}, command=[MOCK])
        message = str(ctx.exception)
        for key in (
            "HARDWARE_AGENT_TENANT_ID",
            "HARDWARE_AGENT_ACTOR_REF",
            "RK3588_BASE_URL",
            "AGENTOS_COMMAND_API_KEY",
        ):
            self.assertIn(key, message)

    def test_missing_executable_is_rejected(self):
        with self.assertRaises(HardwareAgentConfigError):
            HardwareAgentConfig.from_env(
                {
                    "HARDWARE_AGENT_TENANT_ID": "tenant-a",
                    "HARDWARE_AGENT_ACTOR_REF": "desktop-user",
                    "RK3588_BASE_URL": "http://192.168.0.140:8099",
                    "AGENTOS_COMMAND_API_KEY": "secret-key",
                }
            )

    def test_env_is_mapped_and_timeout_covers_http_timeout(self):
        data_dir = tempfile.mkdtemp(prefix="vha-test-")
        config = HardwareAgentConfig.from_env(
            {
                "HARDWARE_AGENT_TENANT_ID": "tenant-a",
                "HARDWARE_AGENT_ACTOR_REF": "desktop-user",
                "RK3588_BASE_URL": "http://192.168.0.140:8099",
                "AGENTOS_COMMAND_API_KEY": "secret-key",
                "RK3588_HTTP_TIMEOUT": "30",
                "VHA_DATA_DIR": data_dir,
            },
            command=[MOCK],
        )
        self.assertEqual(config.env["HARDWARE_AGENT_TENANT_ID"], "tenant-a")
        self.assertEqual(config.env["RK3588_HTTP_TIMEOUT"], "30")
        self.assertEqual(config.env["VHA_DATA_DIR"], data_dir)
        # 单次调用超时要覆盖插件内部的 HTTP 超时
        self.assertGreaterEqual(config.call_timeout, 30)
        # 日志只在键名层面留痕，不能泄漏密钥值
        self.assertNotIn("secret-key", json.dumps(config.describe(), ensure_ascii=False))

    def test_python_script_gets_interpreter(self):
        config = HardwareAgentConfig.from_env(
            {}, command=[MOCK], require_identity=False
        )
        self.assertTrue(config.command[0].endswith(("python", "python3")))
        self.assertEqual(config.command[1], MOCK)


class McpClientTest(unittest.TestCase):
    def setUp(self):
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()

    def start(self, extra_args=None, **overrides):
        self.client = McpStdioClient(make_config(extra_args, **overrides))
        self.client.start()
        return self.client

    def test_handshake_exposes_plugin_tools(self):
        client = self.start()
        self.assertEqual(client.negotiated_version, "2025-03-26")
        self.assertEqual(client.server_info.get("name"), "mock-hardware-agent")
        names = [tool["name"] for tool in client.tools]
        self.assertEqual(len(names), 7)
        self.assertIn("hardware_agent_execute_commands", names)

        forwarded = to_openai_tools(client.tools)
        self.assertEqual(forwarded[0]["type"], "function")
        self.assertIn("parameters", forwarded[0]["function"])

    def test_list_devices_returns_real_ids_and_capabilities(self):
        client = self.start()
        outcome = client.call_tool("hardware_agent_list_devices", {})
        self.assertTrue(outcome.ok)
        device_ids = [item["device_id"] for item in outcome.payload["devices"]]
        self.assertIn("living_room_light_1", device_ids)
        light = next(
            item
            for item in outcome.payload["devices"]
            if item["device_id"] == "living_room_light_1"
        )
        self.assertEqual(light["capabilities"][0]["label"], "开关灯")

    def test_execute_command_success_and_duplicate_request(self):
        client = self.start()
        arguments = {
            "request_id": "req-fixed-1",
            "conversation_id": "chat-1",
            "user_request": "打开客厅灯",
            "device_id": "living_room_light_1",
            "capability": "light.power.set",
            "params": {"power": "on"},
        }
        first = client.call_tool("hardware_agent_execute_command", arguments)
        self.assertTrue(first.ok)
        self.assertEqual(first.status, "succeeded")
        self.assertFalse(first.duplicate)

        second = client.call_tool("hardware_agent_execute_command", arguments)
        self.assertTrue(second.ok)
        self.assertTrue(second.duplicate, "同一 request_id 必须被识别为重复请求")

    def test_business_errors_are_surfaced(self):
        client = self.start()
        offline = client.call_tool(
            "hardware_agent_execute_command",
            {
                "request_id": "req-offline",
                "conversation_id": "chat-1",
                "user_request": "打开插座",
                "device_id": "bedroom_plug_1",
                "capability": "plug.power.set",
                "params": {"power": "on"},
            },
        )
        self.assertFalse(offline.ok)
        self.assertEqual(offline.error_code, "DEVICE_OFFLINE")
        self.assertTrue(offline.is_error)

        unsupported = client.call_tool(
            "hardware_agent_execute_command",
            {
                "request_id": "req-unsupported",
                "conversation_id": "chat-1",
                "user_request": "把温湿度计开灯",
                "device_id": "kitchen_sensor_1",
                "capability": "light.power.set",
                "params": {},
            },
        )
        self.assertFalse(unsupported.ok)
        self.assertEqual(unsupported.error_code, "CAPABILITY_NOT_SUPPORTED")

        invalid = client.call_tool(
            "hardware_agent_execute_command",
            {
                "request_id": "req-invalid",
                "conversation_id": "chat-1",
                "user_request": "把灯调成紫色",
                "device_id": "living_room_light_1",
                "capability": "light.power.set",
                "params": {"power": "purple"},
            },
        )
        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.error_code, "INVALID_PARAMS")

        unknown = client.call_tool(
            "hardware_agent_execute_command",
            {
                "request_id": "req-unknown",
                "conversation_id": "chat-1",
                "user_request": "开个不存在的设备",
                "device_id": "ghost_1",
                "capability": "light.power.set",
                "params": {},
            },
        )
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error_code, "DEVICE_NOT_FOUND")

    def test_query_tool_restarts_dead_process(self):
        client = self.start()
        self.assertTrue(client.call_tool("hardware_agent_list_devices", {}).ok)

        client._proc.kill()  # 模拟插件进程被外部杀掉
        deadline = time.time() + 5
        while client._proc is not None and time.time() < deadline:
            time.sleep(0.02)

        outcome = client.call_tool("hardware_agent_list_devices", {})
        self.assertTrue(outcome.ok, "查询工具应在新进程上自动重试成功")

    def test_write_tool_timeout_is_marked_as_sent(self):
        client = self.start(
            ["--hang-tool", "hardware_agent_execute_command"],
            call_timeout=1.0,
        )
        with self.assertRaises(McpCallTimeout) as ctx:
            client.call_tool(
                "hardware_agent_execute_command",
                {
                    "request_id": "req-hang",
                    "conversation_id": "chat-1",
                    "user_request": "打开客厅灯",
                    "device_id": "living_room_light_1",
                    "capability": "light.power.set",
                    "params": {"power": "on"},
                },
            )
        self.assertTrue(ctx.exception.sent, "已写入的写操作超时必须标记 sent，禁止自动重发")

    def test_noisy_stderr_does_not_break_protocol(self):
        client = self.start(["--noisy-stderr"])
        outcome = client.call_tool("hardware_agent_list_devices", {})
        self.assertTrue(outcome.ok)
        time.sleep(0.3)
        self.assertTrue(client.stderr_tail, "stderr 应被单独收集")

    def test_write_tool_names_match_protocol(self):
        self.assertIn("hardware_agent_execute_command", WRITE_TOOL_NAMES)
        self.assertIn("hardware_agent_execute_commands", WRITE_TOOL_NAMES)


class ToolResultParsingTest(unittest.TestCase):
    def test_non_json_text_is_not_treated_as_success(self):
        outcome = parse_tool_result(
            "hardware_agent_execute_command",
            {"content": [{"type": "text", "text": "boom"}], "isError": True},
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "failed")

    def test_missing_status_defaults_to_unknown(self):
        outcome = parse_tool_result(
            "hardware_agent_execute_command",
            {"content": [{"type": "text", "text": "{}"}]},
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "unknown")

    def test_incomplete_statuses_are_never_success(self):
        """插件只在 failed 时置 isError，timed_out/queued/executing 必须继续看内层 status。"""
        for status in ("timed_out", "queued", "executing", "pending"):
            outcome = parse_tool_result(
                "hardware_agent_execute_command",
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"status": status, "error_code": "DEVICE_TIMEOUT"},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": False,
                },
            )
            self.assertFalse(outcome.ok, status)
            self.assertTrue(outcome.pending, status)

    def test_pending_flag_without_status(self):
        outcome = parse_tool_result(
            "hardware_agent_execute_command",
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"status": "failed", "pending": True}, ensure_ascii=False
                        ),
                    }
                ],
                "isError": True,
            },
        )
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.pending)

    def test_jsonrpc_error_carries_business_error_code(self):
        """交接回复 4.4：参数非法是 JSON-RPC 错误，业务码在 error.data.error_code。"""
        client = McpStdioClient(make_config())
        client.start()
        self.addCleanup(client.close)
        with self.assertRaises(McpProtocolError) as ctx:
            client._request(
                "tools/call",
                {"name": "hardware_agent_list_devices", "arguments": [1]},
            )
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.code, -32000)

    def test_failed_with_real_error_code(self):
        outcome = parse_tool_result(
            "hardware_agent_execute_command",
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "failed",
                                "error_code": "DEVICE_NOT_FOUND",
                                "message": "设备未在硬件智能体中注册",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            },
        )
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.pending)
        self.assertEqual(outcome.error_code, "DEVICE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
