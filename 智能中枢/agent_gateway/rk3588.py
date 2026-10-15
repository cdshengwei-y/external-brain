from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class RK3588AgentError(RuntimeError):
    def __init__(self, message: str, *, is_timeout: bool = False) -> None:
        super().__init__(message)
        self.is_timeout = is_timeout


class RK3588AgentClient:
    """Client for the local RK3588 AI service discovered on the test hub."""

    PROTOCOL_VERSION = "1.0"
    ROUTE_REQUEST_TIMEOUT = 30.0
    ROUTE_RETRY_COUNT = 1

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

    def route_message(
        self,
        request_id: str,
        conversation_id: str,
        input_text: str,
        target_device_id: Optional[str] = None,
        source: str = "desktop",
        audio_data_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "protocol_version": self.PROTOCOL_VERSION,
            "request_id": request_id,
            "conversation_id": conversation_id,
            "source": source,
            "task": "auto",
            "input": input_text,
        }
        if target_device_id is not None:
            payload["target_device_id"] = target_device_id
        if audio_data_url is not None:
            payload["audio"] = audio_data_url
        return self._route_request(payload)

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
        conversation_id: str,
        input_text: Optional[str] = None,
        audio_data_url: Optional[str] = None,
        target_device_id: Optional[str] = None,
        source: str = "desktop",
    ) -> Dict[str, Any]:
        return self._hub_command(
            request_id=request_id,
            conversation_id=conversation_id,
            input_text=input_text,
            audio_data_url=audio_data_url,
            target_device_id=target_device_id,
            source=source,
            dispatch=False,
        )

    def dispatch_hub_command(
        self,
        request_id: str,
        conversation_id: str,
        plan_id: str,
        target_device_id: str,
        source: str = "desktop",
        input_text: Optional[str] = None,
        audio_data_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._hub_command(
            request_id=request_id,
            conversation_id=conversation_id,
            plan_id=plan_id,
            input_text=input_text,
            audio_data_url=audio_data_url,
            target_device_id=target_device_id,
            source=source,
            dispatch=True,
        )

    def confirm_hub_command(
        self,
        request_id: str,
        plan_id: str,
        conversation_id: str,
        source: str = "desktop",
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/hub/v1/commands/{request_id}/confirm",
            {
                "protocol_version": self.PROTOCOL_VERSION,
                "request_id": request_id,
                "conversation_id": conversation_id,
                "plan_id": plan_id,
                "source": source,
            },
        )

    def _hub_command(
        self,
        request_id: str,
        conversation_id: str,
        input_text: Optional[str],
        audio_data_url: Optional[str],
        target_device_id: Optional[str],
        source: str,
        dispatch: bool,
        plan_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "protocol_version": self.PROTOCOL_VERSION,
            "request_id": request_id,
            "conversation_id": conversation_id,
            "source": source,
            "dispatch": dispatch,
        }
        if input_text is not None:
            payload["input"] = input_text
        if audio_data_url is not None:
            payload["audio"] = audio_data_url
        if target_device_id is not None:
            payload["target_device_id"] = target_device_id
        if plan_id is not None:
            payload["plan_id"] = plan_id
        return self._request("POST", "/api/hub/v1/commands", payload)

    def _route_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a route request and retry only timeouts with the same request ID."""
        timeout = max(self.ROUTE_REQUEST_TIMEOUT, self.timeout_seconds)
        for attempt in range(self.ROUTE_RETRY_COUNT + 1):
            try:
                return self._request(
                    "POST",
                    "/api/route",
                    payload,
                    timeout_seconds=timeout,
                )
            except RK3588AgentError as error:
                if not error.is_timeout or attempt >= self.ROUTE_RETRY_COUNT:
                    raise
        raise AssertionError("route retry loop exited unexpectedly")

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
        timeout_seconds: Optional[float] = None,
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
            timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = {"error": error.reason}
            raise RK3588AgentError(
                detail.get("error", str(error)),
                is_timeout=error.code == 504,
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            is_timeout = isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)
            if not is_timeout:
                is_timeout = "timed out" in str(reason).lower() or "timeout" in str(reason).lower()
            raise RK3588AgentError(
                f"无法连接 RK3588 本地 AI 服务: {reason}",
                is_timeout=is_timeout,
            ) from error
        if not isinstance(result, dict):
            raise RK3588AgentError("RK3588 本地 AI 返回的数据格式不正确")
        return result

    @staticmethod
    def reply_text(response: Dict[str, Any]) -> str:
        """Return only the user-facing reply and hide routing diagnostics."""
        reply = response.get("reply")
        if isinstance(reply, str) and reply.strip():
            return reply.strip()
        return "暂时没有收到可播放的回答。"

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
