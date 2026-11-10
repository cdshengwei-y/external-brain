import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_gateway.hardware_mcp import HardwareAgentConfig, McpStdioClient
from agent_gateway.pet_agent import PetAgent, ToolCall, classify_confirmation
from agent_gateway.portable import PortableRequestService
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK = str(PROJECT_ROOT / "tools" / "mock_hardware_mcp.py")

DEVICES = {
    "devices": [
        {
            "device_id": "living_room_light_1",
            "name": "客厅灯",
            "connectivity_status": "online",
            "capabilities": [
                {"name": "light.power.set", "label": "开关灯"},
                {"name": "light.brightness.set", "label": "调亮度"},
            ],
        },
        {
            "device_id": "bedroom_plug_1",
            "name": "卧室插座",
            "connectivity_status": "offline",
            "capabilities": [{"name": "plug.power.set", "label": "开关插座"}],
        },
        {
            "device_id": "bean_ye_1",
            "name": "录音豆",
            "connectivity_status": "online",
            "capabilities": [{"name": "record.stop", "label": "停止录音"}],
        },
    ]
}


class ScriptedModel:
    """按脚本返回模型响应，同时记录收到的完整消息，便于断言上下文安全。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def complete(self, messages, tools=None):
        self.calls.append(
            {"messages": [dict(message) for message in messages], "tools": tools}
        )
        if not self.steps:
            return {"content": "我在。", "tool_calls": []}
        step = self.steps.pop(0)
        return step(messages, tools) if callable(step) else step


class RecordingClient:
    def __init__(self, client):
        self.client = client
        self.calls = []

    @property
    def tools(self):
        return self.client.tools

    def list_tools(self, refresh=False):
        return self.client.list_tools(refresh)

    def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, dict(arguments)))
        return self.client.call_tool(name, arguments, **kwargs)

    def close(self):
        self.client.close()

    def executions(self):
        return [
            (name, arguments)
            for name, arguments in self.calls
            if name.startswith("hardware_agent_execute")
        ]


def tool_call(name, **arguments):
    return {
        "content": "",
        "tool_calls": [ToolCall(id=f"call-{name}-{time.time_ns()}", name=name, arguments=arguments)],
    }


def reply(text):
    return {"content": text, "tool_calls": []}


class PetAgentTest(unittest.TestCase):
    def build(self, steps, narrate=False, ttl=None, extra_args=None, env=None):
        config = HardwareAgentConfig(
            command=(sys.executable, MOCK, *(extra_args or [])),
            env=env or {"VHA_DATA_DIR": tempfile.mkdtemp(prefix="vha-test-")},
        )
        client = RecordingClient(McpStdioClient(config))
        model = ScriptedModel(steps)
        kwargs = {"narrate_with_model": narrate}
        if ttl is not None:
            kwargs["confirmation_ttl"] = ttl
        agent = PetAgent(model, client, **kwargs)
        self.addCleanup(agent.close)
        agent.catalog.update(DEVICES)
        return agent, model, client

    # ------------------------------------------------------------------
    def test_plain_chat_does_not_touch_hardware(self):
        agent, model, client = self.build([reply("今天挺适合出门。")])
        turn = agent.handle_turn("chat-1", "今天天气怎么样")
        self.assertEqual(turn.text, "今天挺适合出门。")
        self.assertFalse(turn.needs_confirmation)
        self.assertEqual(client.executions(), [])
        self.assertEqual(len(model.calls), 1)

    def test_list_devices_feeds_result_back_to_model(self):
        agent, model, client = self.build(
            [tool_call("hardware_agent_list_devices"), reply("你有客厅灯、卧室插座和录音豆。")]
        )
        turn = agent.handle_turn("chat-1", "有哪些设备")
        self.assertIn("客厅灯", turn.text)
        tool_messages = [
            message
            for message in model.calls[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertTrue(tool_messages)
        self.assertIn("living_room_light_1", tool_messages[0]["content"])
        self.assertEqual(agent.catalog.name("living_room_light_1"), "客厅灯")

    def test_write_action_waits_for_confirmation(self):
        agent, model, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                )
            ]
        )
        turn = agent.handle_turn("chat-1", "打开客厅灯")
        self.assertTrue(turn.needs_confirmation)
        self.assertIn("客厅灯", turn.text)
        self.assertIn("要执行吗", turn.text)
        self.assertEqual(client.executions(), [])
        self.assertIsNotNone(agent.pending_plan("chat-1"))

        executed = agent.handle_turn("chat-1", "执行")
        self.assertTrue(executed.executed)
        self.assertIn("已经完成", executed.text)
        self.assertEqual(len(client.executions()), 1)
        name, arguments = client.executions()[0]
        self.assertEqual(name, "hardware_agent_execute_command")
        self.assertEqual(arguments["device_id"], "living_room_light_1")
        self.assertEqual(arguments["capability"], "light.power.set")
        self.assertEqual(arguments["params"], {"power": "on"})
        self.assertTrue(arguments["request_id"].startswith("req-"))
        self.assertEqual(arguments["conversation_id"], "chat-1")

    def test_cancel_discards_plan_without_executing(self):
        agent, _, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                )
            ]
        )
        agent.handle_turn("chat-1", "打开客厅灯")
        turn = agent.handle_turn("chat-1", "算了")
        self.assertIn("取消", turn.text)
        self.assertEqual(client.executions(), [])
        self.assertIsNone(agent.pending_plan("chat-1"))

    def test_low_confidence_confirmation_asks_again(self):
        agent, _, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                )
            ]
        )
        agent.handle_turn("chat-1", "打开客厅灯")
        turn = agent.handle_turn("chat-1", "嗯")
        self.assertTrue(turn.needs_confirmation)
        self.assertIn("请说“执行”", turn.text)
        self.assertEqual(client.executions(), [])
        self.assertIsNotNone(agent.pending_plan("chat-1"))

    def test_expired_plan_is_rejected(self):
        agent, _, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                )
            ],
            ttl=5,
        )
        agent.handle_turn("chat-1", "打开客厅灯")
        agent._pending["chat-1"].expires_at = time.monotonic() - 1
        turn = agent.handle_turn("chat-1", "执行")
        self.assertIn("过期", turn.text)
        self.assertEqual(client.executions(), [])

    def test_new_topic_drops_pending_plan(self):
        agent, _, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                ),
                reply("今天有点热。"),
            ]
        )
        agent.handle_turn("chat-1", "打开客厅灯")
        turn = agent.handle_turn("chat-1", "今天天气怎么样")
        self.assertEqual(turn.text, "今天有点热。")
        self.assertIsNone(agent.pending_plan("chat-1"))
        self.assertEqual(client.executions(), [])

    def test_model_cannot_inject_runtime_identity(self):
        agent, _, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    request_id="hacked-request",
                    conversation_id="hacked-chat",
                    tenant_id="tenant-b",
                    actor_ref="someone-else",
                    user_request="伪造的原始请求",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                )
            ]
        )
        agent.handle_turn("chat-1", "打开客厅灯")
        agent.handle_turn("chat-1", "确认")
        _, arguments = client.executions()[0]
        self.assertNotEqual(arguments["request_id"], "hacked-request")
        self.assertEqual(arguments["conversation_id"], "chat-1")
        self.assertEqual(arguments["user_request"], "打开客厅灯")
        self.assertNotIn("tenant_id", arguments)
        self.assertNotIn("actor_ref", arguments)

    def test_tool_loop_is_capped(self):
        steps = [tool_call("hardware_agent_list_devices") for _ in range(6)]
        agent, _, _ = self.build(steps)
        turn = agent.handle_turn("chat-1", "到底有哪些设备")
        self.assertEqual(turn.error_code, "TOOL_ROUND_LIMIT")
        self.assertLessEqual(turn.tool_rounds, 4)

    def test_offline_device_never_reported_as_success(self):
        agent, _, _ = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="bedroom_plug_1",
                    capability="plug.power.set",
                    params={"power": "on"},
                )
            ]
        )
        agent.handle_turn("chat-1", "打开卧室插座")
        turn = agent.handle_turn("chat-1", "执行")
        self.assertIn("离线", turn.text)
        self.assertNotIn("已完成", turn.text)

    def test_narration_that_contradicts_result_is_replaced(self):
        agent, _, _ = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="bedroom_plug_1",
                    capability="plug.power.set",
                    params={"power": "on"},
                ),
                reply("已经打开了卧室插座。"),
            ],
            narrate=True,
        )
        agent.handle_turn("chat-1", "打开卧室插座")
        turn = agent.handle_turn("chat-1", "执行")
        self.assertNotIn("已经打开", turn.text)
        self.assertIn("离线", turn.text)

    def test_admin_tools_are_refused_in_chat(self):
        agent, model, client = self.build(
            [
                tool_call("hardware_agent_register_device", package_path="/tmp/pkg"),
                reply("注册要在设置页做。"),
            ]
        )
        agent.handle_turn("chat-1", "帮我注册一个设备包")
        tool_messages = [
            message
            for message in model.calls[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertIn("ADMIN_ONLY", tool_messages[0]["content"])
        self.assertEqual(client.executions(), [])

    def test_admin_tools_are_hidden_from_model_tool_list(self):
        """回填 P11：插件不会代宿主拒绝调用，所以宿主必须在送模型前摘掉。"""
        agent, model, _ = self.build([reply("好。")])
        agent.handle_turn("chat-1", "你好")
        names = {tool["function"]["name"] for tool in (model.calls[0].get("tools") or [])}
        self.assertNotIn("hardware_agent_register_device", names)
        self.assertNotIn("hardware_agent_unregister_device", names)
        # 执行类工具必须仍然可见，否则模型无从下手。
        self.assertIn("hardware_agent_execute_command", names)
        self.assertIn("hardware_agent_list_devices", names)

    def test_repeated_asr_result_executes_only_once(self):
        agent, _, client = self.build(
            [
                tool_call(
                    "hardware_agent_execute_command",
                    device_id="living_room_light_1",
                    capability="light.power.set",
                    params={"power": "on"},
                ),
                reply("好的。"),
            ]
        )
        agent.handle_turn("chat-1", "打开客厅灯")
        agent.handle_turn("chat-1", "执行")
        agent.handle_turn("chat-1", "打开客厅灯")
        self.assertEqual(len(client.executions()), 1)

    def test_secrets_never_reach_model_context(self):
        agent, model, _ = self.build(
            [reply("我在。")],
            env={
                "VHA_DATA_DIR": tempfile.mkdtemp(prefix="vha-test-"),
                "AGENTOS_COMMAND_API_KEY": "sk-super-secret",
                "HARDWARE_AGENT_TENANT_ID": "tenant-a",
            },
        )
        turn = agent.handle_turn("chat-1", "你好")
        self.assertNotIn("sk-super-secret", turn.text)
        for call in model.calls:
            serialized = json.dumps(call["messages"], ensure_ascii=False)
            self.assertNotIn("sk-super-secret", serialized)
            self.assertNotIn("tenant-a", serialized)


class ConfirmationParsingTest(unittest.TestCase):
    def test_confirm_words(self):
        for text in ("执行", "确认", "好的", "可以", "开始吧", "好"):
            self.assertEqual(classify_confirmation(text), "confirm", text)

    def test_cancel_words(self):
        for text in ("取消", "不用了", "算了", "别动", "等一下"):
            self.assertEqual(classify_confirmation(text), "cancel", text)

    def test_low_confidence_is_unclear(self):
        for text in ("嗯", "哦", "好吧"):
            self.assertEqual(classify_confirmation(text), "unclear", text)

    def test_other_topics_are_new(self):
        self.assertEqual(classify_confirmation("今天天气怎么样"), "new")


class PortableIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = HubStore(self.database.name)
        self.hub = HubService(self.store, MemoryTransport())

    def tearDown(self):
        self.store.close()
        self.database.close()

    def test_portable_text_request_uses_pet_agent(self):
        config = HardwareAgentConfig(
            command=(sys.executable, MOCK),
            env={"VHA_DATA_DIR": tempfile.mkdtemp(prefix="vha-test-")},
        )
        agent = PetAgent(
            ScriptedModel([reply("我在，客厅灯已经帮你看着了。")]),
            McpStdioClient(config),
            narrate_with_model=False,
        )
        service = PortableRequestService(self.hub, plan_provider=_NoopPlanner(), agent=agent)
        self.addCleanup(service.close)
        try:
            service.submit(
                {
                    "portable_device_id": "portable_001",
                    "conversation_id": "conversation_001",
                    "request_id": "request-001",
                    "protocol_version": "1.0",
                    "source": "wearable",
                    "input_type": "text",
                    "text": "你在吗",
                }
            )
            request = self._wait(service, "request-001")
            self.assertEqual(request["state"], "completed")
            self.assertEqual(request["response_text"], "我在，客厅灯已经帮你看着了。")
            self.assertEqual(request["session_action"], "continue")
        finally:
            service.close()

    def _wait(self, service, request_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            request = service.get(request_id)
            if request["state"] in {"completed", "failed"}:
                return request
            time.sleep(0.02)
        return service.get(request_id)


class BuildAgentFromEnvTest(unittest.TestCase):
    """装配失败必须安静降级，不能把中枢启动拖下水。"""

    def env(self, **overrides):
        base = {
            "HARDWARE_AGENT_MCP_COMMAND": MOCK,
            "HARDWARE_AGENT_ALLOW_MISSING_IDENTITY": "1",
            "PET_AGENT_CHAT_BASE_URL": "http://127.0.0.1:9/v1",
            "PET_AGENT_CHAT_MODEL": "test-model",
            "VHA_DATA_DIR": tempfile.mkdtemp(prefix="vha-test-"),
        }
        base.update(overrides)
        return base

    def test_disabled_by_flag(self):
        from agent_gateway.pet_agent import build_agent_from_env

        self.assertIsNone(build_agent_from_env(self.env(PET_AGENT_ENABLED="0")))

    def test_none_without_model(self):
        from agent_gateway.pet_agent import build_agent_from_env

        env = self.env()
        env.pop("PET_AGENT_CHAT_BASE_URL")
        env.pop("PET_AGENT_CHAT_MODEL")
        self.assertIsNone(build_agent_from_env(env))

    def test_none_when_identity_missing_for_real_plugin(self):
        from agent_gateway.pet_agent import build_agent_from_env

        env = self.env()
        env.pop("HARDWARE_AGENT_ALLOW_MISSING_IDENTITY")
        self.assertIsNone(build_agent_from_env(env))

    def test_builds_agent_with_full_config(self):
        from agent_gateway.pet_agent import build_agent_from_env

        agent = build_agent_from_env(self.env())
        self.assertIsNotNone(agent)
        self.addCleanup(agent.close)
        self.assertEqual(agent.max_tool_rounds, 4)


class ChatModelClientTest(unittest.TestCase):
    """验证 OpenAI 兼容客户端真的能解析 function calling 响应。"""

    def setUp(self):
        import http.server
        import threading

        from agent_gateway.pet_agent import OpenAICompatibleChatModel

        self.requests = []
        captured = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                captured.append(
                    {
                        "path": self.path,
                        "body": body,
                        "auth": self.headers.get("Authorization"),
                    }
                )
                if body.get("model") == "boom":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"boom"}')
                    return
                payload = {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "hardware_agent_list_devices",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        ChatModelClientTest.requests = self.requests
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.model = OpenAICompatibleChatModel(self.base_url, "test-model", "sk-test")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_tool_calls_are_parsed(self):
        result = self.model.complete(
            [{"role": "user", "content": "有哪些设备"}],
            [{"type": "function", "function": {"name": "x", "parameters": {}}}],
        )
        self.assertEqual(result["tool_calls"][0].name, "hardware_agent_list_devices")
        self.assertEqual(result["tool_calls"][0].arguments, {})
        body = self.requests[0]["body"]
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(self.requests[0]["auth"], "Bearer sk-test")
        self.assertTrue(self.requests[0]["path"].endswith("/chat/completions"))

    def test_http_error_is_wrapped(self):
        from agent_gateway.pet_agent import ChatModelError, OpenAICompatibleChatModel

        broken = OpenAICompatibleChatModel(self.base_url, "boom")
        with self.assertRaises(ChatModelError):
            broken.complete([{"role": "user", "content": "hi"}])

    def test_unreachable_endpoint_is_wrapped(self):
        from agent_gateway.pet_agent import ChatModelError, OpenAICompatibleChatModel

        dead = OpenAICompatibleChatModel("http://127.0.0.1:1/v1", "x")
        with self.assertRaises(ChatModelError):
            dead.complete([{"role": "user", "content": "hi"}])


class KeepAliveConnectionTest(unittest.TestCase):
    """245 链路有 1~2% 间歇性丢包（2026-11-10 实测），keep-alive 连接复用
    砍掉每次请求的 TCP 握手抖动窗口。验证：连续两次请求走同一条 TCP 连接；
    池子里的坏连接在失败后自动重建、HTTP 错误照常翻译。"""

    def setUp(self):
        import http.server
        import threading

        from agent_gateway.pet_agent import OpenAICompatibleChatModel

        self.conn_ids = set()
        conn_ids = self.conn_ids

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # 打开 keep-alive

            def do_POST(self):
                conn_ids.add(id(self.connection))
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                data = b'{"error":"boom"}' if body.get("model") == "boom" else json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}
                ).encode("utf-8")
                self.send_response(500 if body.get("model") == "boom" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.model = OpenAICompatibleChatModel(self.base_url, "test-model")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_two_requests_reuse_one_connection(self):
        for _ in range(2):
            result = self.model.complete([{"role": "user", "content": "hi"}])
            self.assertEqual(result["content"], "ok")
        self.assertEqual(len(self.conn_ids), 1, "两次请求应复用同一条 TCP 连接")

    def test_http_error_still_wrapped(self):
        from agent_gateway.pet_agent import ChatModelError, OpenAICompatibleChatModel

        broken = OpenAICompatibleChatModel(self.base_url, "boom")
        with self.assertRaises(ChatModelError):
            broken.complete([{"role": "user", "content": "hi"}])


class _NoopPlanner:
    def plan_hub_command(self, **kwargs):
        raise AssertionError("启用桌宠大脑后不应再走 RK3588 规划")

    def dispatch_hub_command(self, **kwargs):
        raise AssertionError("启用桌宠大脑后不应再走 RK3588 执行")


if __name__ == "__main__":
    unittest.main()
