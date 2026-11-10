"""统一外设动作分发器：外设插件只声明意图，执行统一走这里。

架构约定（2026-11-03 与用户对齐）：Bot 平台负责「理解需求」判出 intent，
真正的硬件动作由中枢收口执行 —— 所有外设插件（录音豆、未来的其他设备）
都把命令交给 :class:`ActionDispatcher`，由它负责：

- 拼 AgentOS 命令报文（MQTT 直连 broker，与录音豆固件协议一致，
  见 ``交接文档_录音豆-AgentOS-MQTT.md``）；
- 生成幂等 request_id（tenant_id + request_id 同键去重，防重说被
  DUPLICATE_REQUEST 卡住）；
- 把服务端/设备侧错误码翻译成用户听得懂的一句话，绝不裸抛异常栈；
- 写操作（record.start/stop）失败不自动重试 —— 换 key 重发有重复开始
  录音的风险，宁可让用户再说一遍；只读查询（status.read）网络抖动重试一次。

为什么直连 MQTT 而不走 RK3588 的 8099 HTTP 命令 API（2026-11-03 用户拍板）：
录音豆固件只认 MQTT 命令报文，8099 只是盒子上多包了一层的中转。中枢自己
连 1883 的 broker 直接发命令，少一跳 HTTP，盒子退化成纯「信箱」。MCP 工具
循环也不考虑——它只在 ``--agent`` 模式存在，桌宠语音链路跑在 ``--swbot``
模式下，两者互斥。
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Union


@dataclass
class ActionSpec:
    """一个意图对应的一条外设命令声明。插件在 ``actions`` 表里给出。"""

    capability: str
    # 空 = 用分发器的默认设备（单设备场景够用，多设备时插件自己填）
    device_id: str = ""
    # 成功话术：固定文案，或接收设备 ACK result 返回一句话的函数
    ok_reply: Union[str, Callable[[Dict[str, Any]], str]] = ""
    # 只读命令网络失败允许重试一次；写命令绝不重试（副作用）
    read_only: bool = False
    # 是否把 Bot 给的 slots 透传成命令 params。默认关闭：多数设备协议要求
    # params 为空（录音豆就是），Bot 幻觉出的多余字段会换来 INVALID_PARAMS。
    forward_slots: bool = False


@dataclass
class ActionOutcome:
    """分发器执行结果的统一出口，ok=False 时 user_reply 一定有话可说。"""

    ok: bool
    status: str = ""
    error_code: str = ""
    # 给日志看的细节，不念给用户
    detail: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    user_reply: str = ""


class AgentOSCommandError(RuntimeError):
    """命令 API 调用失败。kind: failed=业务拒绝 / auth=鉴权 / network=连不上"""

    def __init__(self, message: str, *, error_code: str = "", kind: str = "failed"):
        super().__init__(message)
        self.error_code = error_code
        self.kind = kind


class AgentOSCommandClient:
    """RK3588 命令 API 直连客户端（无第三方依赖，纯 urllib）。

    请求体格式照抄 ``config/rk3588_routes.json`` 里已回填确认的模板：
    tenant 走请求体 ``tenant_id`` 字段（对方明确说不是 HTTP 头），
    鉴权走 ``Authorization: Bearer``。同步返回终态，无轮询接口。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        tenant_id: str = "tenant-a",
        actor_ref: str = "hardware-agent",
        conversation_id: str = "pet-voice",
        timeout_seconds: float = 20.0,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.tenant_id = tenant_id
        self.actor_ref = actor_ref
        self.conversation_id = conversation_id
        self.timeout_seconds = float(timeout_seconds or 20.0)

    def execute_command(
        self,
        capability: str,
        device_id: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> Dict[str, Any]:
        """发一条命令，返回服务端 JSON（status=succeeded/failed）。"""
        if not self.base_url or not self.api_key:
            raise AgentOSCommandError("命令 API 未配置", kind="auth")
        # 幂等键 = tenant_id + request_id；同一轮请求里同一意图重放会拿到
        # 首次结果而不是重复执行，这正是想要的行为。
        body = {
            "protocol_version": "1.0",
            "request_id": request_id or uuid.uuid4().hex,
            "conversation_id": self.conversation_id,
            "plan_id": "",
            "tenant_id": self.tenant_id,
            "actor_ref": self.actor_ref,
            "user_request": f"语音指令 {capability}",
            "command": {
                "device_id": device_id,
                "capability": capability,
                "params": dict(params or {}),
            },
        }
        url = f"{self.base_url}/api/agentos/v1/commands"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self._request(url, "POST", headers, data)

    def _request(
        self,
        url: str,
        method: str,
        headers: Dict[str, str],
        data: Optional[bytes],
    ) -> Dict[str, Any]:
        """真正的一次 HTTP。独立成方法方便单测 monkeypatch，不联网。"""
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # 内网直连必须绕过系统代理：这台机器配过代理，会把 192.168 劫走
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            status = error.code
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AgentOSCommandError(f"连不上 {url}: {error}", kind="network") from error

        if status in (401, 403):
            raise AgentOSCommandError(
                f"API Key 无效（HTTP {status}）", error_code="UNAUTHORIZED", kind="auth"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if status != 200:
            raise AgentOSCommandError(
                f"HTTP {status}: {raw[:200]}",
                error_code=str(payload.get("error_code") or ""),
                kind="failed",
            )
        if payload.get("status") == "failed":
            raise AgentOSCommandError(
                str(payload.get("message") or "服务端拒绝"),
                error_code=str(payload.get("error_code") or ""),
                kind="failed",
            )
        return payload


class AgentMqttClient:
    """AgentOS 命令的 MQTT 直连客户端（录音豆固件协议）。

    链路：发布 ``agentos/devices/{device_id}/commands``（QoS 1），
    等设备在 ``agentos/devices/{device_id}/acks`` 回带相同 ``command_id``
    的 ACK，同步返回。对上层的接口与 :class:`AgentOSCommandClient`
    完全一致（返回 ACK dict / 抛 AgentOSCommandError），
    所以 ActionDispatcher 和插件层不用动。

    协议要点（见交接文档）：

    - 命令 JSON 只允许 5 个字段，``params`` 必须是 ``{}``（分发器已拦）；
    - 设备幂等键是 ``command_id``（≤64 字符，仅字母数字-``_``），
      HTTP 版的 ``request_id`` 不能直接透传，这里做清洗；
    - ACK ``status`` 只有 ``succeeded`` / ``failed`` 两种。
    """

    # 允许外层注入假 client 做单测；默认返回真正的 paho 客户端。
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 10.0,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.host = str(host or "").strip()
        self.port = int(port or 1883)
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.timeout_seconds = float(timeout_seconds or 10.0)
        self._client_factory = client_factory or self._default_paho_client
        self._client: Any = None
        self._pending: Dict[str, Future] = {}
        self._connected = threading.Event()
        self._connect_error: Optional[AgentOSCommandError] = None
        self._lock = threading.Lock()

    @staticmethod
    def _default_paho_client() -> Any:
        import paho.mqtt.client as mqtt

        # 回调 API 用 VERSION2（paho 2.x 默认），reason_code 是枚举但可比整数
        return mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"hub-{uuid.uuid4().hex[:12]}",
        )

    @staticmethod
    def sanitize_command_id(request_id: str) -> str:
        """清洗成设备认可的 command_id：≤64 字符、[A-Za-z0-9_-]。"""
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", str(request_id or ""))[:64]
        return cleaned or uuid.uuid4().hex

    # -- 连接管理 ---------------------------------------------------------

    def _ensure_connected(self) -> None:
        # 缓存的连接失败（鉴权被拒等）优先于一切：即使事件已置位也要抛，
        # 否则被拒后 publish 会继续走然后莫名超时。
        if self._connect_error is not None:
            raise self._connect_error
        if self._connected.is_set():
            return
        with self._lock:
            if self._connect_error is not None:
                raise self._connect_error
            if self._connected.is_set():
                return
            try:
                client = self._client_factory()
                client.on_connect = self._on_connect
                client.on_message = self._on_message
                client.username_pw_set(self.username, self.password)
                client.connect(self.host, self.port, keepalive=30)
                client.loop_start()
                self._client = client
            except (OSError, ValueError) as error:
                self._connect_error = AgentOSCommandError(
                    f"连不上 MQTT broker {self.host}:{self.port}：{error}",
                    kind="network",
                )
                raise self._connect_error from error
        if not self._connected.wait(timeout=self.timeout_seconds):
            if self._connect_error is not None:
                raise self._connect_error
            self._connect_error = AgentOSCommandError(
                f"MQTT broker {self.host}:{self.port} 没在 {self.timeout_seconds:.0f}s 内回应连接",
                kind="network",
            )
            raise self._connect_error
        if self._connect_error is not None:
            raise self._connect_error

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if int(reason_code) == 0:
            self._connect_error = None
            client.subscribe("agentos/+/acks", qos=1)
            self._connected.set()
        else:
            self._connect_error = AgentOSCommandError(
                f"MQTT broker 拒绝连接（reason={reason_code}），检查用户名密码",
                error_code="UNAUTHORIZED",
                kind="auth",
            )
            self._connected.set()  # 放行等待方，由它读 _connect_error 抛出

    def _on_message(self, client, userdata, message):
        try:
            ack = json.loads(message.payload.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(ack, dict):
            return
        command_id = str(ack.get("command_id") or "")
        future = self._pending.pop(command_id, None)
        if future is not None and not future.done():
            future.set_result(ack)

    # -- 命令执行 ---------------------------------------------------------

    def execute_command(
        self,
        capability: str,
        device_id: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> Dict[str, Any]:
        if not self.host:
            raise AgentOSCommandError("MQTT broker 未配置", kind="auth")
        command_id = self.sanitize_command_id(request_id)
        self._ensure_connected()
        assert self._client is not None
        topic = f"agentos/devices/{device_id}/commands"
        payload = json.dumps(
            {
                "protocol_version": "1.0",
                "command_id": command_id,
                "device_id": device_id,
                "capability": capability,
                "params": dict(params or {}),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        future = Future()
        self._pending[command_id] = future
        try:
            info = self._client.publish(topic, payload, qos=1)
            if getattr(info, "rc", 0) != 0:
                raise AgentOSCommandError(
                    f"命令发布失败（rc={info.rc}）", kind="network"
                )
            ack = future.result(timeout=self.timeout_seconds)
        except TimeoutError:
            raise AgentOSCommandError(
                f"录音豆没在 {self.timeout_seconds:.0f} 秒内应答 {capability}",
                error_code="ACK_TIMEOUT",
                kind="network",
            ) from None
        except (OSError, ValueError) as error:
            raise AgentOSCommandError(
                f"MQTT 发送失败：{error}", kind="network"
            ) from error
        finally:
            self._pending.pop(command_id, None)
        if ack.get("status") == "failed":
            raise AgentOSCommandError(
                str(ack.get("message") or "设备拒绝执行"),
                error_code=str(ack.get("error_code") or ""),
                kind="failed",
            )
        return ack


class AgentMcpExecutor:
    """把分发器的命令交给同事的硬件动作插件（MCP，HTTP 或 stdio 传输）。

    插件官方链路（README 定义）：本客户端 -> 插件注册表/权限/幂等 ->
    RK3588 /api/agentos/v1/commands -> 网关/MQTT -> 设备 -> ACK 原路返回。
    2026-11-03 用户拍板：这是外设命令的**主执行器**，录音豆、小车都走它；
    MQTT 直连与 HTTP 命令 API 保留为后备通道。MCP 传输与协议细节复用
    ``hardware_mcp.py`` 的既有实现（会话长驻、断线重连、Bearer 令牌）。
    """

    TOOL = "hardware_agent_execute_command"

    def __init__(self, mcp_client: Any):
        self._client = mcp_client

    def execute_command(
        self,
        capability: str,
        device_id: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> Dict[str, Any]:
        args = {
            "request_id": str(request_id or uuid.uuid4().hex),
            "conversation_id": "pet-voice",
            "user_request": f"语音指令 {capability}",
            "device_id": device_id,
            "capability": capability,
            "params": dict(params or {}),
        }
        try:
            outcome = self._client.call_tool(self.TOOL, args)
        except Exception as error:
            # 连接失败/协议错误/调用超时都算够不着插件，交给分发器的
            # 网络话术（「联系不上硬件服务」）。
            raise AgentOSCommandError(
                f"硬件插件调用失败：{error}", kind="network"
            ) from error
        if outcome.ok:
            return outcome.payload
        # timed_out / pending = 设备没确认（可能是网络），按网络类；
        # 其余（DEVICE_NOT_FOUND、CAPABILITY_NOT_SUPPORTED…）是业务拒绝。
        kind = (
            "network"
            if getattr(outcome, "pending", False) or outcome.status in ("timed_out", "unknown")
            else "failed"
        )
        raise AgentOSCommandError(
            outcome.message or "插件返回失败",
            error_code=outcome.error_code or "",
            kind=kind,
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class ActionDispatcher:
    """外设命令的统一出口。插件声明 ActionSpec，执行（含错误话术）在这里。"""

    # 错误码 → 用户听得懂的话。不给 error_code 本身，那是给人看的不是给人听的。
    ERROR_REPLIES: Dict[str, str] = {
        "DEVICE_TIMEOUT": "录音豆半天没回话，可能不在线了，等会儿再试试。",
        "DEVICE_UNVERIFIED": "录音豆还没上线，确认它开机联网后再试。",
        "DEVICE_NOT_FOUND": "现在找不到录音豆这台设备。",
        "DEVICE_ACK_INVALID": "录音豆回了个看不懂的答复，再试一次吧。",
        "MQTT_UNAVAILABLE": "中控和录音豆的通道断了，稍后再试。",
        "CAPABILITY_NOT_SUPPORTED": "录音豆不支持这个操作。",
        "PROFILE_NOT_SUPPORTED": "录音豆的固件版本不支持这个操作。",
        "TF_CARD_UNAVAILABLE": "录音豆的存储卡有问题，检查一下卡。",
        "MICROPHONE_UNAVAILABLE": "录音豆的麦克风出了问题。",
        "INVALID_PARAMS": "这条指令参数不对，我没法执行。",
        "QUEUE_FULL": "录音豆忙不过来了，稍等再说。",
        "UNAUTHORIZED": "中控的硬件钥匙失效了，需要重新配置。",
        "DUPLICATE_REQUEST": "这条命令刚发过，别急。",
    }
    # 原来写「中控联系不上硬件服务」——「中控」「硬件服务」都是内部叫法，
    # 念出来用户不知道在说哪台机器。改成说他面前这台设备。
    _NETWORK_REPLY = "设备现在没连上，检查一下电源和网络再试。"
    _AUTH_REPLY = "中控的硬件钥匙失效了，需要重新配置。"
    _DISABLED_REPLY = "外设控制还没配置好，缺命令接口的密钥。"
    _DEFAULT_REPLY = "这条命令没执行成功，等会儿再试试。"

    def __init__(
        self,
        client: Optional[AgentOSCommandClient],
        *,
        default_device_id: str = "bean_ye_1",
    ):
        self.client = client
        self.default_device_id = str(default_device_id or "bean_ye_1")

    def execute(
        self,
        intent_name: str,
        spec: ActionSpec,
        slots: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> ActionOutcome:
        """执行一条外设命令。任何异常都收敛成 ok=False + 可念的话术。"""
        if self.client is None:
            return ActionOutcome(
                ok=False, status="disabled", user_reply=self._DISABLED_REPLY
            )
        device_id = str(spec.device_id or self.default_device_id)
        # 幂等键带上意图名：同一轮里不同意图不会撞键；同一意图重放去重
        key = f"{request_id}-{intent_name}" if request_id else uuid.uuid4().hex
        attempts = 2 if spec.read_only else 1
        last_error: Optional[AgentOSCommandError] = None
        for attempt in range(attempts):
            try:
                payload = self.client.execute_command(
                    capability=spec.capability,
                    device_id=device_id,
                    params=(slots or {}) if spec.forward_slots else {},
                    request_id=key,
                )
            except AgentOSCommandError as error:
                last_error = error
                # 只有只读命令的网络抖动才值得再试一次
                if not (spec.read_only and error.kind == "network"):
                    break
            else:
                result = payload.get("result")
                return ActionOutcome(
                    ok=True,
                    status=str(payload.get("status") or "succeeded"),
                    result=result if isinstance(result, dict) else {},
                )
        assert last_error is not None
        return self._outcome_from_error(last_error)

    def _outcome_from_error(self, error: AgentOSCommandError) -> ActionOutcome:
        if error.kind == "network":
            reply = self._NETWORK_REPLY
            status = "network_error"
        elif error.kind == "auth":
            reply = self._AUTH_REPLY
            status = "auth_error"
        else:
            status = "failed"
            reply = self.ERROR_REPLIES.get(error.error_code) or self._DEFAULT_REPLY
        return ActionOutcome(
            ok=False,
            status=status,
            error_code=error.error_code,
            detail=str(error),
            user_reply=reply,
        )


def build_dispatcher_from_env() -> ActionDispatcher:
    """按 plugin.env 的配置构造分发器；缺配置时返回 disabled 态，服务照常起。

    执行器优先级（2026-11-03 与用户对齐）：

    1. 同事的硬件动作插件（``HARDWARE_AGENT_MCP_URL``）——主通道，官方链路
       带设备注册表/权限/幂等，录音豆和小车都经它；
    2. MQTT 直连 broker（``RK3588_MQTT_HOST``）——后备；
    3. 8099 HTTP 命令 API（``RK3588_BASE_URL`` + ``AGENTOS_COMMAND_API_KEY``）——再后备。
    """
    default_device = (os.environ.get("RECORD_BEAN_DEVICE_ID") or "").strip() or "bean_ye_1"

    mcp_url = (os.environ.get("HARDWARE_AGENT_MCP_URL") or "").strip()
    if mcp_url:
        from .hardware_mcp import HardwareAgentConfig, McpStdioClient

        config = HardwareAgentConfig.from_env(url=mcp_url)
        executor = AgentMcpExecutor(McpStdioClient(config))
        return ActionDispatcher(executor, default_device_id=default_device)

    mqtt_host = (os.environ.get("RK3588_MQTT_HOST") or "").strip()
    if mqtt_host:
        client = AgentMqttClient(
            mqtt_host,
            int(os.environ.get("RK3588_MQTT_PORT") or 1883),
            (os.environ.get("RK3588_MQTT_USERNAME") or "").strip(),
            os.environ.get("RK3588_MQTT_PASSWORD") or "",
            timeout_seconds=float(os.environ.get("RK3588_MQTT_TIMEOUT") or 10.0),
        )
        return ActionDispatcher(client, default_device_id=default_device)

    base_url = (os.environ.get("RK3588_BASE_URL") or "").strip()
    api_key = (os.environ.get("AGENTOS_COMMAND_API_KEY") or "").strip()
    timeout = float(os.environ.get("RK3588_HTTP_TIMEOUT") or 20.0)
    if not base_url or not api_key:
        return ActionDispatcher(None, default_device_id=default_device)
    client = AgentOSCommandClient(
        base_url,
        api_key,
        timeout_seconds=timeout,
    )
    return ActionDispatcher(client, default_device_id=default_device)
