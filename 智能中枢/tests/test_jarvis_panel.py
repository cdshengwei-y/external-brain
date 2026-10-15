import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop_app.jarvis_panel import ChatBubble, JarvisPanel


class JarvisPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication(sys.argv)

    def test_pending_requests_do_not_disable_composer(self):
        panel = JarvisPanel()
        panel.add_pending_request("request-1")
        panel.add_pending_request("request-2")

        self.assertTrue(panel.message_entry.isEnabled())
        self.assertEqual(len(panel.pending_items), 2)
        self.assertEqual(panel.chat.count(), 2)

        panel.finish_request("request-1", "第一条已完成。", True, "success")
        self.assertTrue(panel.message_entry.isEnabled())
        self.assertEqual(len(panel.pending_items), 1)
        first_bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertIsInstance(first_bubble, ChatBubble)
        self.assertEqual(first_bubble.status_key, "success")
        self.assertEqual(first_bubble.message_label.text(), "第一条已完成。")

    def test_audio_button_is_available_alongside_text_composer(self):
        panel = JarvisPanel()

        self.assertTrue(panel.audio_button.isEnabled())
        self.assertTrue(panel.message_entry.isEnabled())

    def test_user_and_jarvis_messages_use_opposite_sides_and_styles(self):
        panel = JarvisPanel()
        panel.add_user_message("你好")
        panel.add_pending_request("request-1")

        user_bubble = panel.chat.itemWidget(panel.chat.item(0))
        jarvis_bubble = panel.chat.itemWidget(panel.chat.item(1))

        self.assertEqual(user_bubble.role, "user")
        self.assertEqual(user_bubble.horizontal_alignment, "right")
        self.assertTrue(user_bubble.header_widget.isHidden())
        self.assertEqual(jarvis_bubble.role, "assistant")
        self.assertEqual(jarvis_bubble.horizontal_alignment, "left")
        self.assertNotEqual(
            user_bubble.bubble.objectName(),
            jarvis_bubble.bubble.objectName(),
        )

    def test_multiline_response_stays_in_one_bubble(self):
        panel = JarvisPanel()
        panel.add_pending_request("request-1")
        panel.finish_request("request-1", "第一行\n第二行", True, "success")

        self.assertEqual(panel.chat.count(), 1)
        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertEqual(bubble.message_label.text(), "第一行\n第二行")

    def test_chat_response_hides_execution_status(self):
        panel = JarvisPanel()
        panel.add_pending_request("request-1")
        panel.finish_request("request-1", "你好，我在。", True, "chat")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertEqual(bubble.status_key, "chat")
        self.assertTrue(bubble.status_label.isHidden())
        self.assertGreaterEqual(bubble.bubble.minimumWidth(), 220)

    def test_assistant_reply_exposes_speech_button(self):
        panel = JarvisPanel()
        requested = []
        panel.speech_requested.connect(requested.append)
        panel.add_pending_request("request-1")
        panel.finish_request("request-1", "你好，我在。", True, "chat")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertTrue(bubble.speech_button.isEnabled())
        self.assertEqual(bubble.speech_button.property("speechAvailable"), True)
        self.assertEqual(bubble.speech_button.text(), "播放")
        bubble.speech_button.click()
        self.assertEqual(requested, [bubble])

    def test_speech_button_uses_clear_labels_for_each_state(self):
        panel = JarvisPanel()
        panel.add_pending_request("request-1")
        panel.finish_request("request-1", "你好，我在。", True, "chat")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        bubble.set_speech_state("playing")
        self.assertEqual(bubble.speech_button.text(), "停止")
        bubble.set_speech_state("error")
        self.assertEqual(bubble.speech_button.text(), "重试")

    def test_pending_reply_hides_speech_button(self):
        panel = JarvisPanel()
        panel.add_pending_request("request-1")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertFalse(bubble.speech_button.isEnabled())
        self.assertEqual(bubble.speech_button.property("speechAvailable"), False)

    def test_system_message_is_not_user_message(self):
        panel = JarvisPanel()
        panel.add_system_message("中枢连接失败")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertEqual(bubble.role, "system")
        self.assertEqual(bubble.horizontal_alignment, "left")

    def test_plan_message_shows_confirmation_button_and_confirmation_state(self):
        panel = JarvisPanel()
        requested = []
        panel.plan_confirmation_requested.connect(requested.append)
        panel.add_pending_request("request-1")
        panel.finish_plan("request-1", "准备开始录音，请确认。")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertEqual(bubble.status_key, "planned")
        self.assertIsNotNone(bubble.confirm_button)
        bubble.confirm_button.click()
        self.assertEqual(requested, ["request-1"])

        panel.confirm_request("request-1")
        self.assertEqual(bubble.status_key, "confirming")
        self.assertFalse(bubble.confirm_button.isVisible())

    def test_confirm_request_accepts_custom_execution_message(self):
        panel = JarvisPanel()
        panel.add_pending_request("request-1")
        panel.finish_plan("request-1", "准备查询状态。")

        panel.confirm_request("request-1", "已识别，正在查询设备状态…")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertEqual(bubble.message_label.text(), "已识别，正在查询设备状态…")
        self.assertEqual(bubble.status_key, "confirming")


if __name__ == "__main__":
    unittest.main()
