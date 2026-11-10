import time
import unittest
from unittest import mock

from agent_gateway.chat_llm import (
    ChatLLMClient,
    ChatLLMError,
    FallbackChatClient,
    build_chat_client_from_env,
)
from agent_gateway.pet_agent import ChatModelError


def _reply(content):
    return {"content": content, "tool_calls": [], "raw": {}}


class SendPayloadTest(unittest.TestCase):
    """send() 的两种形态：闲聊要带口语前缀，意图要裸提示词。"""

    def _client(self):
        return ChatLLMClient(
            "http://127.0.0.1:1/v1",
            "test-model",
            "k",
            system_prompt="你是桌宠。",
            voice_prefix="（口语回答）",
        )

    def test_chat_call_prepends_prefix_and_system(self):
        client = self._client()
        with mock.patch.object(client._model, "complete", return_value=_reply("  好的  ")) as spy:
            self.assertEqual(client.send("你好"), "好的")
        messages = spy.call_args[0][0]
        self.assertEqual(messages[0], {"role": "system", "content": "你是桌宠。"})
        self.assertEqual(messages[1]["content"], "（口语回答）\n你好")

    def test_raw_call_keeps_prompt_clean(self):
        client = self._client()
        with mock.patch.object(client._model, "complete", return_value=_reply('{"intent":"chat"}')) as spy:
            client.send("判意图", raw=True)
        messages = spy.call_args[0][0]
        # 意图分类不能掺人设和口语前缀，否则模型会把 JSON 包进一段话里
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "判意图")

    def test_empty_system_prompt_is_skipped(self):
        client = ChatLLMClient(
            "http://127.0.0.1:1/v1", "m", system_prompt="", voice_prefix=""
        )
        with mock.patch.object(client._model, "complete", return_value=_reply("在")) as spy:
            client.send("在吗")
        self.assertEqual([m["role"] for m in spy.call_args[0][0]], ["user"])


class FallbackTest(unittest.TestCase):
    """主用挂了要能顶上，而且不能把异常漏给设备。

    2026-11-06 下午改成并发兜底（hedge）后，兜底路径走的是
    ``complete_only``（不动历史），所以这里的替身也打在这个入口上。
    """

    def _make(self, label, result):
        client = ChatLLMClient("http://127.0.0.1:1/v1", label, label=label)
        if isinstance(result, Exception):
            client.complete_only = mock.Mock(side_effect=result)
        else:
            client.complete_only = mock.Mock(return_value=result)
        return client

    def test_first_success_wins(self):
        primary = self._make("主模型", "本地回复")
        backup = self._make("备用模型", "备用回复")
        self.assertEqual(FallbackChatClient([primary, backup]).send("hi"), "本地回复")
        backup.complete_only.assert_not_called()

    def test_falls_back_on_error(self):
        primary = self._make("主模型", ChatModelError("连不上"))
        backup = self._make("备用模型", "备用回复")
        self.assertEqual(FallbackChatClient([primary, backup]).send("hi"), "备用回复")

    def test_falls_back_on_empty(self):
        primary = self._make("主模型", "   ")
        backup = self._make("备用模型", "备用回复")
        self.assertEqual(FallbackChatClient([primary, backup]).send("hi"), "备用回复")

    def test_all_failed_raises(self):
        primary = self._make("主模型", ChatModelError("连不上"))
        backup = self._make("备用模型", OSError("超时"))
        with self.assertRaises(ChatLLMError):
            FallbackChatClient([primary, backup]).send("hi")

    def test_slow_primary_is_overtaken_by_backup(self):
        """主端点不报错、只是慢 —— 这正是 245 那天 32.9s 长尾的形态。

        光靠「报错才切」救不了它，必须在 hedge 点把备用放出去。
        """
        import threading

        def slow(*args, **kwargs):
            time.sleep(1.2)
            return "本地迟到的回复"

        primary = ChatLLMClient("http://127.0.0.1:1/v1", "主模型", label="主模型")
        primary.complete_only = mock.Mock(side_effect=slow)
        backup = self._make("备用模型", "备用回复")
        client = FallbackChatClient([primary, backup], hedge_after_seconds=0.1)
        started = time.monotonic()
        try:
            reply = client.send("hi")
            elapsed = time.monotonic() - started
        finally:
            client.close()
        self.assertEqual(reply, "备用回复")
        self.assertLess(elapsed, 1.0, "不该等主端点那 1.2 秒")

    def test_history_written_once_when_hedged(self):
        """两个端点同时跑时，记忆只能记赢家那一份。"""
        import threading

        primary = ChatLLMClient(
            "http://127.0.0.1:1/v1", "主模型", label="主模型", history_turns=6
        )
        primary.complete_only = mock.Mock(side_effect=lambda *a, **k: time.sleep(0.8) or "迟到")
        backup = ChatLLMClient(
            "http://127.0.0.1:1/v1", "备用模型", label="备用模型", history_turns=6
        )
        backup.complete_only = mock.Mock(return_value="备用回复")
        client = FallbackChatClient([primary, backup], hedge_after_seconds=0.05)
        try:
            client.send("我叫小明", conversation_id="c1")
        finally:
            client.close()
        history = primary.history_text("c1")
        self.assertIn("我叫小明", history)
        self.assertEqual(history.count("我叫小明"), 1, "同一轮不能被记两遍")


    def test_late_failure_puts_primary_into_cooldown(self):
        """输掉的那次请求迟到超时，也要把主端点记进冷却。

        否则 hedged 永远是「备用先回就返回」，主端点 12 秒超时异常无人接收，
        下一句还是先白等一个 hedge 周期 —— 245 掉线时一句一句全耗在这儿。
        """

        def slow_fail(*args, **kwargs):
            time.sleep(0.4)
            raise ChatModelError("timed out")

        primary = ChatLLMClient("http://127.0.0.1:1/v1", "主模型", label="主模型")
        primary.complete_only = mock.Mock(side_effect=slow_fail)
        backup = self._make("备用模型", "备用回复")
        client = FallbackChatClient(
            [primary, backup], hedge_after_seconds=0.05, cooldown_seconds=30
        )
        try:
            self.assertEqual(client.send("hi"), "备用回复")
            time.sleep(0.8)  # 等迟到那次失败落定
            self.assertIn(0, client._down_until, "迟到失败也该记进冷却")
            self.assertEqual(client.send("再来一句"), "备用回复")
            self.assertEqual(
                primary.complete_only.call_count, 1, "冷却期内不该再打主端点"
            )
        finally:
            client.close()


    def test_repeated_failures_back_off(self):
        """网络烂的端点越失败越靠边站，不能每 45 秒复活一次继续拖累。"""
        primary = self._make("主模型", ChatModelError("连不上"))
        backup = self._make("备用模型", "备用回复")
        client = FallbackChatClient(
            [primary, backup], hedge_after_seconds=0.05, cooldown_seconds=45
        )
        try:
            client.send("一")
            first = client._down_until[0] - time.monotonic()
            client._down_until.clear()  # 模拟冷却到期
            client.send("二")
            second = client._down_until[0] - time.monotonic()
            self.assertEqual(client._fail_streak[0], 2)
            self.assertGreater(second, first * 1.5, "第二次失败该退得更久")
        finally:
            client.close()

    def test_budget_caps_the_worst_case_wait(self):
        """两端都卡住时不能一路等到 24 秒，到预算就认输。

        实测那次「主 12s 超时 + 备用 12s 超时」让设备静了 32 秒，
        用户以为死机；桌宠场景超过 8 秒这句话就没意义了。
        """

        def hang(*args, **kwargs):
            time.sleep(2.0)
            return "太晚了"

        primary = ChatLLMClient("http://127.0.0.1:1/v1", "主模型", label="主模型")
        primary.complete_only = mock.Mock(side_effect=hang)
        backup = ChatLLMClient("http://127.0.0.1:1/v1", "备用模型", label="备用模型")
        backup.complete_only = mock.Mock(side_effect=hang)
        client = FallbackChatClient(
            [primary, backup],
            hedge_after_seconds=0.05,
            total_budget_seconds=0.4,
        )
        started = time.monotonic()
        try:
            with self.assertRaises(ChatLLMError):
                client.send("hi")
            elapsed = time.monotonic() - started
        finally:
            client.close()
        self.assertLess(elapsed, 1.2, f"到预算就该放弃，实际等了 {elapsed:.2f}s")


class BuildFromEnvTest(unittest.TestCase):
    def test_missing_config_returns_none(self):
        self.assertIsNone(build_chat_client_from_env({}))

    def test_primary_only(self):
        client = build_chat_client_from_env(
            {
                "HUB_CHAT_BASE_URL": "http://192.168.0.245:8888/v1/",
                "HUB_CHAT_MODEL": "deepseek-v4.1-flash",
            }
        )
        self.assertIsInstance(client, ChatLLMClient)
        self.assertEqual(client.base_url, "http://192.168.0.245:8888/v1")

    def test_with_fallback(self):
        client = build_chat_client_from_env(
            {
                "HUB_CHAT_BASE_URL": "http://127.0.0.1:1/v1",
                "HUB_CHAT_MODEL": "a",
                "HUB_CHAT_FALLBACK_BASE_URL": "https://sub2api.vx123.xin/v1",
                "HUB_CHAT_FALLBACK_MODEL": "gpt-6-luna",
                "HUB_CHAT_TIMEOUT": "15",
            }
        )
        self.assertIsInstance(client, FallbackChatClient)
        self.assertEqual(len(client.clients), 2)
        self.assertEqual(client.clients[0]._model.timeout_seconds, 15.0)


class ConversationHistoryTest(unittest.TestCase):
    """同一个 conversation_id 下，模型要记得上一句说过什么。

    2026-11-06 现场：说完「叫麦德」，下一句问「我叫什么名字」，
    它答「你还没给我起名字呢」—— 刚答应就忘，因为每句都是一次全新请求。
    """

    def _client(self, **kwargs):
        return ChatLLMClient(
            "http://127.0.0.1:1/v1",
            "test-model",
            "k",
            system_prompt="你是桌宠。",
            voice_prefix="（口语回答）",
            **kwargs,
        )

    def test_second_turn_carries_first_turn(self):
        client = self._client()
        with mock.patch.object(client._model, "complete", return_value=_reply("好呀，就叫麦德。")) as spy:
            client.send("你叫麦德", conversation_id="c1")
        with mock.patch.object(client._model, "complete", return_value=_reply("你叫麦德。")) as spy:
            client.send("我叫什么名字", conversation_id="c1")

        messages = spy.call_args[0][0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1], {"role": "user", "content": "你叫麦德"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "好呀，就叫麦德。"})
        self.assertEqual(messages[-1]["content"], "（口语回答）\n我叫什么名字")

    def test_history_keeps_original_text_without_prefix(self):
        # 前缀只该出现在当前这一句，历史里再存一遍等于每轮多烧一份 token。
        client = self._client()
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")):
            client.send("第一句", conversation_id="c1")
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")) as spy:
            client.send("第二句", conversation_id="c1")

        contents = [message["content"] for message in spy.call_args[0][0]]
        self.assertIn("第一句", contents)
        self.assertEqual(contents.count("（口语回答）\n第一句"), 0)

    def test_raw_call_never_carries_history(self):
        client = self._client()
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")):
            client.send("你叫麦德", conversation_id="c1")
        with mock.patch.object(client._model, "complete", return_value=_reply('{"intent":"chat"}')) as spy:
            client.send("判意图", raw=True, conversation_id="c1")

        # 意图分类只发一条 user，历史掺进去会把 JSON 带偏
        self.assertEqual(len(spy.call_args[0][0]), 1)

    def test_different_conversations_are_isolated(self):
        client = self._client()
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")):
            client.send("你叫麦德", conversation_id="c1")
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")) as spy:
            client.send("你好", conversation_id="c2")

        self.assertEqual(len(spy.call_args[0][0]), 2)  # system + 这一句

    def test_idle_conversation_expires(self):
        client = self._client(history_ttl_seconds=0.0)
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")):
            client.send("你叫麦德", conversation_id="c1")
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")) as spy:
            client.send("我叫什么名字", conversation_id="c1")

        self.assertEqual(len(spy.call_args[0][0]), 2)

    def test_history_is_capped(self):
        client = self._client(history_turns=2)
        for index in range(5):
            with mock.patch.object(client._model, "complete", return_value=_reply(f"第{index}答")):
                client.send(f"第{index}问", conversation_id="c1")
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")) as spy:
            client.send("最后一句", conversation_id="c1")

        messages = spy.call_args[0][0]
        # system + 2 轮（4 条）+ 当前这句
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[1]["content"], "第3问")


class HistoryPersistenceTest(unittest.TestCase):
    """历史落盘：重启不丢对话，过期的对话不复活，坏文件不致命。"""

    def setUp(self):
        import tempfile, os
        self._dir = tempfile.mkdtemp(prefix="chat-hist-")
        self._path = os.path.join(self._dir, "history.json")

    def _client(self, **kwargs):
        return ChatLLMClient(
            "http://127.0.0.1:1/v1",
            "test-model",
            "k",
            system_prompt="你是桌宠。",
            history_path=self._path,
            **kwargs,
        )

    def test_roundtrip_across_instances(self):
        first = self._client()
        with mock.patch.object(first._model, "complete", return_value=_reply("好呀，就叫小灰。")):
            first.send("你就叫小灰", conversation_id="c1")

        second = self._client()
        self.assertIn("你就叫小灰", second.history_text("c1"))
        self.assertIn("好呀，就叫小灰。", second.history_text("c1"))

    def test_expired_history_is_not_revived(self):
        import json, os, time
        first = self._client()
        with mock.patch.object(first._model, "complete", return_value=_reply("嗯。")):
            first.send("你叫麦德", conversation_id="c1")
        # 手动把落盘时间拨到 TTL 之外
        with open(self._path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["c1"]["wall"] = time.time() - 400.0
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

        second = self._client()
        self.assertEqual(second.history_text("c1"), "")

    def test_corrupt_file_starts_fresh(self):
        with open(self._path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        client = self._client()  # 不应抛异常
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")):
            client.send("你好", conversation_id="c1")
        self.assertIn("你好", client.history_text("c1"))

    def test_no_path_stays_memory_only(self):
        client = ChatLLMClient("http://127.0.0.1:1/v1", "test-model", "k")
        with mock.patch.object(client._model, "complete", return_value=_reply("嗯。")):
            client.send("你好", conversation_id="c1")
        self.assertIsNone(client.history_path)


class FallbackHistoryTest(unittest.TestCase):
    def test_fallback_passes_conversation_id(self):
        from agent_gateway.chat_llm import FallbackChatClient

        primary = ChatLLMClient("http://127.0.0.1:1/v1", "a", "k")
        backup = ChatLLMClient("http://127.0.0.1:2/v1", "b", "k")
        client = FallbackChatClient([primary, backup])

        with mock.patch.object(
            primary._model, "complete", side_effect=ChatModelError("连不上")
        ), mock.patch.object(
            backup._model, "complete", return_value=_reply("备用答")
        ) as spy:
            client.send("你好", conversation_id="c1")

        # 备用端点也要接住上下文，否则切一次就失忆
        self.assertEqual(spy.call_args[0][0][-1]["content"].endswith("你好"), True)


if __name__ == "__main__":
    unittest.main()
