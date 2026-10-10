from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


DEVICE_NAMES = {
    "recording_bean": "录音豆",
    "speaker": "音箱",
    "smart_speaker": "智能音箱",
    "mic": "麦克风",
}


def is_real_device(device: Dict[str, Any]) -> bool:
    metadata = device.get("metadata") or {}
    identity = " ".join(
        str(device.get(key, ""))
        for key in ("device_id", "device_type", "firmware_version")
    ).lower()
    identity += " " + " ".join(
        str(metadata.get(key, ""))
        for key in ("display_name", "model", "source")
    ).lower()
    return not any(marker in identity for marker in ("simulator", "simulation", "-sim", "_sim"))


def device_display_name(device: Dict[str, Any]) -> str:
    metadata = device.get("metadata") or {}
    return str(
        metadata.get("display_name")
        or DEVICE_NAMES.get(device.get("device_type"), device.get("device_type", "智能设备"))
    )


def format_device_state(
    device: Dict[str, Any],
    state: Optional[Dict[str, Any]],
) -> str:
    online = device.get("status") == "online"
    if not state or not state.get("available"):
        last_seen = device.get("last_seen") or "未知"
        return (
            (
                "设备当前离线，暂无可用的历史状态。\n"
                f"最近连接时间：{last_seen}"
            )
            if not online
            else "设备在线，但当前没有可用的设备状态。"
        )

    lines = []
    if device.get("device_type") == "recording_bean":
        lines.append(f"录音：{'录音中' if state.get('recording') else '未录音'}")
        total = state.get("sd_total_mb")
        free = state.get("sd_free_mb")
        if total is not None and free is not None:
            lines.append(f"TF 卡：{free} / {total} MB 可用")
        if state.get("wifi_connected"):
            wifi = f"Wi-Fi：已连接 {state.get('wifi_ip') or ''}".strip()
            if state.get("wifi_rssi_dbm") is not None:
                wifi += f"（{state['wifi_rssi_dbm']} dBm）"
            lines.append(wifi)
        else:
            lines.append("Wi-Fi：未连接")
        lines.append(f"MQTT：{'已连接' if state.get('mqtt_connected') else '未连接'}")
        lines.append(f"当前文件：{state.get('current_file') or '无'}")
    else:
        lines.append(f"设备状态：{'在线' if online else '离线'}")
        if state.get("wifi_connected") is not None:
            lines.append(f"网络：{'已连接' if state.get('wifi_connected') else '未连接'}")

    updated_at = state.get("updated_at") or "未知"
    if online:
        lines.append(f"状态更新时间：{updated_at}")
    else:
        lines.insert(0, "设备当前离线，以下为最近一次有效状态")
        lines.append(f"最近状态（设备离线）：{updated_at}")
    return "\n".join(lines)


class DeviceCard(QFrame):
    selected = Signal(str)

    def __init__(
        self,
        device: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.state = state
        self.device_id = str(device.get("device_id", ""))
        self.setObjectName("deviceCard")
        self.setProperty("selected", False)
        self.setStyleSheet(
            """
            QFrame#deviceCard { background: #24202d; border: 1px solid #3c3450; border-radius: 12px; }
            QFrame#deviceCard[selected="true"] { border: 2px solid #8d73e8; }
            QLabel#deviceCardName { color: #f4f1f2; font-size: 16px; font-weight: 700; }
            QLabel#deviceCardOnline { color: #70e6ad; background: #173d32; border-radius: 9px; padding: 3px 8px; }
            QLabel#deviceCardOffline { color: #ff9ba8; background: #492631; border-radius: 9px; padding: 3px 8px; }
            QLabel#deviceCardId, QLabel#deviceCardMeta, QLabel#deviceCardCapabilities { color: #a9a1b4; }
            QLabel#deviceCardState { color: #e9e3ee; }
            """
        )
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(7)

        header = QGridLayout()
        name = QLabel(device_display_name(self.device))
        name.setObjectName("deviceCardName")
        header.addWidget(name, 0, 0)
        status = QLabel("在线" if self.device.get("status") == "online" else "离线")
        status.setObjectName("deviceCardOnline" if self.device.get("status") == "online" else "deviceCardOffline")
        status.setAlignment(Qt.AlignCenter)
        header.addWidget(status, 0, 1)
        header.setColumnStretch(0, 1)
        layout.addLayout(header)

        device_id = QLabel(self.device_id)
        device_id.setObjectName("deviceCardId")
        layout.addWidget(device_id)

        firmware = self.device.get("firmware_version") or "固件未知"
        type_label = QLabel(f"{self.device.get('device_type', 'unknown')} · {firmware}")
        type_label.setObjectName("deviceCardMeta")
        layout.addWidget(type_label)

        state_label = QLabel(format_device_state(self.device, self.state))
        state_label.setObjectName("deviceCardState")
        state_label.setWordWrap(True)
        state_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(state_label, 1)

        capabilities = ", ".join(self.device.get("capabilities") or []) or "无能力描述"
        capability_label = QLabel(f"可用能力：{capabilities}")
        capability_label.setObjectName("deviceCardCapabilities")
        capability_label.setWordWrap(True)
        layout.addWidget(capability_label)

        for child in (
            name,
            status,
            device_id,
            type_label,
            state_label,
            capability_label,
        ):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.selected.emit(self.device_id)
        super().mousePressEvent(event)


class DeviceCardWall(QWidget):
    device_selected = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cards: Dict[str, DeviceCard] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("deviceScroll")
        self.container = QWidget()
        self.grid = QGridLayout(self.container)
        self.grid.setContentsMargins(4, 4, 12, 4)
        self.grid.setSpacing(12)
        self.empty_label = QLabel("暂无真实注册设备")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #a9a1b4; font-size: 16px; padding: 40px;")
        self.grid.addWidget(self.empty_label, 0, 0, 1, 2)
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll)

    def update_snapshot(
        self,
        devices: Iterable[Dict[str, Any]],
        states: Dict[str, Dict[str, Any]],
    ) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards.clear()
        self.empty_label = QLabel("暂无真实注册设备")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #a9a1b4; font-size: 16px; padding: 40px;")
        self.grid.addWidget(self.empty_label, 0, 0, 1, 2)
        real_devices = [device for device in devices if is_real_device(device)]
        self.empty_label.setVisible(not real_devices)
        for index, device in enumerate(real_devices):
            device_id = str(device.get("device_id", ""))
            card = DeviceCard(device, states.get(device_id))
            card.selected.connect(self._select_card)
            self.cards[device_id] = card
            self.grid.addWidget(card, index // 2, index % 2)
        self.grid.setRowStretch((len(real_devices) + 1) // 2, 1)

    def _select_card(self, device_id: str) -> None:
        card = self.cards.get(device_id)
        if card is None:
            return
        self.device_selected.emit(device_id, device_display_name(card.device))

    def set_selected(self, device_id: Optional[str]) -> None:
        for current_id, card in self.cards.items():
            card.setProperty("selected", current_id == device_id)
            card.style().unpolish(card)
            card.style().polish(card)
            card.update()
