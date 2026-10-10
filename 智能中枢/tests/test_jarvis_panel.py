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

    def test_system_message_is_not_user_message(self):
        panel = JarvisPanel()
        panel.add_system_message("中枢连接失败")

        bubble = panel.chat.itemWidget(panel.chat.item(0))
        self.assertEqual(bubble.role, "system")
        self.assertEqual(bubble.horizontal_alignment, "left")


if __name__ == "__main__":
    unittest.main()
