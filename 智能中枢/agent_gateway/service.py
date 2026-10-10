from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, Optional


class AgentGatewayError(RuntimeError):
    pass


class HubApiClient:
    """HTTP client used by the text agent to call the local hub."""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def list_devices(self) -> list:
        return self._request("GET", "/api/v1/devices")["devices"]

    def get_device_state(self, device_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/devices/{device_id}/state")

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/health")

    def submit_command(
        self,
        device_id: str,
        capability: str,
        params: Optional[Dict[str, Any]],
        conversation_id: str,
        policy: str,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/devices/{device_id}/commands",
            {
                "conversation_id": conversation_id,
                "capability": capability,
                "params": params or {},
                "policy": policy,
            },
        )

    def wait_for_command(
        self,
        command_id: str,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/commands/{command_id}/wait?timeout_ms={timeout_ms}",
            timeout_seconds=max(self.timeout_seconds, timeout_ms / 1000 + 1),
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds or self.timeout_seconds,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = {"error": error.reason}
            raise AgentGatewayError(detail.get("error", str(error))) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise AgentGatewayError(f"无法连接智能中枢: {error}") from error
        if not isinstance(result, dict):
            raise AgentGatewayError("中枢返回的数据格式不正确")
        return result


class TextAgent:
    """第一阶段的本地文字智能体，后续可替换为大模型意图解析器。"""

    DEVICE_ID_PATTERN = re.compile(r"bean_ye_[0-9]+")

    def __init__(
        self,
        hub: HubApiClient,
        device_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        on_command_submitted: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.hub = hub
        self.device_id = device_id
        self.conversation_id = conversation_id or f"text-console-{uuid.uuid4()}"
        self.response_status = "success"
        self.command_id: Optional[str] = None
        self.command_state: Optional[str] = None
        self.on_command_submitted = on_command_submitted

    def respond(self, text: str) -> str:
        self.response_status = "success"
        self.command_id = None
        self.command_state = None
        message = text.strip()
        if not message:
            return "请告诉我想做什么，例如：开始录音、停止录音、查询状态。"
        if self._is_list_devices_request(message):
            return self._format_devices(self.hub.list_devices())

        capability = self._match_capability(message)
        if capability is None:
            return (
                "我现在支持：开始录音、停止录音、查询状态、查看设备。"
                "其他需求先不要执行。"
            )

        device_id = self._resolve_device_id(message)
        command = self.hub.submit_command(
            device_id=device_id,
            capability=capability,
            params={},
            conversation_id=self.conversation_id,
            policy="queue",
        )
        command_id = command["command_id"]
        self.command_id = command_id
        if self.on_command_submitted is not None:
            self.on_command_submitted(command)
        result = self.hub.wait_for_command(command_id)
        command_result = result.get("command", {})
        self.command_state = command_result.get("state")
        self.response_status = self._get_response_status(result)
        response_text = self._format_command_result(capability, result)
        if capability == "status.read" and self.response_status == "success":
            response_text = self._format_status_summary(device_id, response_text)
        return response_text

    def _resolve_device_id(self, message: str) -> str:
        explicit_id = self.DEVICE_ID_PATTERN.search(message)
        if explicit_id:
            return explicit_id.group(0)
        if self.device_id:
            return self.device_id
        devices = self.hub.list_devices()
        online = [device for device in devices if device.get("status") == "online"]
        if online:
            return online[0]["device_id"]
        if devices:
            return devices[0]["device_id"]
        raise AgentGatewayError("当前没有发现真实设备，请先启动中枢并连接设备。")

    @staticmethod
    def _match_capability(message: str) -> Optional[str]:
        if any(keyword in message for keyword in ("开始录音", "启动录音", "录音开始")):
            return "record.start"
        if any(keyword in message for keyword in ("停止录音", "结束录音", "录音停止")):
            return "record.stop"
        if any(keyword in message for keyword in ("查询状态", "查看状态", "设备状态", "现在状态")):
            return "status.read"
        return None

    @staticmethod
    def _is_list_devices_request(message: str) -> bool:
        return any(keyword in message for keyword in ("查看设备", "有哪些设备", "设备列表"))

    @staticmethod
    def _format_devices(devices: list) -> str:
        if not devices:
            return "当前没有发现设备。"
        lines = ["当前设备："]
        for device in devices:
            status = device.get("status", "unknown")
            capabilities = ", ".join(device.get("capabilities", [])) or "无"
            lines.append(
                f"- {device.get('device_id')}: {status}，能力：{capabilities}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_command_result(capability: str, response: Dict[str, Any]) -> str:
        command = response.get("command", {})
        state = command.get("state")
        if not response.get("completed"):
            if state == "queued":
                return f"命令已进入队列，目前状态是 {state}，请稍后再问我。"
            return f"命令等待超时，目前状态是 {state}，请稍后再问我。"
        if state == "succeeded":
            messages = {
                "record.start": "已经开始录音。",
                "record.stop": "已经停止录音。",
                "status.read": "设备状态查询完成。",
            }
            return messages.get(capability, "命令已经执行完成。")
        if state == "cancelled":
            return "命令已取消。"
        if state == "superseded":
            return "命令已被更新的命令替代。"
        return f"命令执行未成功，最终状态是 {state}。"

    @staticmethod
    def _get_response_status(response: Dict[str, Any]) -> str:
        command = response.get("command", {})
        state = command.get("state")
        if not response.get("completed"):
            return "queued" if state == "queued" else "timeout"
        return "success" if state == "succeeded" else "failure"

    def _format_status_summary(self, device_id: str, fallback: str) -> str:
        try:
            state = self.hub.get_device_state(device_id)
        except AgentGatewayError:
            return fallback
        if not state.get("available"):
            return fallback + "\n暂时没有获取到设备状态详情。"

        lines = ["设备状态查询完成："]
        if state.get("recording") is not None:
            lines.append(f"录音：{'录音中' if state.get('recording') else '未录音'}")
        if state.get("sd_free_mb") is not None and state.get("sd_total_mb") is not None:
            lines.append(
                f"TF 卡：{state['sd_free_mb']} / {state['sd_total_mb']} MB 可用"
            )
        if state.get("wifi_connected") is not None:
            if state.get("wifi_connected"):
                wifi = f"Wi-Fi：已连接 {state.get('wifi_ip') or ''}".strip()
                if state.get("wifi_rssi_dbm") is not None:
                    wifi += f"（{state['wifi_rssi_dbm']} dBm）"
                lines.append(wifi)
            else:
                lines.append("Wi-Fi：未连接")
        if state.get("mqtt_connected") is not None:
            lines.append(f"MQTT：{'已连接' if state.get('mqtt_connected') else '未连接'}")
        if state.get("current_file") is not None:
            lines.append(f"当前文件：{state.get('current_file') or '无'}")
        if state.get("battery_percent") is not None and state.get("battery_percent") >= 0:
            lines.append(f"电量：{state['battery_percent']}%")
        return "\n".join(lines) if len(lines) > 1 else fallback
