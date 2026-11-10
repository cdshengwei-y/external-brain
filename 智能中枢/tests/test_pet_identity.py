"""桌宠身份（名字）与端点熔断的单元测试。

两个都来自 2026-11-06 第四轮真机对话：
1. 问「你叫什么？」，本地模型答「小桌」，备用模型答「ChatGPT」—— 提示词里
   压根没有身份；用户随后说「你应该叫幻形之了」，模型嘴上答应，重启就忘。
2. 本地 sglang 卡死时，一次请求要 20 秒超时 + 4.7 秒备用 = 24.7 秒才出声。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from agent_gateway import pet_identity
from agent_gateway.chat_llm import ChatLLMClient, FallbackChatClient
from agent_gateway.pet_agent import ChatModelError


def _reply(content):
    return {"content": content, "tool_calls": [], "raw": {}}


class RenameDetectionTest(unittest.TestCase):
    """改名句式要认得准，问句一律不许误判。"""

    def test_accepts_explicit_rename(self):
        cases = {
            "我用幻形之体，幻形，你应该叫幻形之了。": "幻形之",
            "你以后就叫小灰吧": "小灰",
            "从今天起喊你小知": "小知",
            "给你起个名字叫阿雅": "阿雅",
            "你就叫大白": "大白",
        }
        for text, expected in cases.items():
            self.assertEqual(pet_identity.detect_rename(text), expected, text)

    def test_ignores_questions_and_chatter(self):
        for text in (
            "你叫什么？",
            "你应该叫什么？",
            "我叫小明，你以后这么叫我",
            "那叫你小助手可以吗",
            "等一下吧",
            "今天天气怎么样",
        ):
            self.assertIsNone(pet_identity.detect_rename(text), text)


class IdentityStoreTest(unittest.TestCase):
    """名字要落盘：进程重启后还是那个名字。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._dir.name, "pet_identity.json")
        patcher = mock.patch.dict(os.environ, {"PET_IDENTITY_PATH": self._path})
        patcher.start()
        self.addCleanup(patcher.stop)
        pet_identity.reset_cache()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(pet_identity.reset_cache)

    def test_default_name_matches_wake_word(self):
        self.assertEqual(pet_identity.assistant_name(), "贾维斯")

    def test_rename_survives_restart(self):
        self.assertTrue(pet_identity.set_assistant_name("幻形之"))
        # 模拟重启：丢掉内存缓存，只从磁盘读
        pet_identity.reset_cache()
        self.assertEqual(pet_identity.assistant_name(), "幻形之")

    def test_rejects_empty_or_absurd_names(self):
        self.assertFalse(pet_identity.set_assistant_name(""))
        self.assertFalse(pet_identity.set_assistant_name("什么"))
        self.assertFalse(pet_identity.set_assistant_name("这是一个特别长的名字"))
        self.assertEqual(pet_identity.assistant_name(), "贾维斯")

    def test_identity_line_carries_name_and_forbids_model_names(self):
        pet_identity.set_assistant_name("小灰")
        line = pet_identity.identity_line()
        self.assertIn("小灰", line)
        self.assertIn("ChatGPT", line)

    def test_external_rename_is_visible_without_restart(self):
        # 另一个进程（命令行工具 / 第二份中枢）改了文件，跑着的进程要能看见。
        self.assertEqual(pet_identity.assistant_name(), "贾维斯")
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump({"assistant_name": "小灰"}, handle, ensure_ascii=False)
        os.utime(self._path, (time.time() + 5, time.time() + 5))
        self.assertEqual(pet_identity.assistant_name(), "小灰")


class ResetDetectionTest(unittest.TestCase):
    """名字能改，就必须能改回来 —— 用户原话：「默认就叫贾维斯」。"""

    def test_accepts_reset_phrases(self):
        for text in (
            "改回默认名字",
            "恢复原名",
            "恢复默认",
            "你还是叫贾维斯吧",
            "叫回贾维斯",
            "名字改回贾维斯",
            "别再叫幻形之了，还是叫贾维斯吧",
        ):
            self.assertTrue(pet_identity.detect_reset(text), text)

    def test_does_not_hijack_normal_talk(self):
        for text in (
            "你叫什么名字？",
            "你应该叫什么？",
            "等一下吧",
            "你退下吧",
            "那你就叫小灰吧",
            "今天天气怎么样",
            "别再叫贾维斯了",
        ):
            self.assertFalse(pet_identity.detect_reset(text), text)


class DefaultNameTest(unittest.TestCase):
    """默认名 = 唤醒词，不写死第二份。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._dir.name, "pet_identity.json")
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"PET_IDENTITY_PATH": self._path})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PET_WAKE_WORDS", None)
        self.addCleanup(os.environ.pop, "PET_WAKE_WORDS", None)
        pet_identity.reset_cache()
        self.addCleanup(pet_identity.reset_cache)

    def test_default_is_jarvis(self):
        self.assertEqual(pet_identity.wake_word(), "贾维斯")
        self.assertEqual(pet_identity.assistant_name(), "贾维斯")

    def test_default_follows_configured_wake_word(self):
        os.environ["PET_WAKE_WORDS"] = "小豆"
        pet_identity.reset_cache()
        self.assertEqual(pet_identity.assistant_name(), "小豆")

    def test_renaming_back_to_default_clears_the_file(self):
        pet_identity.set_assistant_name("幻形之")
        self.assertTrue(pet_identity.set_assistant_name("贾维斯"))
        pet_identity.reset_cache()
        self.assertEqual(pet_identity.assistant_name(), "贾维斯")
        with open(self._path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {}, "改回默认应该删键，而不是写死同名")

    def test_reset_is_idempotent(self):
        self.assertFalse(pet_identity.reset_assistant_name(), "没改过名时重置应当无动作")

    def test_identity_line_mentions_call_word_after_rename(self):
        pet_identity.set_assistant_name("小灰")
        line = pet_identity.identity_line()
        self.assertIn("小灰", line)
        self.assertIn("贾维斯", line)
        self.assertIn("唤醒词", line)


class SystemPromptIdentityTest(unittest.TestCase):
    """改名后系统提示词要立刻跟着变，不能等重启。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"PET_IDENTITY_PATH": os.path.join(self._dir.name, "id.json")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        pet_identity.reset_cache()
        self.addCleanup(pet_identity.reset_cache)

    def test_default_prompt_contains_identity(self):
        client = ChatLLMClient("http://127.0.0.1:1/v1", "m", "k")
        with mock.patch.object(client._model, "complete", return_value=_reply("hi")) as spy:
            client.send("你叫什么")
        messages = spy.call_args[0][0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("贾维斯", messages[0]["content"])

    def test_prompt_follows_rename_without_restart(self):
        client = ChatLLMClient("http://127.0.0.1:1/v1", "m", "k")
        pet_identity.set_assistant_name("小灰")
        with mock.patch.object(client._model, "complete", return_value=_reply("hi")) as spy:
            client.send("你叫什么")
        self.assertIn("小灰", spy.call_args[0][0][0]["content"])

    def test_raw_call_has_no_system_prompt(self):
        client = ChatLLMClient("http://127.0.0.1:1/v1", "m", "k")
        with mock.patch.object(client._model, "complete", return_value=_reply("{}")) as spy:
            client.send("判意图", raw=True)
        self.assertEqual([m["role"] for m in spy.call_args[0][0]], ["user"])


class CircuitBreakerTest(unittest.TestCase):
    """挂掉的端点进冷却期，后续请求不再为它白等。"""

    def _client(self):
        primary = ChatLLMClient("http://127.0.0.1:1/v1", "primary", "k", timeout_seconds=1)
        backup = ChatLLMClient("http://127.0.0.1:2/v1", "backup", "k", timeout_seconds=1)
        return primary, backup, FallbackChatClient([primary, backup], cooldown_seconds=60)

    def test_first_failure_falls_back(self):
        primary, backup, client = self._client()
        with mock.patch.object(primary._model, "complete", side_effect=ChatModelError("挂了")):
            with mock.patch.object(backup._model, "complete", return_value=_reply("备用答复")):
                self.assertEqual(client.send("你好"), "备用答复")

    def test_second_call_skips_the_dead_endpoint(self):
        primary, backup, client = self._client()
        with mock.patch.object(
            primary._model, "complete", side_effect=ChatModelError("挂了")
        ) as primary_spy:
            with mock.patch.object(
                backup._model, "complete", return_value=_reply("备用答复")
            ) as backup_spy:
                client.send("第一句")
                client.send("第二句")
        self.assertEqual(primary_spy.call_count, 1, "冷却期内不该再打挂掉的端点")
        self.assertEqual(backup_spy.call_count, 2)

    def test_success_clears_cooldown(self):
        primary, backup, client = self._client()
        with mock.patch.object(primary._model, "complete", side_effect=ChatModelError("挂了")):
            with mock.patch.object(backup._model, "complete", return_value=_reply("备用")):
                client.send("挂了")
        # 到期后主端点恢复：应重新被尝试
        client._down_until.clear()
        with mock.patch.object(primary._model, "complete", return_value=_reply("主端点答复")):
            self.assertEqual(client.send("好了"), "主端点答复")
        self.assertEqual(client._down_until, {})

    def test_all_endpoints_down_still_tries(self):
        primary, backup, client = self._client()
        client._down_until = {0: 9e9, 1: 9e9}
        with mock.patch.object(primary._model, "complete", return_value=_reply("复活")):
            self.assertEqual(client.send("你好"), "复活")


if __name__ == "__main__":
    unittest.main()
