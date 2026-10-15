import os
import sys
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop_app.upper_computer import UpperComputerApp


class UpperComputerRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication(sys.argv)

    def _app_with_response(self, response):
        app = UpperComputerApp.__new__(UpperComputerApp)
        app.conversation_id = "conversation-test"
        app.rk3588 = Mock()
        app.rk3588.route_message.return_value = response
        return app

    def test_status_read_plan_preserves_no_confirmation_policy(self):
        app = self._app_with_response(
            {
                "route": "command",
                "status": "planned",
                "plan_id": "plan-status",
                "reply": "准备查询状态。",
                "requires_confirmation": False,
                "command": {
                    "device_id": "bean_ye_1",
                    "capability": "status.read",
                    "params": {},
                },
            }
        )

        result = app._run_agent_request("request-status", "bean_ye_1", "查询状态")

        self.assertFalse(result["requires_confirmation"])
        self.assertEqual(result["status"], "planned")

    def test_route_response_displays_only_reply_field(self):
        app = self._app_with_response(
            {
                "route": "chat",
                "reply": "今日新闻摘要",
                "sources": [{"url": "https://example.com"}],
                "route_detail": "web_search",
                "search_query": "今日新闻",
                "error_code": "WEB_SUMMARY_UNAVAILABLE",
            }
        )

        result = app._run_agent_request("request-news", None, "讲一个今天的新闻")

        self.assertEqual(result["response"], "今日新闻摘要")

    def test_route_response_without_reply_does_not_expose_content_field(self):
        app = self._app_with_response(
            {
                "route": "chat",
                "content": "内部模型内容",
                "sources": [{"url": "https://example.com"}],
            }
        )

        result = app._run_agent_request("request-no-reply", None, "你好")

        self.assertEqual(result["response"], "我暂时没有可用的回复。")

    def test_record_start_plan_requires_confirmation_by_default(self):
        app = self._app_with_response(
            {
                "route": "command",
                "status": "planned",
                "plan_id": "plan-start",
                "reply": "准备开始录音。",
                "command": {
                    "device_id": "bean_ye_1",
                    "capability": "record.start",
                    "params": {},
                },
            }
        )

        result = app._run_agent_request("request-start", "bean_ye_1", "开始录音")

        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["status"], "planned")

    def test_status_read_plan_dispatches_without_confirmation(self):
        app = UpperComputerApp.__new__(UpperComputerApp)
        app.pending_plans = {}
        app.execution_to_plan = {}
        app.active_requests = 1
        app.jarvis_panel = Mock()
        app._run_background = Mock()

        app._finish_agent_request(
            {
                "request_id": "request-status",
                "response": "准备查询状态。",
                "success": False,
                "status": "planned",
                "plan_id": "plan-status",
                "plan": {
                    "command": {
                        "device_id": "bean_ye_1",
                        "capability": "status.read",
                        "params": {},
                    }
                },
                "device_id": "bean_ye_1",
                "requires_confirmation": False,
            }
        )

        app.jarvis_panel.confirm_request.assert_called_once_with(
            "request-status",
            "已识别，正在查询设备状态…",
        )
        app.jarvis_panel.finish_plan.assert_not_called()
        app._run_background.assert_called_once()
        self.assertEqual(app.active_requests, 1)

    def test_portable_audio_plan_keeps_confirmation_context(self):
        app = UpperComputerApp.__new__(UpperComputerApp)

        result = app._portable_request_result(
            "desktop-request",
            {
                "request_id": "desktop-request",
                "state": "planned",
                "response_text": "准备开始录音，请确认。",
                "plan_id": "plan-audio",
                "intent": {
                    "device_id": "bean_ye_1",
                    "capability": "record.start",
                    "params": {},
                },
            },
        )

        self.assertEqual(result["transport"], "portable")
        self.assertEqual(result["portable_request_id"], "desktop-request")
        self.assertEqual(result["plan"]["command"]["capability"], "record.start")
        self.assertTrue(result["requires_confirmation"])


if __name__ == "__main__":
    unittest.main()
