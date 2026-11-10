"""统一动作分发器的测试：不联网，monkeypatch HTTP 层。

覆盖：成功、业务失败（错误码→话术）、网络失败（只读重试/写不重试）、
鉴权失败、未配置（disabled 态）、幂等键格式、MQTT 直连客户端（fake paho）。
"""

import json
import unittest
from unittest import mock

from agent_gateway.actions import (
    ActionDispatcher,
    ActionSpec,
    AgentMcpExecutor,
    AgentMqttClient,
    AgentOSCommandClient,
    AgentOSCommandError,
    build_dispatcher_from_env,
)
from agent_gateway.hardware_mcp import ToolOutcome


def _client():
    return AgentOSCommandClient(
        "http://192.168.0.140:8099", "test-key", timeout_seconds=5
    )


def _ok_payload(result=None):
    return {"status": "succeeded", "result": result or {}}


class ClientTests(unittest.TestCase):
    def test_execute_builds_agentos_body(self):
        client = _client()
        captured = {}

        def fake_request(url, method, headers, data):
            captured.update(url=url, method=method, headers=headers, data=data)
            return _ok_payload()

        with mock.patch.object(client, "_request", side_effect=fake_request):
            client.execute_command(
                "record.start", "bean_ye_1", request_id="req-1-record_start"
            )
        self.assertEqual(captured["url"], "http://192.168.0.140:8099/api/agentos/v1/commands")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        import json

        body = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(body["request_id"], "req-1-record_start")
        self.assertEqual(body["tenant_id"], "tenant-a")
        # 录音豆协议：params 必须为空对象
        self.assertEqual(body["command"]["params"], {})
        self.assertEqual(body["command"]["capability"], "record.start")
        self.assertEqual(body["command"]["device_id"], "bean_ye_1")

    def test_http_failed_status_raises_with_code(self):
        client = _client()
        with mock.patch.object(
            client,
            "_request",
            side_effect=AgentOSCommandError(
                "设备超时", error_code="DEVICE_TIMEOUT", kind="failed"
            ),
        ):
            with self.assertRaises(AgentOSCommandError) as ctx:
                client.execute_command("record.start", "bean_ye_1", request_id="r1")
        self.assertEqual(ctx.exception.error_code, "DEVICE_TIMEOUT")
        self.assertEqual(ctx.exception.kind, "failed")


class DispatcherTests(unittest.TestCase):
    def _dispatcher(self, execute_result=None, execute_error=None):
        client = mock.Mock(spec=AgentOSCommandClient)
        if execute_error is not None:
            client.execute_command.side_effect = execute_error
        else:
            client.execute_command.return_value = execute_result or _ok_payload()
        return ActionDispatcher(client, default_device_id="bean_ye_1"), client

    def test_success_uses_spec_and_key(self):
        dispatcher, client = self._dispatcher(
            execute_result=_ok_payload({"recording": True})
        )
        spec = ActionSpec(capability="record.start", ok_reply="开始录音了。")
        outcome = dispatcher.execute("record_start", spec, {}, "req-7")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, {"recording": True})
        args, kwargs = client.execute_command.call_args
        self.assertEqual(kwargs["capability"], "record.start")
        self.assertEqual(kwargs["device_id"], "bean_ye_1")
        # 幂等键 = 请求 ID + 意图名
        self.assertEqual(kwargs["request_id"], "req-7-record_start")
        self.assertEqual(kwargs["params"], {})

    def test_error_code_maps_to_user_reply(self):
        dispatcher, _ = self._dispatcher(
            execute_error=AgentOSCommandError(
                "超时", error_code="DEVICE_TIMEOUT", kind="failed"
            )
        )
        outcome = dispatcher.execute(
            "record_start", ActionSpec(capability="record.start"), {}, "r"
        )
        self.assertFalse(outcome.ok)
        self.assertIn("没回话", outcome.user_reply)

    def test_unknown_error_gets_default_reply(self):
        dispatcher, _ = self._dispatcher(
            execute_error=AgentOSCommandError("离谱错误", error_code="WHATEVER")
        )
        outcome = dispatcher.execute(
            "record_start", ActionSpec(capability="record.start"), {}, "r"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.user_reply, ActionDispatcher._DEFAULT_REPLY)

    def test_network_write_no_retry(self):
        dispatcher, client = self._dispatcher(
            execute_error=AgentOSCommandError("断了", kind="network")
        )
        outcome = dispatcher.execute(
            "record_start", ActionSpec(capability="record.start"), {}, "r"
        )
        self.assertFalse(outcome.ok)
        # 2026-11-06：原来念「中控联系不上硬件服务」，「中控」「硬件服务」都是
        # 内部叫法，用户不知道在说哪台机器。改成说他面前这台设备。
        self.assertIn("没连上", outcome.user_reply)
        self.assertNotIn("硬件服务", outcome.user_reply)
        self.assertEqual(client.execute_command.call_count, 1)

    def test_network_readonly_retries_once(self):
        dispatcher, client = self._dispatcher(
            execute_error=AgentOSCommandError("断了", kind="network")
        )
        outcome = dispatcher.execute(
            "record_status", ActionSpec(capability="status.read", read_only=True), {}, "r"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(client.execute_command.call_count, 2)

    def test_auth_error_reply(self):
        dispatcher, _ = self._dispatcher(
            execute_error=AgentOSCommandError("401", error_code="UNAUTHORIZED", kind="auth")
        )
        outcome = dispatcher.execute(
            "record_start", ActionSpec(capability="record.start"), {}, "r"
        )
        self.assertFalse(outcome.ok)
        self.assertIn("钥匙失效", outcome.user_reply)

    def test_disabled_when_no_client(self):
        dispatcher = ActionDispatcher(None)
        outcome = dispatcher.execute(
            "record_status", ActionSpec(capability="status.read"), {}, "r"
        )
        self.assertFalse(outcome.ok)
        self.assertIn("还没配置", outcome.user_reply)

    def test_slots_not_forwarded_by_default(self):
        dispatcher, client = self._dispatcher()
        spec = ActionSpec(capability="record.start")
        dispatcher.execute("record_start", spec, {"query": "幻觉"}, "r")
        self.assertEqual(client.execute_command.call_args.kwargs["params"], {})


class BuildFromEnvTests(unittest.TestCase):
    def test_disabled_without_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            dispatcher = build_dispatcher_from_env()
        self.assertIsNone(dispatcher.client)
        self.assertEqual(dispatcher.default_device_id, "bean_ye_1")

    def test_enabled_with_env(self):
        env = {
            "RK3588_BASE_URL": "http://10.0.0.9:8099",
            "AGENTOS_COMMAND_API_KEY": "abc",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            dispatcher = build_dispatcher_from_env()
        self.assertIsNotNone(dispatcher.client)
        self.assertEqual(dispatcher.client.base_url, "http://10.0.0.9:8099")

    def test_mqtt_host_takes_priority(self):
        env = {
            "RK3588_MQTT_HOST": "10.0.0.9",
            "RK3588_MQTT_PORT": "1883",
            "RK3588_MQTT_USERNAME": "bean_ye_1",
            "RK3588_MQTT_PASSWORD": "pw",
            "RK3588_BASE_URL": "http://10.0.0.9:8099",
            "AGENTOS_COMMAND_API_KEY": "abc",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            dispatcher = build_dispatcher_from_env()
        self.assertIsInstance(dispatcher.client, AgentMqttClient)
        self.assertEqual(dispatcher.client.host, "10.0.0.9")

    def test_mcp_plugin_takes_top_priority(self):
        env = {
            "HARDWARE_AGENT_MCP_URL": "http://127.0.0.1:8787/mcp",
            "HARDWARE_AGENT_MCP_TOKEN": "t" * 32,
            "RK3588_MQTT_HOST": "10.0.0.9",
            "RK3588_BASE_URL": "http://10.0.0.9:8099",
            "AGENTOS_COMMAND_API_KEY": "abc",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            dispatcher = build_dispatcher_from_env()
        self.assertIsInstance(dispatcher.client, AgentMcpExecutor)


def _outcome(**overrides) -> ToolOutcome:
    defaults = dict(
        name="hardware_agent_execute_command",
        status="succeeded",
        ok=True,
        payload={"result": {"recording": True}},
        error_code=None,
        message=None,
        raw_text="{}",
        is_error=False,
    )
    defaults.update(overrides)
    return ToolOutcome(**defaults)


class McpExecutorTests(unittest.TestCase):
    """同事插件执行器的映射逻辑：成功透传 / 失败分类 / 异常归网络。"""

    def _executor(self, outcome=None, side_effect=None):
        fake = mock.Mock()
        if side_effect is not None:
            fake.call_tool.side_effect = side_effect
        else:
            fake.call_tool.return_value = outcome
        return AgentMcpExecutor(fake), fake

    def test_success_returns_payload_and_builds_args(self):
        executor, fake = self._executor(_outcome())
        ack = executor.execute_command("record.start", "bean_ye_1", request_id="req9-x")
        self.assertEqual(ack["result"], {"recording": True})
        tool, args = fake.call_tool.call_args.args
        self.assertEqual(tool, "hardware_agent_execute_command")
        # 插件 schema：6 个必填字段、additionalProperties=false
        self.assertEqual(
            set(args),
            {
                "request_id",
                "conversation_id",
                "user_request",
                "device_id",
                "capability",
                "params",
            },
        )
        self.assertEqual(args["device_id"], "bean_ye_1")
        self.assertEqual(args["capability"], "record.start")
        self.assertEqual(args["params"], {})
        self.assertEqual(args["request_id"], "req9-x")

    def test_failed_is_business_error(self):
        executor, _ = self._executor(
            _outcome(
                status="failed",
                ok=False,
                error_code="DEVICE_NOT_FOUND",
                message="设备未在硬件智能体中注册",
            )
        )
        with self.assertRaises(AgentOSCommandError) as ctx:
            executor.execute_command("record.start", "bean_ye_1", request_id="r")
        self.assertEqual(ctx.exception.error_code, "DEVICE_NOT_FOUND")
        self.assertEqual(ctx.exception.kind, "failed")

    def test_timed_out_and_pending_map_to_network(self):
        for outcome in (
            _outcome(status="timed_out", ok=False, error_code="DEVICE_TIMEOUT"),
            _outcome(status="failed", ok=False, error_code="DEVICE_TIMEOUT", payload={"pending": True}),
        ):
            executor, _ = self._executor(outcome)
            with self.assertRaises(AgentOSCommandError) as ctx:
                executor.execute_command("status.read", "bean_ye_1", request_id="r")
            self.assertEqual(ctx.exception.kind, "network")

    def test_transport_exception_maps_to_network(self):
        executor, _ = self._executor(side_effect=RuntimeError("connection refused"))
        with self.assertRaises(AgentOSCommandError) as ctx:
            executor.execute_command("status.read", "bean_ye_1", request_id="r")
        self.assertEqual(ctx.exception.kind, "network")


class _FakeMessage:
    def __init__(self, payload: bytes):
        self.payload = payload


class _FakePaho:
    """假 paho 客户端：记录调用、可用脚本在 publish 时回 ACK。"""

    def __init__(self, ack_when_publish=None, connect_reason=0):
        self.on_connect = None
        self.on_message = None
        self.connect_args = None
        self.credentials = None
        self.subscriptions = []
        self.published = []
        # publish 时回的 ACK（JSON bytes 或 None=不回，测超时）
        self.ack_when_publish = ack_when_publish
        self.connect_reason = connect_reason

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def connect(self, host, port, keepalive=60):
        self.connect_args = (host, port, keepalive)
        # 模拟 broker CONNACK 异步到达
        if self.on_connect is not None:
            self.on_connect(self, None, None, self.connect_reason, None)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def loop_start(self):
        pass

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        if self.ack_when_publish is not None:
            self.on_message(self, None, _FakeMessage(self.ack_when_publish))

        class _Info:
            rc = 0

        return _Info()


class _FakePahoFactory:
    def __init__(self, fake):
        self.fake = fake
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.fake


def _mqtt_client(fake, timeout=5.0):
    factory = _FakePahoFactory(fake)
    client = AgentMqttClient(
        "10.0.0.9",
        1883,
        "bean_ye_1",
        "pw",
        timeout_seconds=timeout,
        client_factory=factory,
    )
    return client, factory


def _ack(command_id="cmd-1", status="succeeded", result=None, **extra):
    payload = {
        "protocol_version": "1.0",
        "command_id": command_id,
        "device_id": "bean_ye_1",
        "status": status,
        "result": result or {},
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class MqttClientTests(unittest.TestCase):
    def test_success_roundtrip_and_payload_shape(self):
        ack_bytes = _ack(command_id="req1-record_start", result={"recording": True})
        fake = _FakePaho(ack_when_publish=ack_bytes)
        client, factory = _mqtt_client(fake)
        ack = client.execute_command(
            "record.start", "bean_ye_1", request_id="req1-record_start"
        )
        self.assertEqual(ack["result"], {"recording": True})
        self.assertEqual(fake.connect_args, ("10.0.0.9", 1883, 30))
        self.assertEqual(fake.credentials, ("bean_ye_1", "pw"))
        # 订了 ACK topic 才收得到回执
        self.assertEqual(fake.subscriptions[0][0], "agentos/+/acks")
        topic, payload, qos = fake.published[0]
        self.assertEqual(topic, "agentos/devices/bean_ye_1/commands")
        self.assertEqual(qos, 1)
        body = json.loads(payload.decode("utf-8"))
        # 协议只允许 5 个字段，多一个设备都会拒
        self.assertEqual(
            set(body),
            {"protocol_version", "command_id", "device_id", "capability", "params"},
        )
        self.assertEqual(body["params"], {})

    def test_failed_ack_raises_with_code(self):
        fake = _FakePaho(
            ack_when_publish=_ack(
                command_id="r1", status="failed", error_code="TF_CARD_UNAVAILABLE"
            )
        )
        client, _ = _mqtt_client(fake)
        with self.assertRaises(AgentOSCommandError) as ctx:
            client.execute_command("record.start", "bean_ye_1", request_id="r1")
        self.assertEqual(ctx.exception.error_code, "TF_CARD_UNAVAILABLE")
        self.assertEqual(ctx.exception.kind, "failed")

    def test_missing_ack_times_out_as_network(self):
        fake = _FakePaho(ack_when_publish=None)  # 设备不回话
        client, _ = _mqtt_client(fake, timeout=0.3)
        with self.assertRaises(AgentOSCommandError) as ctx:
            client.execute_command("status.read", "bean_ye_1", request_id="r2")
        self.assertEqual(ctx.exception.kind, "network")
        self.assertEqual(ctx.exception.error_code, "ACK_TIMEOUT")

    def test_connack_rejection_is_auth(self):
        fake = _FakePaho(connect_reason=5)  # 5 = not authorized
        client, _ = _mqtt_client(fake)
        with self.assertRaises(AgentOSCommandError) as ctx:
            client.execute_command("status.read", "bean_ye_1", request_id="r3")
        self.assertEqual(ctx.exception.kind, "auth")
        # 第二次直接复用缓存的失败，不再重连
        with self.assertRaises(AgentOSCommandError) as ctx2:
            client.execute_command("status.read", "bean_ye_1", request_id="r4")
        self.assertEqual(ctx2.exception.kind, "auth")
        self.assertEqual(factory_count(client), 1)

    def test_unreachable_broker_is_network(self):
        def broken_factory():
            raise OSError("connection refused")

        client = AgentMqttClient(
            "10.0.0.9", 1883, "u", "p", timeout_seconds=1.0, client_factory=broken_factory
        )
        with self.assertRaises(AgentOSCommandError) as ctx:
            client.execute_command("status.read", "bean_ye_1", request_id="r5")
        self.assertEqual(ctx.exception.kind, "network")

    def test_sanitize_command_id(self):
        self.assertEqual(
            AgentMqttClient.sanitize_command_id("req-7/_record.start@x"),
            "req-7-_record-start-x",
        )
        long = "a" * 100 + "!"
        cleaned = AgentMqttClient.sanitize_command_id(long)
        self.assertEqual(len(cleaned), 64)
        self.assertTrue(all(c.isalnum() or c in "-_" for c in cleaned))
        # 空的换 uuid，保证非空
        self.assertTrue(AgentMqttClient.sanitize_command_id(""))


def factory_count(client: AgentMqttClient) -> int:
    factory = client._client_factory
    return factory.count if isinstance(factory, _FakePahoFactory) else -1


if __name__ == "__main__":
    unittest.main()
