from __future__ import annotations

import queue
import os
import mimetypes
import shutil
import socket
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
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

from agent_gateway import (
    AgentGatewayError,
    HubApiClient,
    MacOsTtsClient,
    RK3588AgentClient,
    RK3588DeviceRegistration,
    RK3588DeviceRegistrar,
)
from desktop_app.device_cards import DeviceCardWall, device_display_name, is_real_device
from desktop_app.jarvis_panel import JarvisPanel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "http://127.0.0.1:18787"
BRIDGE_HEALTH_URL = "http://127.0.0.1:19700/health"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
RK3588_BASE_URL = os.getenv("RK3588_BASE_URL", "http://192.168.0.140:8095")
RK3588_API_KEY = os.getenv("RK3588_API_KEY")
PUBLIC_BRIDGE_BASE_URL = os.getenv(
    "HUB_BRIDGE_PUBLIC_URL",
    "http://192.168.0.107:19700",
)
REGISTER_DEVICE_ID = os.getenv("HUB_REGISTER_DEVICE_ID", "bean_ye_1")
REGISTER_DEVICE_NAME = os.getenv("HUB_REGISTER_DEVICE_NAME", "录音豆")
REGISTER_DEVICE_CAPABILITIES = tuple(
    capability.strip()
    for capability in os.getenv(
        "HUB_REGISTER_DEVICE_CAPABILITIES",
        "record.start,record.stop,status.read",
    ).split(",")
    if capability.strip()
)
PORTABLE_DEVICE_ID = os.getenv("PORTABLE_DEVICE_ID", "portable_desktop_001")


class HubRuntime:
    def __init__(self, registrar: Optional[RK3588DeviceRegistrar] = None) -> None:
        self.broker_process: Optional[subprocess.Popen[Any]] = None
        self.hub_process: Optional[subprocess.Popen[Any]] = None
        self.bridge_process: Optional[subprocess.Popen[Any]] = None
        self.registrar = registrar

    def ensure_started(self) -> None:
        if not self._port_open(BROKER_HOST, BROKER_PORT):
            self._start_broker()
        if not self._health_ok():
            self._start_hub()
        if not self._bridge_health_ok() and self.hub_process is None:
            self._start_bridge_sidecar()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._health_ok() and self._bridge_health_ok():
                self._start_device_registrar()
                return
            time.sleep(0.25)
        raise RuntimeError("智能中枢或 HTTP-MQTT 兼容桥未能启动，请检查本地服务日志。")

    def stop_started_processes(self) -> None:
        if self.registrar is not None:
            self.registrar.stop()
        for process in (self.bridge_process, self.hub_process, self.broker_process):
            if process is None or process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def _start_device_registrar(self) -> None:
        if self.registrar is None:
            self.registrar = RK3588DeviceRegistrar(
                RK3588AgentClient(
                    RK3588_BASE_URL,
                    timeout_seconds=3.0,
                    api_key=RK3588_API_KEY,
                ),
                RK3588DeviceRegistration(
                    device_id=REGISTER_DEVICE_ID,
                    name=REGISTER_DEVICE_NAME,
                    base_url=PUBLIC_BRIDGE_BASE_URL,
                    capabilities=REGISTER_DEVICE_CAPABILITIES,
                ),
            )
        self.registrar.start()

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
                "--mqtt-client-id",
                "smart-hub-desktop",
                "--bridge-host",
                "0.0.0.0",
                "--bridge-port",
                "19700",
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _start_bridge_sidecar(self) -> None:
        database = PROJECT_ROOT / ".data" / "hub.db"
        self.bridge_process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "run.py"),
                "--host",
                "127.0.0.1",
                "--port",
                "18788",
                "--db",
                str(database),
                "--mqtt-host",
                "127.0.0.1",
                "--mqtt-port",
                "1883",
                "--mqtt-client-id",
                "smart-hub-desktop-bridge",
                "--bridge-host",
                "0.0.0.0",
                "--bridge-port",
                "19700",
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

    @staticmethod
    def _bridge_health_ok() -> bool:
        try:
            with urllib.request.urlopen(BRIDGE_HEALTH_URL, timeout=1) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False


class UpperComputerApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("外接大脑 · JARVIS 智能中枢")
        self.resize(1180, 780)
        self.setMinimumSize(980, 660)
        self.runtime = HubRuntime()
        self.hub = HubApiClient(HUB_URL)
        self.rk3588 = RK3588AgentClient(
            RK3588_BASE_URL,
            timeout_seconds=30.0,
            api_key=RK3588_API_KEY,
        )
        self.tts = MacOsTtsClient()
        self.audio_processes: Dict[int, subprocess.Popen[Any]] = {}
        self.speech_files: set[str] = set()
        self.ui_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.conversation_id = f"desktop-{uuid.uuid4()}"
        self.selected_device_id: Optional[str] = None
        self.devices: list[Dict[str, Any]] = []
        self.active_requests = 0
        self.pending_plans: Dict[str, Dict[str, Any]] = {}
        self.execution_to_plan: Dict[str, str] = {}
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
        self.jarvis_panel.audio_submitted.connect(self._submit_audio)
        self.jarvis_panel.speech_requested.connect(self._handle_speech_request)
        self.jarvis_panel.plan_confirmation_requested.connect(
            self._confirm_plan_request
        )
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
        device_id = self._get_target_device()
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

    def _submit_audio(self, audio_path: str) -> None:
        path = Path(audio_path)
        if not path.is_file():
            self.jarvis_panel.add_system_message("找不到所选音频文件。")
            return
        device_id = self._get_target_device()
        request_id = str(uuid.uuid4())
        self.jarvis_panel.add_user_message(f"语音消息：{path.name}")
        self.jarvis_panel.add_pending_request(request_id)
        self.active_requests += 1
        self.jarvis_panel.set_activity("thinking")
        self._run_background(
            lambda: self._run_portable_audio_request(
                request_id,
                device_id,
                path,
            ),
            "response",
        )

    def _handle_speech_request(self, bubble: Any) -> None:
        bubble_key = id(bubble)
        process = self.audio_processes.get(bubble_key)
        if process is not None and process.poll() is None:
            process.terminate()
            self.audio_processes.pop(bubble_key, None)
            bubble.set_speech_state("ready")
            return
        if bubble.audio_path:
            self._play_speech(bubble)
            return

        bubble.set_speech_state("generating")
        self._run_background(
            lambda: self._synthesize_speech(bubble),
            "tts",
        )

    def _synthesize_speech(self, bubble: Any) -> Dict[str, Any]:
        try:
            audio_path = self.tts.synthesize(bubble.message_label.text())
            self.speech_files.add(str(audio_path))
            return {"bubble": bubble, "audio_path": str(audio_path)}
        except Exception as error:
            return {"bubble": bubble, "error": str(error)}

    def _play_speech(self, bubble: Any) -> None:
        afplay = shutil.which("afplay") or "/usr/bin/afplay"
        audio_path = bubble.audio_path
        if not audio_path or not Path(afplay).is_file():
            bubble.set_speech_state("error")
            return
        try:
            process = subprocess.Popen(
                [afplay, audio_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            bubble.set_speech_state("error")
            return
        bubble_key = id(bubble)
        self.audio_processes[bubble_key] = process
        bubble.set_speech_state("playing")
        QTimer.singleShot(
            100,
            lambda: self._poll_speech(bubble, process),
        )

    def _poll_speech(self, bubble: Any, process: subprocess.Popen[Any]) -> None:
        if process.poll() is None:
            QTimer.singleShot(100, lambda: self._poll_speech(bubble, process))
            return
        self.audio_processes.pop(id(bubble), None)
        bubble.set_speech_state("ready")

    def _run_portable_audio_request(
        self,
        request_id: str,
        device_id: Optional[str],
        audio_path: Path,
    ) -> Dict[str, Any]:
        try:
            audio_format = audio_path.suffix.lower().lstrip(".") or "wav"
            content_type = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
            media = self.hub.upload_portable_media(
                portable_device_id=PORTABLE_DEVICE_ID,
                data=audio_path.read_bytes(),
                audio_format=audio_format,
                content_type=content_type,
            )
            self.hub.submit_portable_request(
                {
                    "portable_device_id": PORTABLE_DEVICE_ID,
                    "conversation_id": self.conversation_id,
                    "request_id": request_id,
                    "protocol_version": "1.0",
                    "source": "desktop_audio",
                    "input_type": "audio",
                    "media_id": media["media_id"],
                    "target_device_id": device_id,
                }
            )
            request = self._wait_for_portable_request(request_id)
            return self._portable_request_result(request_id, request)
        except Exception as error:
            return {
                "request_id": request_id,
                "response": self._friendly_error_message(error),
                "success": False,
                "status": self._error_status(error),
            }

    def _run_portable_dispatch_request(
        self,
        execution_request_id: str,
        portable_request_id: str,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            self.hub.confirm_portable_request(
                portable_request_id,
                str(plan_result["plan_id"]),
            )
            request = self._wait_for_portable_request(
                portable_request_id,
                include_execution_states=True,
            )
            result = self._portable_request_result(
                execution_request_id,
                request,
                include_plan=False,
            )
            return result
        except Exception as error:
            return {
                "request_id": execution_request_id,
                "response": self._friendly_error_message(error),
                "success": False,
                "status": self._error_status(error),
            }

    def _wait_for_portable_request(
        self,
        request_id: str,
        include_execution_states: bool = False,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + 120
        terminal_states = {"completed", "planned", "failed", "timed_out"}
        if include_execution_states:
            terminal_states.update({"succeeded", "queued", "executing"})
        while time.monotonic() < deadline:
            request = self.hub.get_portable_request(request_id)
            if str(request.get("state") or "") in terminal_states:
                return request
            time.sleep(0.5)
        raise TimeoutError("随身设备音频请求处理超时")

    def _portable_request_result(
        self,
        request_id: str,
        request: Dict[str, Any],
        include_plan: bool = True,
    ) -> Dict[str, Any]:
        state = str(request.get("state") or "")
        response = str(request.get("response_text") or "")
        if state == "planned" and include_plan:
            intent = request.get("intent") or {}
            command = {
                "device_id": intent.get("device_id"),
                "capability": intent.get("capability"),
                "params": intent.get("params") or {},
            }
            return {
                "request_id": request_id,
                "response": response or "计划已生成，请确认是否执行。",
                "success": False,
                "status": "planned",
                "plan_id": request.get("plan_id"),
                "plan": {"command": command},
                "device_id": command["device_id"],
                "requires_confirmation": True,
                "transport": "portable",
                "portable_request_id": request.get("request_id"),
            }
        if state == "completed":
            return {
                "request_id": request_id,
                "response": response or "已处理完成。",
                "success": True,
                "status": "chat",
            }
        if state == "succeeded":
            return {
                "request_id": request_id,
                "response": response or "设备执行完成。",
                "success": True,
                "status": "success",
            }
        if state in {"queued", "executing"}:
            return {
                "request_id": request_id,
                "response": response or "命令已进入设备队列。",
                "success": False,
                "status": "queued",
            }
        if state == "timed_out" or request.get("error_code") == "COMMAND_TIMEOUT":
            return {
                "request_id": request_id,
                "response": response or "设备执行超时。",
                "success": False,
                "status": "timeout",
            }
        return {
            "request_id": request_id,
            "response": response or "音频请求处理失败。",
            "success": False,
            "status": "failure",
        }

    def _run_agent_request(
        self,
        request_id: str,
        device_id: Optional[str],
        message: str,
    ) -> Dict[str, Any]:
        try:
            response = self.rk3588.route_message(
                request_id=request_id,
                conversation_id=self.conversation_id,
                input_text=message,
                target_device_id=device_id,
                source="desktop",
            )

            route = str(response.get("route") or "").strip().lower()
            if route in {"chat", "clarification", "assistant"}:
                return {
                    "request_id": request_id,
                    "response": self._response_message(
                        response,
                        "我暂时没有可用的回复。",
                    ),
                    "success": True,
                    "status": "chat",
                }

            plan_id = response.get("plan_id")
            is_plan = response.get("status") == "planned" or route in {
                "command",
                "hardware",
                "tool",
            }
            if is_plan and isinstance(plan_id, str):
                return {
                    "request_id": request_id,
                    "response": self._response_message(
                        response,
                        "计划已生成，请确认是否执行。",
                    ),
                    "success": False,
                    "status": "planned",
                    "plan_id": plan_id,
                    "plan": response,
                    "device_id": device_id,
                    "requires_confirmation": response.get(
                        "requires_confirmation", True
                    ),
                }

            if is_plan:
                return {
                    "request_id": request_id,
                    "response": self._response_message(
                        response,
                        "RK3588 没有返回有效的结构化计划。",
                    ),
                    "success": False,
                    "status": "failure",
                }

            if isinstance(response.get("reply"), str) and response["reply"].strip():
                return {
                    "request_id": request_id,
                    "response": self._response_message(
                        response,
                        "我暂时没有可用的回复。",
                    ),
                    "success": True,
                    "status": "chat",
                }

            return {
                "request_id": request_id,
                "response": "自动路由没有返回聊天回复或结构化计划。",
                "success": False,
                "status": "failure",
            }
        except Exception as error:
            return {
                "request_id": request_id,
                "response": self._friendly_error_message(error),
                "success": False,
                "status": self._error_status(error),
            }

    def _confirm_plan_request(self, request_id: str) -> None:
        plan_result = self.pending_plans.get(request_id)
        if plan_result is None:
            return
        if plan_result.get("transport") == "portable":
            self._dispatch_portable_plan_request(
                request_id,
                plan_result,
                confirmation_message="已确认，正在执行计划…",
                increment_active=True,
            )
            return
        self._dispatch_plan_request(
            request_id,
            plan_result,
            confirmation_message="已确认，正在执行计划…",
            increment_active=True,
        )

    def _dispatch_plan_request(
        self,
        request_id: str,
        plan_result: Dict[str, Any],
        confirmation_message: str,
        increment_active: bool,
    ) -> None:
        plan_id = str(plan_result.get("plan_id", ""))
        if not plan_id:
            self.jarvis_panel.add_system_message("计划缺少 plan_id，不能执行。")
            return

        execution_request_id = str(uuid.uuid4())
        self.execution_to_plan[execution_request_id] = request_id
        self.jarvis_panel.confirm_request(request_id, confirmation_message)
        if increment_active:
            self.active_requests += 1
        self.jarvis_panel.set_activity("executing")
        self._run_background(
            lambda: self._run_dispatch_request(
                execution_request_id,
                plan_result,
            ),
            "response",
        )

    def _dispatch_portable_plan_request(
        self,
        request_id: str,
        plan_result: Dict[str, Any],
        confirmation_message: str,
        increment_active: bool,
    ) -> None:
        plan_id = str(plan_result.get("plan_id", ""))
        portable_request_id = str(
            plan_result.get("portable_request_id") or request_id
        )
        if not plan_id:
            self.jarvis_panel.add_system_message("计划缺少 plan_id，不能执行。")
            return

        execution_request_id = str(uuid.uuid4())
        self.execution_to_plan[execution_request_id] = request_id
        self.jarvis_panel.confirm_request(request_id, confirmation_message)
        if increment_active:
            self.active_requests += 1
        self.jarvis_panel.set_activity("executing")
        self._run_background(
            lambda: self._run_portable_dispatch_request(
                execution_request_id,
                portable_request_id,
                plan_result,
            ),
            "response",
        )

    def _run_dispatch_request(
        self,
        request_id: str,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            plan = plan_result.get("plan") or {}
            command = plan.get("command") if isinstance(plan, dict) else {}
            command = command if isinstance(command, dict) else {}
            device_id = str(
                command.get("device_id")
                or plan_result.get("device_id")
                or ""
            )
            response = self.rk3588.dispatch_hub_command(
                request_id=request_id,
                conversation_id=self.conversation_id,
                plan_id=str(plan_result["plan_id"]),
                target_device_id=device_id,
                source="desktop",
            )
            state = str(response.get("state") or response.get("status") or "")
            success = bool(response.get("ok")) or state == "succeeded"
            status = self._execution_status(state, success, response)
            return {
                "request_id": request_id,
                "response": self._response_message(response, "执行结果已返回。"),
                "success": success,
                "status": status,
            }
        except Exception as error:
            return {
                "request_id": request_id,
                "response": self._friendly_error_message(error),
                "success": False,
                "status": self._error_status(error),
            }

    @staticmethod
    def _response_message(response: Dict[str, Any], fallback: str) -> str:
        value = response.get("reply")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    @staticmethod
    def _friendly_error_message(error: Exception) -> str:
        detail = str(error).lower()
        if "504" in detail or "gateway timeout" in detail:
            return "中枢响应超时，请稍后重试。"
        if "timeout" in detail or "timed out" in detail or "超时" in detail:
            return "中枢处理时间较长，请稍后重试。"
        if "无法连接" in detail or "connection" in detail:
            return "暂时无法连接中枢，请检查 RK3588 是否在线。"
        return "中枢暂时无法处理这条消息，请稍后重试。"

    @classmethod
    def _error_status(cls, error: Exception) -> str:
        detail = str(error).lower()
        if "504" in detail or "gateway timeout" in detail:
            return "timeout"
        if "timeout" in detail or "timed out" in detail or "超时" in detail:
            return "timeout"
        return "failure"

    @staticmethod
    def _execution_status(
        state: str,
        success: bool,
        response: Dict[str, Any],
    ) -> str:
        if success:
            return "success"
        if response.get("timed_out") or state in {"timeout", "expired"}:
            return "timeout"
        if state in {"queued", "accepted", "running", "pending"}:
            return "queued"
        return "failure"

    def _get_target_device(self) -> Optional[str]:
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
        return None

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
            return
        if result_type == "tts":
            bubble = result.get("bubble") if isinstance(result, dict) else None
            audio_path = result.get("audio_path") if isinstance(result, dict) else None
            if success and bubble is not None and audio_path:
                bubble.set_audio_path(audio_path)
                self._play_speech(bubble)
            elif bubble is not None:
                bubble.set_speech_state("error")
                self.jarvis_panel.add_system_message(
                    f"语音生成失败：{result.get('error') or '请稍后重试。'}"
                )

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
        if status == "planned":
            self.pending_plans[request_id] = result
            requires_confirmation = bool(result.get("requires_confirmation", True))
            if requires_confirmation:
                self.jarvis_panel.finish_plan(
                    request_id,
                    str(result.get("response", "计划已生成，请确认是否执行。")),
                )
                self.active_requests = max(0, self.active_requests - 1)
                if self.active_requests == 0:
                    self.jarvis_panel.set_activity("idle")
            else:
                if result.get("transport") == "portable":
                    self._dispatch_portable_plan_request(
                        request_id,
                        result,
                        confirmation_message="已识别，正在查询设备状态…",
                        increment_active=False,
                    )
                else:
                    self._dispatch_plan_request(
                        request_id,
                        result,
                        confirmation_message="已识别，正在查询设备状态…",
                        increment_active=False,
                    )
            return

        display_request_id = self.execution_to_plan.pop(request_id, request_id)
        self.pending_plans.pop(display_request_id, None)
        self.jarvis_panel.finish_request(
            display_request_id,
            str(result.get("response", "")),
            success,
            status,
        )
        self.active_requests = max(0, self.active_requests - 1)
        if status in {"success", "chat"}:
            activity = status
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
            for process in self.audio_processes.values():
                if process.poll() is None:
                    process.terminate()
            self.audio_processes.clear()
            for audio_path in self.speech_files:
                self.tts.cleanup(audio_path)
            self.speech_files.clear()
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
