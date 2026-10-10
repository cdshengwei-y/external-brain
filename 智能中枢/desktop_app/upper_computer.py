from __future__ import annotations

import queue
import shutil
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from agent_gateway import AgentGatewayError, HubApiClient, TextAgent
from desktop_app.device_cards import DeviceCardWall, device_display_name, is_real_device
from desktop_app.jarvis_panel import JarvisPanel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "http://127.0.0.1:18787"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883


class HubRuntime:
    def __init__(self) -> None:
        self.broker_process: Optional[subprocess.Popen[Any]] = None
        self.hub_process: Optional[subprocess.Popen[Any]] = None

    def ensure_started(self) -> None:
        if not self._port_open(BROKER_HOST, BROKER_PORT):
            self._start_broker()
        if not self._health_ok():
            self._start_hub()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._health_ok():
                return
            time.sleep(0.25)
        raise RuntimeError("智能中枢未能启动，请检查 MQTT Broker 和本地服务日志。")

    def stop_started_processes(self) -> None:
        for process in (self.hub_process, self.broker_process):
            if process is None or process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def _start_broker(self) -> None:
        executable = shutil.which("mosquitto")
        if executable is None:
            for candidate in (
                "/opt/homebrew/sbin/mosquitto",
                "/usr/local/sbin/mosquitto",
            ):
                if Path(candidate).is_file():
                    executable = candidate
                    break
        if executable is None:
            raise RuntimeError("找不到 Mosquitto，请先安装 MQTT Broker。")
        config = PROJECT_ROOT / "config" / "mosquitto.lan.dev.conf"
        self.broker_process = subprocess.Popen(
            [executable, "-c", str(config)],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _start_hub(self) -> None:
        database = PROJECT_ROOT / ".data" / "hub.db"
        self.hub_process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "run.py"),
                "--host",
                "127.0.0.1",
                "--port",
                "18787",
                "--db",
                str(database),
                "--mqtt-host",
                "127.0.0.1",
                "--mqtt-port",
                "1883",
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            return False

    @staticmethod
    def _health_ok() -> bool:
        try:
            HubApiClient(HUB_URL, timeout_seconds=1).health()
            return True
        except AgentGatewayError:
            return False


class UpperComputerApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("外接大脑 · JARVIS 智能中枢")
        self.resize(1180, 780)
        self.setMinimumSize(980, 660)
        self.runtime = HubRuntime()
        self.hub = HubApiClient(HUB_URL)
        self.ui_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.conversation_id = f"desktop-{uuid.uuid4()}"
        self.selected_device_id: Optional[str] = None
        self.devices: list[Dict[str, Any]] = []
        self.active_requests = 0
        self._closed = False
        self._build_ui()
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._drain_ui_queue)
        self.queue_timer.start(100)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_snapshot)
        self.refresh_timer.start(3000)
        self._run_background(self._connect_runtime, "runtime")

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet(
            """
            QWidget#central { background: #17151b; color: #f4f1f2; }
            QLabel { color: #f4f1f2; }
            QLabel#sectionTitle { color: #f4f1f2; font-size: 18px; font-weight: 700; }
            QLabel#sectionSubtitle { color: #a9a1b4; }
            QLabel#connectionStatus { color: #71dfb0; }
            QPushButton { background: #393149; color: #f4f1f2; border: 1px solid #62547d; border-radius: 7px; padding: 8px 14px; }
            QPushButton:hover { background: #51436c; }
            QSplitter::handle { background: #30293c; }
            """
        )
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(20, 18, 20, 20)
        root_layout.setSpacing(14)

        header = QHBoxLayout()
        title_layout = QVBoxLayout()
        title = QLabel("外接大脑 · JARVIS")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("真实智能硬件控制中枢")
        subtitle.setObjectName("sectionSubtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header.addLayout(title_layout)
        header.addSpacing(24)
        self.connection_status = QLabel("正在连接中枢…")
        self.connection_status.setObjectName("connectionStatus")
        header.addWidget(self.connection_status)
        header.addStretch()
        refresh_button = QPushButton("刷新设备")
        refresh_button.clicked.connect(self._refresh_snapshot)
        header.addWidget(refresh_button)
        root_layout.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root_layout.addWidget(splitter, 1)

        self.jarvis_panel = JarvisPanel()
        self.jarvis_panel.setObjectName("jarvisPanel")
        self.jarvis_panel.message_submitted.connect(self._submit_message)
        splitter.addWidget(self.jarvis_panel)

        hardware_panel = QWidget()
        hardware_layout = QVBoxLayout(hardware_panel)
        hardware_layout.setContentsMargins(12, 0, 0, 0)
        hardware_layout.setSpacing(10)
        hardware_header = QHBoxLayout()
        hardware_title = QLabel("智能硬件")
        hardware_title.setObjectName("sectionTitle")
        hardware_header.addWidget(hardware_title)
        hardware_header.addStretch()
        hardware_header.addWidget(QLabel("已注册设备"))
        hardware_layout.addLayout(hardware_header)
        self.device_wall = DeviceCardWall()
        self.device_wall.device_selected.connect(self._select_device)
        hardware_layout.addWidget(self.device_wall, 1)
        splitter.addWidget(hardware_panel)
        splitter.setSizes([500, 620])

    def _connect_runtime(self) -> None:
        self.runtime.ensure_started()

    def _refresh_snapshot(self) -> None:
        self._run_background(self._fetch_snapshot, "snapshot")

    def _fetch_snapshot(self) -> tuple[list[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        devices = self.hub.list_devices()
        states: Dict[str, Dict[str, Any]] = {}
        for device in devices:
            if not is_real_device(device):
                continue
            device_id = str(device.get("device_id", ""))
            try:
                states[device_id] = self.hub.get_device_state(device_id)
            except AgentGatewayError:
                states[device_id] = {"device_id": device_id, "available": False}
        return devices, states

    def _submit_message(self, message: str) -> None:
        try:
            device_id = self._resolve_target_device()
        except AgentGatewayError as error:
            self.jarvis_panel.add_user_message(message)
            self.jarvis_panel.add_system_message(str(error))
            self.jarvis_panel.set_activity("error")
            return
        request_id = str(uuid.uuid4())
        self.jarvis_panel.add_user_message(message)
        self.jarvis_panel.add_pending_request(request_id)
        self.active_requests += 1
        self.jarvis_panel.set_activity("thinking")
        QTimer.singleShot(180, self._set_executing_if_busy)
        self._run_background(
            lambda: self._run_agent_request(request_id, device_id, message),
            "response",
        )

    def _run_agent_request(
        self,
        request_id: str,
        device_id: str,
        message: str,
    ) -> Dict[str, Any]:
        try:
            agent = TextAgent(
                self.hub,
                device_id=device_id,
                conversation_id=self.conversation_id,
            )
            response = agent.respond(message)
            return {
                "request_id": request_id,
                "response": response,
                "success": agent.response_status == "success",
                "status": agent.response_status,
            }
        except Exception as error:
            return {
                "request_id": request_id,
                "response": str(error),
                "success": False,
                "status": "failure",
            }

    def _resolve_target_device(self) -> str:
        if self.selected_device_id:
            return self.selected_device_id
        visible_devices = [device for device in self.devices if is_real_device(device)]
        online = [device for device in visible_devices if device.get("status") == "online"]
        if online:
            self.selected_device_id = str(online[0]["device_id"])
            return self.selected_device_id
        if visible_devices:
            self.selected_device_id = str(visible_devices[0]["device_id"])
            return self.selected_device_id
        raise AgentGatewayError("当前没有发现真实设备，请先启动中枢并连接设备。")

    def _run_background(self, action: Callable[[], Any], result_type: str) -> None:
        def worker() -> None:
            try:
                self.ui_queue.put((result_type, (True, action())))
            except Exception as error:
                self.ui_queue.put((result_type, (False, error)))

        self.executor.submit(worker)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                result_type, (success, result) = self.ui_queue.get_nowait()
                self._handle_result(result_type, success, result)
        except queue.Empty:
            pass

    def _handle_result(self, result_type: str, success: bool, result: Any) -> None:
        if result_type == "runtime":
            if success:
                self.connection_status.setText("中枢已连接")
                self._refresh_snapshot()
            else:
                self.connection_status.setText("中枢连接失败")
                self.jarvis_panel.add_system_message(str(result))
                self.jarvis_panel.set_activity("error")
            return
        if result_type == "snapshot":
            if success:
                devices, states = result
                self._update_devices(devices, states)
            else:
                self.connection_status.setText("设备状态刷新失败")
            return
        if result_type == "response":
            self._finish_agent_request(result)

    def _update_devices(
        self,
        devices: list[Dict[str, Any]],
        states: Dict[str, Dict[str, Any]],
    ) -> None:
        self.devices = devices
        online_count = sum(
            device.get("status") == "online"
            for device in devices
            if is_real_device(device)
        )
        self.connection_status.setText(f"中枢已连接 · {online_count} 台真实设备在线")
        self.device_wall.update_snapshot(devices, states)
        if self.selected_device_id not in self.device_wall.cards:
            self.selected_device_id = next(iter(self.device_wall.cards), None)
        if self.selected_device_id:
            self.device_wall.set_selected(self.selected_device_id)
            card = self.device_wall.cards.get(self.selected_device_id)
            if card is not None:
                self.jarvis_panel.set_target(
                    device_display_name(card.device),
                    self.selected_device_id,
                )

    def _select_device(self, device_id: str, device_name: str) -> None:
        self.selected_device_id = device_id
        self.device_wall.set_selected(device_id)
        self.jarvis_panel.set_target(device_name, device_id)

    def _finish_agent_request(self, result: Dict[str, Any]) -> None:
        request_id = result.get("request_id", "")
        success = bool(result.get("success"))
        status = str(result.get("status", "success" if success else "failure"))
        self.jarvis_panel.finish_request(
            request_id,
            str(result.get("response", "")),
            success,
            status,
        )
        self.active_requests = max(0, self.active_requests - 1)
        if status == "success":
            activity = "success"
        elif status == "queued":
            activity = "executing"
        else:
            activity = "error"
        self.jarvis_panel.set_activity(activity)
        self._refresh_snapshot()
        if self.active_requests == 0:
            QTimer.singleShot(1200, self._set_idle_if_quiet)

    def _set_executing_if_busy(self) -> None:
        if self.active_requests > 0:
            self.jarvis_panel.set_activity("executing")

    def _set_idle_if_quiet(self) -> None:
        if self.active_requests == 0:
            self.jarvis_panel.set_activity("idle")

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closed:
            self._closed = True
            self.queue_timer.stop()
            self.refresh_timer.stop()
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.runtime.stop_started_processes()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = UpperComputerApp()
    window.show()
    window.raise_()
    window.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
