"""远端接口传输的测试：不启动对方的 exe，只通过 HTTP 接入。

对应「桌宠只接插件接口」的形态：插件跑在哪台机器、什么系统都不重要，
宿主这边只有一个 URL。
"""

import json
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from agent_gateway.hardware_mcp import (
    HardwareAgentConfig,
    HardwareAgentConfigError,
    McpCallTimeout,
    McpProcessError,
    McpStdioClient,
    parse_tool_result,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_HTTP = PROJECT_ROOT / "tools" / "mock_hardware_mcp_http.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"mock 服务未在 {timeout}s 内监听 {port}")


class MockHttpServer:
    def __init__(self, *args: str) -> None:
        self.port = _free_port()
        self.process = subprocess.Popen(
            [sys.executable, str(MOCK_HTTP), "--port", str(self.port), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        _wait_port(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


class HttpConfigTest(unittest.TestCase):
    def test_url_mode_does_not_require_identity(self):
        config = HardwareAgentConfig.from_env(
            {"HARDWARE_AGENT_MCP_URL": "http://192.168.0.20:18800/mcp"}
        )
        self.assertEqual(config.transport, "http")
        self.assertEqual(config.command, ())
        self.assertEqual(config.url, "http://192.168.0.20:18800/mcp")
        self.assertIsNone(config.token)

    def test_url_must_be_http(self):
        with self.assertRaises(HardwareAgentConfigError):
            HardwareAgentConfig.from_env({"HARDWARE_AGENT_MCP_URL": "192.168.0.20:18800"})

    def test_token_defaults_to_bearer(self):
        config = HardwareAgentConfig.from_env(
            {
                "HARDWARE_AGENT_MCP_URL": "http://192.168.0.20:18800/mcp",
                "HARDWARE_AGENT_MCP_TOKEN": "tk-1",
            }
        )
        self.assertEqual(config.token, "tk-1")
        self.assertEqual(config.token_header, "Authorization")
        self.assertEqual(config.token_prefix, "Bearer ")
        # 日志描述里不能出现令牌
        self.assertNotIn("tk-1", json.dumps(config.describe(), ensure_ascii=False))

    def test_url_takes_priority_over_command(self):
        config = HardwareAgentConfig.from_env(
            {
                "HARDWARE_AGENT_MCP_URL": "http://127.0.0.1:18800/mcp",
                "HARDWARE_AGENT_MCP_COMMAND": "/no/such/plugin",
            }
        )
        self.assertEqual(config.transport, "http")
        self.assertEqual(config.command, ())


class HttpTransportTest(unittest.TestCase):
    server_options: tuple = ()

    @classmethod
    def setUpClass(cls):
        cls.server = MockHttpServer(*cls.server_options)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()

    def start(self, **env):
        config = HardwareAgentConfig.from_env(
            {"HARDWARE_AGENT_MCP_URL": self.server.url, **env}
        )
        self.client = McpStdioClient(config)
        self.client.start()
        return self.client

    def test_handshake_and_session(self):
        client = self.start()
        self.assertEqual(client.transport, "http")
        self.assertEqual(client.negotiated_version, "2025-03-26")
        self.assertEqual(client.server_info.get("name"), "mock-hardware-agent")
        self.assertEqual(len(client.tools), 7)
        self.assertTrue(client._session_id, "服务端下发的 Mcp-Session-Id 应被保存")

    def test_list_devices(self):
        client = self.start()
        outcome = client.call_tool("hardware_agent_list_devices", {})
        self.assertTrue(outcome.ok)
        ids = [item["device_id"] for item in outcome.payload["devices"]]
        self.assertIn("living_room_light_1", ids)

    def test_write_is_idempotent_by_request_id(self):
        client = self.start()
        arguments = {
            "request_id": "http-req-1",
            "conversation_id": "c-1",
            "user_request": "打开客厅灯",
            "device_id": "living_room_light_1",
            "capability": "light.power.set",
            "params": {"power": "on"},
        }
        first = client.call_tool("hardware_agent_execute_command", arguments)
        second = client.call_tool("hardware_agent_execute_command", arguments)
        self.assertEqual(first.status, "succeeded")
        self.assertTrue(second.duplicate)
        self.assertEqual(
            first.payload.get("result"), second.payload.get("result")
        )

    def test_business_error_is_not_a_transport_error(self):
        client = self.start()
        outcome = client.call_tool(
            "hardware_agent_execute_command",
            {
                "request_id": "http-req-offline",
                "conversation_id": "c-1",
                "user_request": "开插座",
                "device_id": "bedroom_plug_1",
                "capability": "plug.power.set",
                "params": {"power": "on"},
            },
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_code, "DEVICE_OFFLINE")

    def test_unknown_tool_raises_protocol_error(self):
        client = self.start()
        with self.assertRaises(Exception) as ctx:
            client.call_tool("hardware_agent_nope", {})
        self.assertIn("未知工具", str(ctx.exception))


class HttpSseAndAuthTest(HttpTransportTest):
    server_options = ("--sse", "--require-token", "tk-ok")

    def start(self, **env):
        return super().start(**{"HARDWARE_AGENT_MCP_TOKEN": "tk-ok", **env})

    def test_sse_response_is_parsed(self):
        client = self.start()
        outcome = client.call_tool("hardware_agent_list_devices", {})
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.payload["devices"]), 4)


class HttpAuthFailureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockHttpServer("--require-token", "tk-ok")

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_wrong_token_is_reported_clearly(self):
        config = HardwareAgentConfig.from_env(
            {
                "HARDWARE_AGENT_MCP_URL": self.server.url,
                "HARDWARE_AGENT_MCP_TOKEN": "wrong",
            }
        )
        client = McpStdioClient(config)
        with self.assertRaises(McpProcessError) as ctx:
            client.start()
        self.assertIn("401", str(ctx.exception))
        self.assertIn("HARDWARE_AGENT_MCP_TOKEN", str(ctx.exception))
        client.close()


class HttpUnreachableTest(unittest.TestCase):
    def test_connection_refused(self):
        port = _free_port()
        config = HardwareAgentConfig.from_env(
            {"HARDWARE_AGENT_MCP_URL": f"http://127.0.0.1:{port}/mcp"}
        )
        client = McpStdioClient(config)
        with self.assertRaises(McpProcessError) as ctx:
            client.start()
        self.assertIn("连接硬件插件失败", str(ctx.exception))
        client.close()


class HttpTimeoutTest(unittest.TestCase):
    """写操作超时后不能自动重发，否则设备可能被执行两次。"""

    @classmethod
    def setUpClass(cls):
        cls.server = MockHttpServer(
            "--delay-tool", "hardware_agent_execute_command"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_write_timeout_marks_sent_and_does_not_retry(self):
        config = HardwareAgentConfig.from_env(
            {
                "HARDWARE_AGENT_MCP_URL": self.server.url,
                "HARDWARE_AGENT_MCP_CALL_TIMEOUT": "2",
            }
        )
        client = McpStdioClient(config)
        client.start()
        try:
            with self.assertRaises(McpCallTimeout) as ctx:
                client.call_tool(
                    "hardware_agent_execute_command",
                    {
                        "request_id": "timeout-1",
                        "conversation_id": "c-1",
                        "user_request": "打开客厅灯",
                        "device_id": "living_room_light_1",
                        "capability": "light.power.set",
                        "params": {"power": "on"},
                    },
                )
            self.assertTrue(ctx.exception.sent)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
