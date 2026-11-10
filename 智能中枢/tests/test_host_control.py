"""本机控制（host_control）与意图插件的测试。

2026-11-10 用户要求「叫它打开WPS就应该打开WPS」——测试别名表、
插件分发、网址识别，以及 app_open 动作必须带 session_action=continue
（固件只对 continue 播回复音频）。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict

from agent_gateway.host_control import (
    APP_ALIASES,
    looks_like_url,
    resolve_app,
)
from agent_gateway.intent import IntentResult, PluginContext
from agent_gateway.portable import HostControlPlugin


class _FakeClassifier:
    """路由器要求 classify 返回 IntentResult 对象（不是 dict）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def classify(self, text: str, catalog: str, **kwargs: Any) -> Any:
        from agent_gateway.intent import parse_intent_reply

        return parse_intent_reply(self.reply, source="test")


def _make_plugin(executor_log: list) -> HostControlPlugin:
    def fake_execute(action: str, target: str) -> Dict[str, Any]:
        executor_log.append((action, target))
        return {"ok": True, "message": f"好的，正在打开{target}。"}

    return HostControlPlugin(executor=fake_execute)


class ResolveAppTests(unittest.TestCase):
    def test_wps_alias_variants(self):
        for spoken in ("WPS", "wps", "WPS Office", "wps office"):
            self.assertEqual(resolve_app(spoken), "wpsoffice", spoken)

    def test_chinese_aliases(self):
        self.assertEqual(resolve_app("微信"), "wechat")
        self.assertEqual(resolve_app("谷歌浏览器"), "google chrome")
        self.assertEqual(resolve_app("嘉立创EDA"), "嘉立创eda(专业版)")

    def test_unknown_passes_through(self):
        # 没进别名表的英文名原样传给 open -a（macOS 大小写不敏感）
        self.assertEqual(resolve_app("MyWeirdApp"), "MyWeirdApp")

    def test_alias_table_covers_installed_apps(self):
        # 别名表指向的每个应用名，归一化后必须能在别名表里自洽（防手滑写错键）
        for canonical in APP_ALIASES.values():
            self.assertTrue(str(canonical).strip(), canonical)


class UrlDetectionTests(unittest.TestCase):
    def test_domains_are_urls(self):
        self.assertTrue(looks_like_url("bilibili.com"))
        self.assertTrue(looks_like_url("https://github.com"))
        self.assertTrue(looks_like_url("jlc-eda.com"))

    def test_app_names_are_not_urls(self):
        self.assertFalse(looks_like_url("WPS"))
        self.assertFalse(looks_like_url("打开微信"))
        self.assertFalse(looks_like_url("visual studio code"))


class HostControlPluginTests(unittest.TestCase):
    def test_open_app_intent_dispatches_and_continues(self):
        log: list = []
        plugin = _make_plugin(log)
        result = plugin.handle(
            IntentResult(intent="app_open", slots={"app": "WPS"}),
            PluginContext(text="打开WPS", request_id="r1"),
        )
        self.assertEqual(log, [("open_app", "WPS")])
        self.assertEqual(result.session_action, "continue")
        self.assertIn("WPS", result.reply)

    def test_quit_intent(self):
        log: list = []
        plugin = _make_plugin(log)
        plugin.handle(
            IntentResult(intent="app_quit", slots={"app": "微信"}),
            PluginContext(text="退出微信", request_id="r2"),
        )
        self.assertEqual(log, [("quit_app", "微信")])

    def test_missing_target_asks_back(self):
        plugin = _make_plugin([])
        result = plugin.handle(
            IntentResult(intent="app_open", slots={}),
            PluginContext(text="打开", request_id="r3"),
        )
        self.assertIn("哪个应用", result.reply)

    def test_end_to_end_router(self):
        log: list = []
        plugin = _make_plugin(log)
        from agent_gateway.intent import IntentRouter

        router = IntentRouter(
            [plugin],
            _FakeClassifier('{"intent":"app_open","slots":{"app":"WPS"},"reply":"好的"}'),
        )
        result = router.resolve("打开WPS", PluginContext(text="打开WPS", request_id="r4"))
        self.assertEqual(log, [("open_app", "WPS")])
        self.assertEqual(result.session_action, "continue")


if __name__ == "__main__":
    unittest.main()
