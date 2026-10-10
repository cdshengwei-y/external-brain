from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class RK3588AgentError(RuntimeError):
    pass


class RK3588AgentClient:
    """Client for the local RK3588 AI service discovered on the test hub."""

    def __init__(
        self,
        base_url: str = "http://192.168.0.140:8095",
        timeout_seconds: float = 120.0,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/health")

    def get_mode(self) -> Dict[str, Any]:
        return self._request("GET", "/api/mode")

    def set_mode(self, mode: str) -> Dict[str, Any]:
        return self._request("POST", "/api/mode", {"mode": mode})

    def run_agent(
        self,
        input_text: str = "",
        audio_data_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"task": "auto", "input": input_text}
        if audio_data_url is not None:
            payload["audio"] = audio_data_url
        return self._request("POST", "/api/agent", payload)

    def transcribe(self, audio_data_url: str) -> Dict[str, Any]:
        return self._request("POST", "/api/asr", {"audio": audio_data_url})

    def synthesize(self, text: str) -> Dict[str, Any]:
        return self._request("POST", "/api/tts", {"input": text})

    def hub_health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/hub/v1/health")

    def list_hub_devices(self) -> Dict[str, Any]:
        return self._request("GET", "/api/hub/v1/devices")

    def register_hub_device(
        self,
        device_id: str,
        name: str,
        base_url: str,
        capabilities: List[str],
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/hub/v1/devices",
            {
                "device_id": device_id,
                "name": name,
                "base_url": base_url,
                "capabilities": capabilities,
            },
        )

    def plan_hub_command(
        self,
        request_id: str,
        input_text: Optional[str] = None,
        audio_data_url: Optional[str] = None,
        target_device: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._hub_command(
            request_id=request_id,
            input_text=input_text,
            audio_data_url=audio_data_url,
            target_device=target_device,
            dispatch=False,
        )

    def dispatch_hub_command(
        self,
        request_id: str,
        input_text: Optional[str] = None,
        audio_data_url: Optional[str] = None,
        target_device: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._hub_command(
            request_id=request_id,
            input_text=input_text,
            audio_data_url=audio_data_url,
            target_device=target_device,
            dispatch=True,
        )

    def confirm_hub_command(self, request_id: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/hub/v1/commands/{request_id}/confirm",
        )

    def _hub_command(
        self,
        request_id: str,
        input_text: Optional[str],
        audio_data_url: Optional[str],
        target_device: Optional[str],
        dispatch: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "request_id": request_id,
            "dispatch": dispatch,
        }
        if input_text is not None:
            payload["input"] = input_text
        if audio_data_url is not None:
            payload["audio"] = audio_data_url
        if target_device is not None:
            payload["target_device"] = target_device
        return self._request("POST", "/api/hub/v1/commands", payload)

    @staticmethod
    def extract_actions(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract only explicitly structured hardware actions from an agent reply."""
        candidates: Any = response.get("actions") or response.get("commands")
        if candidates is None and isinstance(response.get("plan"), dict):
            candidates = response["plan"].get("actions") or response["plan"].get(
                "commands"
            )
        if candidates is None:
            candidates = RK3588AgentClient._extract_json_actions(
                response.get("content")
            )
        if not isinstance(candidates, list):
            return []

        actions: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            capability = candidate.get("capability")
            device_id = candidate.get("device_id")
            params = candidate.get("params") or {}
            if (
                isinstance(capability, str)
                and capability.strip()
                and isinstance(device_id, str)
                and device_id.strip()
                and isinstance(params, dict)
            ):
                actions.append(
                    {
                        "capability": capability.strip(),
                        "device_id": device_id.strip(),
                        "params": params,
                    }
                )
        return actions

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Hub-Key"] = self.api_key
        if extra_headers:
            headers.update(extra_headers)
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
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = {"error": error.reason}
            raise RK3588AgentError(detail.get("error", str(error))) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RK3588AgentError(f"无法连接 RK3588 本地 AI 服务: {error}") from error
        if not isinstance(result, dict):
            raise RK3588AgentError("RK3588 本地 AI 返回的数据格式不正确")
        return result

    @staticmethod
    def _extract_json_actions(content: Any) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(content, str) or not content.strip():
            return None
        candidates = [content.strip()]
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.S | re.I)
        if fenced:
            candidates.insert(0, fenced.group(1).strip())
        object_match = re.search(r"\{.*\}", content, re.S)
        if object_match:
            candidates.append(object_match.group(0))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                actions = parsed.get("actions") or parsed.get("commands")
                if isinstance(actions, list):
                    return actions
        return None
