from __future__ import annotations

import math
from typing import Dict

from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class EnergyOrb(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status_key = "idle"
        self.phase = 0.0
        self.setMinimumSize(150, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(45)

    def set_status(self, status_key: str) -> None:
        self.status_key = status_key
        self.update()

    def _animate(self) -> None:
        self.phase = (self.phase + 0.045) % 1.0
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) * 0.25
        colors = {
            "idle": (QColor("#6b63c7"), QColor("#2b2754")),
            "thinking": (QColor("#4fbcff"), QColor("#153a69")),
            "executing": (QColor("#a27bff"), QColor("#432875")),
            "success": (QColor("#54e3a2"), QColor("#1a604c")),
            "error": (QColor("#ff7386"), QColor("#6b263d")),
        }
        bright, dark = colors.get(self.status_key, colors["idle"])
        pulse = 0.84 + 0.16 * (0.5 + 0.5 * math.sin(self.phase * 6.283))

        for ring_scale, alpha in ((1.9, 26), (1.55, 38), (1.3, 54)):
            ring_radius = radius * ring_scale * pulse
            painter.setPen(QPen(QColor(bright.red(), bright.green(), bright.blue(), alpha), 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, ring_radius, ring_radius)

        gradient = QRadialGradient(center, radius * 1.25)
        gradient.setColorAt(0.0, QColor(255, 255, 255, 235))
        gradient.setColorAt(0.25, QColor(bright.red(), bright.green(), bright.blue(), 245))
        gradient.setColorAt(1.0, QColor(dark.red(), dark.green(), dark.blue(), 245))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, radius, radius)

        orbit_radius = radius * 1.65
        for index in range(3):
            angle = self.phase * 6.283 + index * 2.094
            point = QPointF(
                center.x() + orbit_radius * math.cos(angle),
                center.y() + orbit_radius * math.sin(angle),
            )
            painter.setBrush(QColor(bright.red(), bright.green(), bright.blue(), 210))
            painter.drawEllipse(point, 3.5, 3.5)


class ChatBubble(QWidget):
    STATUS_TEXT = {
        "pending": "处理中",
        "success": "执行完成",
        "queued": "已排队",
        "failure": "执行失败",
        "timeout": "执行超时",
    }

    def __init__(
        self,
        role: str,
        message: str,
        status: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.horizontal_alignment = "right" if role == "user" else "left"
        self.status_key = status
        self.list_item: QListWidgetItem | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._build_ui(message)
        self._apply_status(status)

    def _build_ui(self, message: str) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(0)

        if self.horizontal_alignment == "right":
            outer.addStretch(1)

        self.bubble = QFrame()
        self.bubble.setObjectName(f"{self.role}Bubble")
        self.bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(13, 9, 13, 10)
        bubble_layout.setSpacing(4)

        self.header_widget = QWidget()
        header = QHBoxLayout(self.header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.role_label = QLabel({"user": "你", "assistant": "JARVIS"}.get(self.role, "系统"))
        self.role_label.setObjectName("bubbleRole")
        header.addWidget(self.role_label)
        header.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setObjectName("bubbleStatus")
        header.addWidget(self.status_label)
        bubble_layout.addWidget(self.header_widget)
        if self.role == "user":
            self.header_widget.hide()

        self.message_label = QLabel()
        self.message_label.setObjectName("bubbleMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bubble_layout.addWidget(self.message_label)
        self.set_message(message)

        outer.addWidget(self.bubble)
        if self.horizontal_alignment == "left":
            outer.addStretch(1)

    def set_message(self, message: str) -> None:
        self.message_label.setText(message)
        self.message_label.adjustSize()
        self.adjustSize()

    def set_status(self, status: str | None) -> None:
        self.status_key = status
        self._apply_status(status)
        self.adjustSize()

    def set_available_width(self, width: int) -> None:
        available_width = max(1, width)
        self.setFixedWidth(available_width)
        self.bubble.setMaximumWidth(max(220, int(available_width * 0.72)))
        self.bubble.updateGeometry()
        self.adjustSize()

    def _apply_status(self, status: str | None) -> None:
        if self.role != "assistant" or status is None:
            self.status_label.hide()
            return
        self.status_label.setText(self.STATUS_TEXT.get(status, status))
        self.status_label.setProperty("status", status)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_label.show()

    def resizeEvent(self, event) -> None:
        max_width = max(220, int(self.width() * 0.72))
        self.bubble.setMaximumWidth(max_width)
        super().resizeEvent(event)


class JarvisPanel(QWidget):
    message_submitted = Signal(str)

    STATUS_TEXT = {
        "idle": "待命",
        "thinking": "正在理解",
        "executing": "正在执行",
        "success": "执行完成",
        "error": "执行失败",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pending_items: Dict[str, ChatBubble] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(10)

        header = QHBoxLayout()
        identity = QVBoxLayout()
        title = QLabel("JARVIS")
        title.setObjectName("jarvisTitle")
        subtitle = QLabel("智能中枢 · 真实硬件控制")
        subtitle.setObjectName("jarvisSubtitle")
        identity.addWidget(title)
        identity.addWidget(subtitle)
        header.addLayout(identity)
        header.addStretch()
        self.status_label = QLabel("● 待命")
        self.status_label.setObjectName("jarvisStatus")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.orb = EnergyOrb()
        layout.addWidget(self.orb)

        self.target_label = QLabel("当前目标：自动选择在线设备")
        self.target_label.setObjectName("targetLabel")
        layout.addWidget(self.target_label)

        self.chat = QListWidget()
        self.chat.setObjectName("chatList")
        self.chat.setSpacing(8)
        self.chat.setUniformItemSizes(False)
        self.chat.setSelectionMode(QListWidget.NoSelection)
        self.chat.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.chat, 1)

        composer = QHBoxLayout()
        self.message_entry = QLineEdit()
        self.message_entry.setObjectName("messageEntry")
        self.message_entry.setPlaceholderText("输入中文命令，例如：查询状态")
        self.message_entry.returnPressed.connect(self._submit)
        composer.addWidget(self.message_entry, 1)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("sendButton")
        self.send_button.clicked.connect(self._submit)
        composer.addWidget(self.send_button)
        layout.addLayout(composer)

        self.setStyleSheet(
            """
            QListWidget#chatList {
                background: #15151d;
                border: 1px solid #2d2b3e;
                border-radius: 14px;
                padding: 10px 8px;
                outline: none;
            }
            QListWidget#chatList::item {
                background: transparent;
                border: none;
                padding: 0;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 4px 0;
            }
            QScrollBar::handle:vertical {
                background: #51486b;
                border-radius: 4px;
                min-height: 32px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QFrame#userBubble {
                background: #5d4db0;
                border: 1px solid #806edc;
                border-radius: 15px;
            }
            QFrame#assistantBubble {
                background: #27283a;
                border: 1px solid #46547b;
                border-left: 3px solid #7b6be0;
                border-radius: 15px;
            }
            QFrame#systemBubble {
                background: #3c3025;
                border: 1px solid #80633f;
                border-radius: 12px;
            }
            QLabel#bubbleRole {
                color: #e9e5ff;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#bubbleMessage {
                color: #f7f5ff;
                font-size: 13px;
            }
            QLabel#bubbleStatus {
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#bubbleStatus[status="pending"], QLabel#bubbleStatus[status="queued"] {
                color: #9fcbff;
            }
            QLabel#bubbleStatus[status="success"] {
                color: #70e6ad;
            }
            QLabel#bubbleStatus[status="failure"] {
                color: #ff9ba8;
            }
            QLabel#bubbleStatus[status="timeout"] {
                color: #ffcf74;
            }
            QLineEdit#messageEntry {
                background: #20202b;
                color: #f7f5ff;
                border: 1px solid #3a3850;
                border-radius: 12px;
                padding: 9px 13px;
                selection-background-color: #6555bd;
            }
            QLineEdit#messageEntry:focus {
                border: 1px solid #8171df;
            }
            QPushButton#sendButton {
                background: #5d4db0;
                color: #ffffff;
                border: 1px solid #8978e0;
                border-radius: 11px;
                padding: 9px 17px;
                font-weight: 700;
            }
            QPushButton#sendButton:hover {
                background: #7060c5;
            }
            QPushButton#sendButton:pressed {
                background: #493c8d;
            }
            """
        )

    def _submit(self) -> None:
        message = self.message_entry.text().strip()
        if not message:
            return
        self.message_entry.clear()
        self.message_submitted.emit(message)

    def set_target(self, device_name: str, device_id: str) -> None:
        self.target_label.setText(f"当前目标：{device_name} · {device_id}")

    def add_user_message(self, message: str) -> None:
        self._append_message("user", message)

    def add_pending_request(self, request_id: str) -> None:
        bubble = self._append_message(
            "assistant",
            "正在处理你的指令…",
            status="pending",
        )
        self.pending_items[request_id] = bubble

    def finish_request(
        self,
        request_id: str,
        message: str,
        success: bool,
        status: str = "success",
    ) -> None:
        bubble = self.pending_items.pop(request_id, None)
        resolved_status = status or ("success" if success else "failure")
        if bubble is None:
            self._append_message("assistant", message, status=resolved_status)
            return
        bubble.set_message(message)
        bubble.set_status(resolved_status)
        self._sync_item_size(bubble)
        self.chat.scrollToBottom()

    def add_system_message(self, message: str) -> None:
        self._append_message("system", message)

    def set_activity(self, status_key: str) -> None:
        status_text = self.STATUS_TEXT.get(status_key, self.STATUS_TEXT["idle"])
        self.status_label.setText(f"● {status_text}")
        self.orb.set_status(status_key)

    def _append_message(
        self,
        role: str,
        message: str,
        status: str | None = None,
    ) -> ChatBubble:
        bubble = ChatBubble(role, message, status=status)
        item = QListWidgetItem()
        self.chat.addItem(item)
        self.chat.setItemWidget(item, bubble)
        bubble.list_item = item
        self._sync_item_size(bubble)
        QTimer.singleShot(0, lambda: self._sync_item_size(bubble))
        self.chat.scrollToBottom()
        return bubble

    def _sync_item_size(self, bubble: ChatBubble) -> None:
        if bubble.list_item is not None:
            available_width = self.chat.viewport().width() - 2
            if available_width <= 0:
                available_width = self.chat.width() - 2
            bubble.set_available_width(available_width)
            bubble.list_item.setSizeHint(
                QSize(available_width, bubble.sizeHint().height())
            )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        for index in range(self.chat.count()):
            bubble = self.chat.itemWidget(self.chat.item(index))
            if isinstance(bubble, ChatBubble):
                self._sync_item_size(bubble)
