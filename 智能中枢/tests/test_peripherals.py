"""录音豆外设插件的测试：Fake 分发器注入结果，不联网。

覆盖：intent→capability 映射、成功/失败话术、真实数据回复（停止报文件名、
状态报存储）、catalog 自动扩展（Bot 提示词认识新意图）、别名归一。
"""

import unittest

from agent_gateway.actions import ActionOutcome, ActionSpec
from agent_gateway.intent import (
    IntentResult,
    IntentRouter,
    PluginContext,
    normalize_intent,
    parse_intent_reply,
)
from agent_gateway.peripherals import RecordingBeanPlugin


class FakeDispatcher:
    """记录调用参数，可注入成功/失败 outcome。"""

    def __init__(self, outcome=None):
        self.outcome = outcome or ActionOutcome(ok=True, result={})
        self.calls = []

    def execute(self, intent_name, spec, slots, request_id):
        self.calls.append(
            {
                "intent": intent_name,
                "capability": spec.capability,
                "device_id": spec.device_id,
                "slots": slots,
                "request_id": request_id,
            }
        )
        return self.outcome


def _ctx(request_id="req-1"):
    return PluginContext(text="开始录音", request_id=request_id)


class RecordingBeanPluginTests(unittest.TestCase):
    def test_intent_capability_mapping(self):
        fake = FakeDispatcher()
        plugin = RecordingBeanPlugin(dispatcher=fake)
        for intent_name, capability in (
            ("record_start", "record.start"),
            ("record_stop", "record.stop"),
            ("record_status", "status.read"),
        ):
            plugin.handle(
                IntentResult(intent=intent_name, reply="好"), _ctx()
            )
            self.assertEqual(fake.calls[-1]["capability"], capability)
            self.assertEqual(fake.calls[-1]["device_id"], "")  # 用分发器默认设备

    def test_start_success_reply(self):
        plugin = RecordingBeanPlugin(dispatcher=FakeDispatcher())
        result = plugin.handle(IntentResult(intent="record_start"), _ctx())
        self.assertEqual(result.reply, "开始录音了。")
        self.assertEqual(result.session_action, "continue")

    def test_stop_reply_includes_file(self):
        fake = FakeDispatcher(
            outcome=ActionOutcome(
                ok=True, result={"last_completed_file": "REC_20261103_090000.wav"}
            )
        )
        plugin = RecordingBeanPlugin(dispatcher=fake)
        result = plugin.handle(IntentResult(intent="record_stop"), _ctx())
        self.assertIn("REC_20261103_090000.wav", result.reply)
        self.assertIn("停了", result.reply)

    def test_status_reply_with_real_data(self):
        fake = FakeDispatcher(
            outcome=ActionOutcome(
                ok=True, result={"recording": True, "sd_free_mb": 7400}
            )
        )
        plugin = RecordingBeanPlugin(dispatcher=fake)
        result = plugin.handle(IntentResult(intent="record_status"), _ctx())
        self.assertIn("正在录音", result.reply)
        self.assertIn("7400 MB", result.reply)

    def test_status_reply_idle(self):
        fake = FakeDispatcher(outcome=ActionOutcome(ok=True, result={"recording": False}))
        plugin = RecordingBeanPlugin(dispatcher=fake)
        result = plugin.handle(IntentResult(intent="record_status"), _ctx())
        self.assertIn("空闲", result.reply)

    def test_status_reply_missing_fields_honest(self):
        fake = FakeDispatcher(outcome=ActionOutcome(ok=True, result={}))
        plugin = RecordingBeanPlugin(dispatcher=fake)
        result = plugin.handle(IntentResult(intent="record_status"), _ctx())
        self.assertIn("没读全", result.reply)

    def test_failure_reaches_user_as_speech(self):
        fake = FakeDispatcher(
            outcome=ActionOutcome(
                ok=False,
                status="failed",
                error_code="DEVICE_TIMEOUT",
                user_reply="录音豆半天没回话，可能不在线了，等会儿再试试。",
            )
        )
        plugin = RecordingBeanPlugin(dispatcher=fake)
        result = plugin.handle(IntentResult(intent="record_start"), _ctx())
        self.assertIn("没回话", result.reply)
        # 失败话术里绝不能带错误码或异常栈
        self.assertNotIn("DEVICE_TIMEOUT", result.reply)

    def test_disabled_dispatcher_reply(self):
        plugin = RecordingBeanPlugin(dispatcher=None)
        result = plugin.handle(IntentResult(intent="record_start"), _ctx())
        self.assertIn("还没配置", result.reply)

    def test_catalog_mentions_recording_intents(self):
        lines = "\n".join(RecordingBeanPlugin().catalog_lines())
        for word in ("record_start", "record_stop", "record_status"):
            self.assertIn(word, lines)


class RoutingTests(unittest.TestCase):
    """走完整 IntentRouter，验证 Bot 回复能命中录音豆插件。"""

    def _router(self, fake):
        classifier_reply = '{"intent":"record.start","reply":"好"}'
        return IntentRouter([RecordingBeanPlugin(dispatcher=fake)], _FakeClassifier(classifier_reply))

    def test_bot_dotted_intent_hits_plugin(self):
        fake = FakeDispatcher()
        router = self._router(fake)
        result = router.resolve("开始录音", _ctx(request_id="req-9"))
        self.assertIsNotNone(result)
        # 动作确认用插件声明的固定话术，不依赖 Bot 现编
        self.assertEqual(result.reply, "开始录音了。")
        self.assertEqual(fake.calls[0]["intent"], "record_start")
        self.assertEqual(fake.calls[0]["capability"], "record.start")

    def test_alias_normalization(self):
        for raw, want in (
            ("record.start", "record_start"),
            ("start_recording", "record_start"),
            ("开始录音", "record_start"),
            ("停止录音", "record_stop"),
            ("录音状态", "record_status"),
        ):
            self.assertEqual(normalize_intent(raw), want, raw)

    def test_classifier_catalog_contains_bean_intents(self):
        """catalog 会喂给 Bot 的意图判定提示词——新插件注册后 Bot 自动认识。"""
        captured = {}

        class CatalogProbe:
            def classify(self, text, catalog):
                captured["catalog"] = catalog
                return None

        router = IntentRouter([RecordingBeanPlugin(dispatcher=FakeDispatcher())], CatalogProbe())
        router.resolve("随便说说", _ctx())
        catalog = captured["catalog"]
        catalog_text = catalog if isinstance(catalog, str) else "\n".join(catalog)
        for word in ("record_start", "record_stop", "record_status"):
            self.assertIn(word, catalog_text)


class _FakeClassifier:
    def __init__(self, reply):
        self.reply = reply

    def classify(self, text, catalog):
        return parse_intent_reply(self.reply, source="fake")


if __name__ == "__main__":
    unittest.main()
