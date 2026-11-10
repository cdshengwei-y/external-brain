import tempfile
import time
import unittest

from agent_gateway.intent import (
    IntentResult,
    IntentRouter,
    Plugin,
    PluginContext,
    build_intent_prompt,
    normalize_intent,
    parse_intent_reply,
)
from agent_gateway.portable import PortableRequestService
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport


class FakeClassifier:
    """模拟 sw-bot 的意图分类器：返回原始文本或直接抛错。"""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def classify(self, text, catalog):
        self.calls.append({"text": text, "catalog": catalog})
        if self.error is not None:
            raise self.error
        if self.reply is None:
            return None
        if isinstance(self.reply, IntentResult):
            return self.reply
        return parse_intent_reply(self.reply, source="fake")


class _EchoPlugin(Plugin):
    intents = ("chat", "search")

    def catalog_lines(self):
        return ["- chat：闲聊", "- search：查实时信息"]

    def handle(self, intent, ctx):
        return IntentResult(intent=intent.intent, reply=intent.reply or "嗯。")


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        result = parse_intent_reply(
            '{"intent":"music_play","slots":{"query":"晴天"},"reply":"好，放晴天。"}'
        )
        self.assertEqual(result.intent, "music_play")
        self.assertEqual(result.slots["query"], "晴天")
        self.assertEqual(result.reply, "好，放晴天。")

    def test_fenced_json(self):
        result = parse_intent_reply(
            '```json\n{"intent": "fountain_stop", "reply": "关掉了"}\n```'
        )
        self.assertEqual(result.intent, "fountain_stop")

    def test_json_buried_in_sentence(self):
        result = parse_intent_reply(
            '好的：{"intent":"chat","reply":"今天晴，二十度。"} 就这些'
        )
        self.assertEqual(result.intent, "chat")
        self.assertEqual(result.reply, "今天晴，二十度。")

    def test_non_json_falls_back_to_chat_text(self):
        result = parse_intent_reply("今天天气不错，适合出去走走。")
        self.assertEqual(result.intent, "chat")
        self.assertEqual(result.source, "bot-text")
        self.assertIn("天气不错", result.reply)

    def test_missing_intent_defaults_to_chat(self):
        result = parse_intent_reply('{"reply":"我在"}')
        self.assertEqual(result.intent, "chat")
        self.assertEqual(result.reply, "我在")

    def test_empty_returns_none(self):
        self.assertIsNone(parse_intent_reply("   "))

    def test_aliases_and_normalization(self):
        self.assertEqual(normalize_intent("music.play"), "music_play")
        self.assertEqual(normalize_intent("Music-Play"), "music_play")
        self.assertEqual(normalize_intent("fountain.start"), "fountain_start")
        self.assertEqual(normalize_intent("点歌"), "music_play")
        self.assertEqual(normalize_intent(None), "chat")

    def test_prompt_carries_catalog_and_text(self):
        prompt = build_intent_prompt("放首歌", "- music_play：点歌")
        self.assertIn("- music_play：点歌", prompt)
        self.assertIn("用户说：放首歌", prompt)
        self.assertIn("只输出一行 JSON", prompt)


class RouterTests(unittest.TestCase):
    def _router(self, classifier):
        return IntentRouter([_EchoPlugin()], classifier)

    def _ctx(self):
        return PluginContext(text="随便说说", request_id="r1", device_id="d1")

    def test_resolve_dispatches_to_plugin(self):
        router = self._router(FakeClassifier('{"intent":"search","reply":"三度"}'))
        result = router.resolve("今天几度", self._ctx())
        self.assertEqual(result.intent, "search")
        self.assertEqual(result.reply, "三度")

    def test_classifier_error_returns_none(self):
        router = self._router(FakeClassifier(error=RuntimeError("超时")))
        self.assertIsNone(router.resolve("今天几度", self._ctx()))

    def test_missing_classifier_returns_none(self):
        router = self._router(None)
        self.assertIsNone(router.resolve("今天几度", self._ctx()))

    def test_empty_text_returns_none(self):
        router = self._router(FakeClassifier('{"intent":"chat","reply":"在"}'))
        self.assertIsNone(router.resolve("  ", self._ctx()))

    def test_unknown_intent_falls_back_to_chat_plugin(self):
        router = self._router(FakeClassifier('{"intent":"order_coffee","reply":"好"}'))
        result = router.resolve("来杯咖啡", self._ctx())
        self.assertEqual(result.reply, "好")

    def test_catalog_lists_registered_intents(self):
        router = self._router(FakeClassifier())
        self.assertIn("- chat：闲聊", router.catalog())
        self.assertEqual(sorted(router.intents), ["chat", "search"])

    def test_plugin_exception_degrades_to_apology(self):
        class Boom(Plugin):
            intents = ("chat",)

            def handle(self, intent, ctx):
                raise RuntimeError("插件炸了")

        router = IntentRouter([Boom()], FakeClassifier('{"intent":"chat","reply":"好"}'))
        result = router.resolve("你好", self._ctx())
        self.assertIn("再说一遍", result.reply)


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = HubStore(self.database.name)
        self.hub = HubService(self.store, MemoryTransport())

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.database.close()

    def _build(self, classifier):
        self.service = PortableRequestService(
            self.hub, intent_classifier=classifier
        )
        return self.service

    def _payload(self, text, session_mode="conversation"):
        return {
            "portable_device_id": "portable_001",
            "conversation_id": "conversation_001",
            "request_id": "request-intent-001",
            "protocol_version": "1.0",
            "source": "wearable",
            "input_type": "text",
            "text": text,
            "session_mode": session_mode,
        }

    def _wait(self, request_id):
        deadline = time.time() + 3
        while time.time() < deadline:
            request = self.service.get(request_id)
            if request["state"] in {"completed", "succeeded", "failed"}:
                return request
            time.sleep(0.02)
        self.fail(f"请求 {request_id} 没有在 3 秒内结束")

    def test_bot_intent_drives_device_action(self):
        classifier = FakeClassifier(
            '{"intent":"fountain_start","reply":"好，喷泉开了。"}'
        )
        self._build(classifier)
        self.service.submit(self._payload("让它跳个舞"))
        request = self._wait("request-intent-001")
        self.assertEqual(request["session_action"], "fountain_start")
        self.assertEqual(request["response_text"], "好，喷泉开了。")
        self.assertEqual(request["result"]["intent"]["intent"], "fountain_start")
        self.assertEqual(len(classifier.calls), 1)

    def test_plain_text_reply_becomes_chat(self):
        # 注意别用天气类文本：天气快答（_try_local_facts）会抢在 Bot 之前，
        # 用真实接口数据回答 —— 那是设计行为，有专门的 tests/test_weather.py。
        self._build(FakeClassifier("今天晴，二十四度，风不大。"))
        self.service.submit(self._payload("陪我聊聊天吧"))
        request = self._wait("request-intent-001")
        self.assertEqual(request["session_action"], "continue")
        self.assertIn("二十四度", request["response_text"])

    def test_classifier_failure_falls_back_to_keyword_rules(self):
        self._build(FakeClassifier(error=RuntimeError("连不上 sw-bot")))
        self.service.submit(self._payload("启动音乐喷泉"))
        request = self._wait("request-intent-001")
        self.assertEqual(request["session_action"], "fountain_start")
        self.assertIn("喷泉", request["response_text"])

    def test_music_control_stays_on_rules_and_skips_bot(self):
        classifier = FakeClassifier('{"intent":"chat","reply":"不该出现"}')
        self._build(classifier)
        self.service.submit(self._payload("嗯……那个", session_mode="music_control"))
        request = self._wait("request-intent-001")
        self.assertEqual(request["session_action"], "music_ignore")
        self.assertEqual(classifier.calls, [])

    def test_no_classifier_keeps_old_behaviour(self):
        self._build(None)
        self.service.submit(self._payload("关闭音乐喷泉"))
        request = self._wait("request-intent-001")
        self.assertEqual(request["session_action"], "fountain_stop")


class ModelDownTest(unittest.TestCase):
    """端点全挂时不许再走一遍 agent —— 那会白等两倍的预算。"""

    def test_chatllmerror_propagates_instead_of_falling_back(self):
        from agent_gateway.chat_llm import ChatLLMError

        class DeadClassifier:
            def __init__(self):
                self.calls = 0

            def classify(self, text, catalog):
                self.calls += 1
                raise ChatLLMError("主模型: timed out；备用模型: timed out")

        classifier = DeadClassifier()
        router = IntentRouter([], classifier)
        ctx = PluginContext(
            text="为什么天空是蓝的",
            request_id="r1",
            device_id="d1",
            session_mode="conversation",
            session_active=False,
            synthesize=lambda text: None,
            chat=lambda: None,
            music=lambda pair: None,
        )
        with self.assertRaises(ChatLLMError):
            router.resolve("为什么天空是蓝的", ctx)
        self.assertEqual(classifier.calls, 1)


if __name__ == "__main__":
    unittest.main()
