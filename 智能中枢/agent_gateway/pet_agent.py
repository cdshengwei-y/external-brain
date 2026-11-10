"""桌宠的对话大脑：模型工具循环 + 语音确认策略。

职责边界（对应硬件插件交接文档 v0.1.1 第 1、2.3、4、5 节）：

* 桌宠是「对话宿主」和「MCP 客户端」，不碰 MQTT、不碰设备协议、不拼 HTTP；
* 每一轮对话都由模型判断是否需要调用硬件工具，唤醒词不触发任何硬件动作；
* 会改变设备状态的调用必须先经用户确认；
* 只有插件返回 ``succeeded`` 才能说"已完成"。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any, Deque, Dict, List, Mapping, Optional, Protocol, Sequence

from .hardware_mcp import (
    ADMIN_TOOL_NAMES,
    MAX_COMMANDS_PER_CALL,
    MAX_CONVERSATION_ID_LEN,
    MAX_REQUEST_ID_LEN,
    MAX_USER_REQUEST_LEN,
    QUERY_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    HardwareAgentConfig,
    HardwareAgentConfigError,
    McpCallTimeout,
    McpProcessError,
    McpProtocolError,
    McpStdioClient,
    ToolOutcome,
    to_openai_tools,
)

LOGGER = logging.getLogger("agent_gateway.pet_agent")

SYSTEM_PROMPT = """你是桌面上的智能桌宠，可以陪用户聊天，也可以操作已注册的智能硬件。

硬件工具使用规则：
1. 只能使用 hardware_agent_list_devices 返回的 device_id 和 capabilities，不能猜测设备 ID、能力名或参数。
2. 查询类请求可以直接调用查询工具；会改变设备状态的请求必须先向用户请求确认，用户确认后系统才会真正执行。
3. 工具返回 succeeded 之前，不得声称硬件已经完成动作；失败时必须说明失败原因，不要编造结果。
4. 不要向用户索要或输出 tenant_id、actor_ref、API Key、RK3588 地址等运行时凭据。
5. 一次请求可能需要多次工具调用（例如先查设备列表再执行），但不要在信息已经足够时反复调用。

回答要求：所有回复都会被念出来，请用 1 到 2 句口语化中文，不要用列表、不要用 Markdown、不要念参数名或设备 ID。
"""

#: 用户明确表示执行。
CONFIRM_WORDS = (
    "执行",
    "确认",
    "确定",
    "可以",
    "好的",
    "好",
    "行",
    "开始",
    "动手",
    "开吧",
    "弄吧",
)
#: 用户明确表示取消。
CANCEL_WORDS = (
    "取消",
    "不要",
    "不用",
    "算了",
    "别",
    "停",
    "等等",
    "等一下",
    "先不",
)
#: 置信度太低，按文档要求再问一次。
UNCLEAR_WORDS = ("嗯", "哦", "啊", "好吧", "可能", "也许", "随便", "嗯嗯", "哦哦")

#: 业务错误码 → 播报话术（插件 0.1.1 错误码表，见交接回复 2.4）。
ERROR_PHRASES: Dict[str, str] = {
    "IDENTITY_REQUIRED": "当前账号还没有完成硬件身份配置，这次先不执行。",
    "HARDWARE_NOT_CONFIGURED": "硬件服务地址或密钥没配好，这次先不执行。",
    "UNAUTHORIZED": "硬件服务的密钥不对，我没法执行。",
    "DEVICE_NOT_FOUND": "没有找到这个设备。",
    "DEVICE_UNVERIFIED": "这个设备还没有确认在线，需要我重新验证一下吗？",
    "CAPABILITY_NOT_SUPPORTED": "这个设备不支持这个动作。",
    "INVALID_PARAMS": "命令参数不对，我没有执行。",
    "INVALID_DEVICE_PACKAGE": "这个设备的配置包有问题，我没法使用。",
    "DEVICE_ALREADY_REGISTERED": "这个设备已经被注册了。",
    "FORBIDDEN": "这个操作没有权限，我没有执行。",
    "DEVICE_TIMEOUT": "设备没有在规定时间内返回结果，这次不确定是否生效。",
    "COMMAND_TIMEOUT": "设备没有在规定时间内返回结果，这次不确定是否生效。",
    "DEVICE_ACK_INVALID": "设备返回的确认信息对不上，这次按失败处理。",
    "MQTT_UNAVAILABLE": "设备消息通道暂时不通，稍后再试。",
    "BRIDGE_UNAVAILABLE": "硬件中枢暂时连不上，稍后再试。",
    "RK3588_UNAVAILABLE": "硬件网关现在连不上，这次不确定是否生效。",
    "RK3588_HTTP_ERROR": "硬件网关返回了错误，这次没有确认成功。",
    "INVALID_GATEWAY_RESPONSE": "硬件网关返回的数据不对，这次按失败处理。",
    "QUEUE_FULL": "设备命令队列满了，稍后再试。",
    "INVALID_REQUEST": "请求格式不对，我没有执行。",
    "METHOD_NOT_FOUND": "硬件插件不支持这个操作。",
    "TOOL_NOT_FOUND": "硬件插件里没有这个工具。",
    "PARSE_ERROR": "硬件插件看不懂这次请求，我没有执行。",
    "DEVICE_OFFLINE": "设备当前是离线的。",
    "DUPLICATE_REQUEST": "这个请求已经处理过了，我没有重复执行。",
    "INTERNAL_ERROR": "硬件插件内部出错了，稍后再试。",
    "PLUGIN_UNAVAILABLE": "硬件插件没有启动起来，稍后再试。",
    "PLUGIN_PROTOCOL_ERROR": "硬件插件返回的数据有问题，这次没有执行。",
    "UNKNOWN_TOOL": "我不支持这个操作。",
    "ADMIN_ONLY": "注册和注销设备要在设置页里做，聊天里我先不处理。",
}

#: 传输失败/超时：设备可能已执行也可能没有，播报时不能说"已完成"。
UNCERTAIN_ERROR_CODES = frozenset(
    {"DEVICE_TIMEOUT", "COMMAND_TIMEOUT", "RK3588_UNAVAILABLE", "MQTT_UNAVAILABLE"}
)

SUCCESS_CLAIMS = (
    "已完成",
    "已执行",
    "已经完成",
    "已经打开",
    "已打开",
    "已经关闭",
    "已关闭",
    "成功了",
    "搞定了",
)


class ChatModelError(RuntimeError):
    pass


# 桌宠只连两个地方：内网的推理服务、固定的公网中转。**任何**代理都不该掺进来。
#
# 2026-11-06 16:50 现场：从带代理的终端启动中枢，一句「为什么天空是蓝的」等了 32 秒，
# 那台机器 ping 100% 丢包、chat 502 upstream refused —— 看着像它挂了，
# 但关掉代理立刻恢复（chat 200 / 1.1s）。根因是 TUN/透明代理在网络层把**内网**
# 流量也劫走了。清 HTTP_PROXY 环境变量不够：macOS 上 urlopen 还会读系统代理设置
# （_scproxy）。2026-11-10 起请求改走 http.client 直连——它天生不读任何
# 代理设置，比当年的 ProxyHandler({}) 更彻底；这里的教训保留作历史注脚。


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


class ChatModel(Protocol):
    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """返回 ``{"content": str, "tool_calls": [ToolCall, ...]}``。"""


class OpenAICompatibleChatModel:
    """最小可用的 /chat/completions 客户端，支持原生 function calling。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout_seconds: float = 30.0,
        max_tokens: Optional[int] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        # sub2api / sw-bot 这类站点前面挂了 WAF，非浏览器 UA 会直接 403。
        self.user_agent = user_agent
        # keep-alive 连接池：245 那条链路有 1~2% 的间歇性丢包（2026-11-10 实测），
        # urllib 每次请求都重新走 TCP 握手，一个 SYN 丢了就是 1 秒起步的卡顿。
        # 复用连接后只在第一次建链，后续请求零握手，抖动窗口直接砍掉一大块。
        # http.client 天生不走任何代理（比 ProxyHandler({}) 更彻底）。
        self._pool_lock = threading.Lock()
        self._idle_conns: List[http.client.HTTPConnection] = []

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional["OpenAICompatibleChatModel"]:
        source = os.environ if environ is None else environ
        base_url = str(
            source.get("PET_AGENT_CHAT_BASE_URL") or source.get("OPENAI_BASE_URL") or ""
        ).strip()
        model = str(
            source.get("PET_AGENT_CHAT_MODEL") or source.get("OPENAI_MODEL") or ""
        ).strip()
        if not base_url or not model:
            return None
        api_key = str(
            source.get("PET_AGENT_CHAT_API_KEY")
            or source.get("OPENAI_API_KEY")
            or ""
        ).strip()
        return cls(base_url, model, api_key or None)

    # -- keep-alive 连接池 -------------------------------------------------

    def _new_conn(self, target: Any) -> http.client.HTTPConnection:
        factory = (
            http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        )
        conn = factory(target.hostname, target.port, timeout=self.timeout_seconds)
        setattr(conn, "pool_key", (target.scheme, target.hostname, target.port))
        return conn

    def _acquire_conn(self, target: Any) -> tuple[http.client.HTTPConnection, bool]:
        """取一条连接：优先复用池里的空闲连接（reused=True），否则新建。"""
        key = (target.scheme, target.hostname, target.port)
        with self._pool_lock:
            while self._idle_conns:
                conn = self._idle_conns.pop()
                if getattr(conn, "pool_key", None) == key:
                    return conn, True
                try:  # 池里混进了别的端点的连接（不该发生），关掉别浪费
                    conn.close()
                except OSError:
                    pass
        return self._new_conn(target), False

    def _release_conn(self, conn: http.client.HTTPConnection, reusable: bool) -> None:
        if reusable:
            with self._pool_lock:
                if len(self._idle_conns) < 4:  # 池子不深，桌宠同时也就一两路
                    self._idle_conns.append(conn)
                    return
        try:
            conn.close()
        except OSError:
            pass

    def _post_persistent(
        self, target: Any, body: bytes, headers: Dict[str, str]
    ) -> Dict[str, Any]:
        """keep-alive POST：池里拿连接 → 发请求 → 可复用就还回池子。

        池里的连接可能早已被链路抖断（对端 RST、中间设备悄悄丢状态），
        所以**复用连接上的第一次失败**免费换一条全新连接重试一次；
        新连接再失败才向上抛 —— 真正的服务端故障不会被这个重试放大。
        """
        path = target.path or "/"
        if target.query:
            path = f"{path}?{target.query}"
        for attempt in (1, 2):
            conn, reused = self._acquire_conn(target)
            try:
                conn.request("POST", path, body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                reusable = not response.will_close
                status = response.status
            except (http.client.HTTPException, OSError):
                self._release_conn(conn, False)
                if reused and attempt == 1:
                    continue  # 换新连接再试一次（旧连接八成是断着的）
                raise
            self._release_conn(conn, reusable)
            if status >= 400:
                detail = raw.decode("utf-8", "replace")[:300]
                raise ChatModelError(f"模型接口返回 HTTP {status}: {detail}")
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ChatModelError("模型接口返回的不是有效 JSON") from error
        raise ChatModelError("模型接口连接重试失败")  # 理论不可达，防御兜底

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0.2,
            "stream": False,
        }
        if self.max_tokens:
            payload["max_tokens"] = int(self.max_tokens)
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        target = urlsplit(f"{self.base_url}/chat/completions")
        try:
            body_out = self._post_persistent(target, body, headers)
        except ChatModelError:
            raise
        except (http.client.HTTPException, OSError) as error:
            raise ChatModelError(f"无法连接模型接口: {error}") from error

        choices = body_out.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ChatModelError("模型接口没有返回 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        return {
            "content": content.strip() if isinstance(content, str) else "",
            "tool_calls": _parse_tool_calls(message.get("tool_calls")),
            "raw": message,
        }


class ScriptedChatModel:
    """不依赖任何模型服务的脚本模型，用来验证整条链路是否通。

    它只做两件事：把用户的话映射成工具调用；把工具结果说成人话。
    接真实模型时换成 OpenAICompatibleChatModel 即可，PetAgent 不用改。

    默认设备可用环境变量 ``PET_AGENT_SCRIPTED_DEVICE`` 指定，
    默认 ``bean_ye_1``（录音豆）——这是当前唯一在 RK3588 注册过的真机。
    """

    DEVICE_WORDS = ("设备", "有什么", "蜂", "灯")
    POWER_WORDS = ("打开", "开启", "关掉", "关闭")
    RECORD_STOP_WORDS = ("停止录音", "别录", "不要录", "停掉录音", "结束录音")
    RECORD_START_WORDS = ("开始录音", "录音", "录一下", "录个音")
    STATUS_WORDS = ("状态", "在不在", "在线", "连上了吗")

    def __init__(self, default_device_id: Optional[str] = None) -> None:
        self.default_device_id = (
            default_device_id
            or os.environ.get("PET_AGENT_SCRIPTED_DEVICE")
            or "bean_ye_1"
        )

    def _call(self, capability: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "content": "",
            "tool_calls": [
                ToolCall(
                    id=f"scripted-{int(time.time() * 1000)}",
                    name="hardware_agent_execute_command",
                    arguments={
                        "device_id": self.default_device_id,
                        "capability": capability,
                        "params": params or {},
                    },
                )
            ],
        }

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            return {
                "content": _summarize_tool_result(str(last.get("content") or "")),
                "tool_calls": [],
            }

        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = str(message.get("content") or "")
                break

        # 顺序有讲究：「停止录音」里也含「录音」，必须先判停止。
        if any(word in user_text for word in self.RECORD_STOP_WORDS):
            return self._call("record.stop")
        if any(word in user_text for word in self.RECORD_START_WORDS):
            return self._call("record.start")
        if any(word in user_text for word in self.STATUS_WORDS):
            return self._call("status.read")

        wants_power = any(word in user_text for word in self.POWER_WORDS)
        if wants_power:
            power = "off" if ("关闭" in user_text or "关掉" in user_text) else "on"
            return self._call("light.power.set", {"power": power})
        if any(word in user_text for word in self.DEVICE_WORDS):
            return {
                "content": "",
                "tool_calls": [
                    ToolCall(
                        id=f"scripted-{int(time.time() * 1000)}",
                        name="hardware_agent_list_devices",
                        arguments={},
                    )
                ],
            }
        return {
            "content": (
                "我在。还没接上大模型，只会说几句固定的话；"
                "说“有哪些设备”“开始录音”“停止录音”我能真的执行。"
            ),
            "tool_calls": [],
        }


def _summarize_tool_result(raw: str) -> str:
    """把工具返回的 JSON 压成一句能念出来的话。"""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "收到执行结果。"
    if not isinstance(payload, dict):
        return "收到执行结果。"

    devices = payload.get("devices")
    if isinstance(devices, list):
        names = [
            str(item.get("name") or item.get("device_id"))
            for item in devices
            if isinstance(item, dict)
        ]
        return "现在的设备有：" + "、".join(names) + "。" if names else "现在没有可用设备。"

    if payload.get("status") == "succeeded":
        return "好的，已经处理完了。"
    # 失败时优先用统一话术表，避免把插件原文（含设备 ID、英文）念给用户。
    code = str(payload.get("error_code") or "")
    if code and code in ERROR_PHRASES:
        return ERROR_PHRASES[code]
    return str(payload.get("message") or "这次没有成功。")


def _parse_tool_calls(raw_calls: Any) -> List[ToolCall]:
    calls: List[ToolCall] = []
    if not isinstance(raw_calls, list):
        return calls
    for index, item in enumerate(raw_calls):
        if not isinstance(item, Mapping):
            continue
        function = item.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                LOGGER.warning("模型返回的工具参数不是合法 JSON，已按空参数处理")
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call-{index}-{uuid.uuid4().hex[:6]}"),
                name=name.strip(),
                arguments=arguments,
            )
        )
    return calls


@dataclass
class PendingPlan:
    """待确认的写操作计划，绑定 conversation_id 与 request_id。"""

    plan_id: str
    request_id: str
    conversation_id: str
    user_request: str
    tool_name: str
    commands: List[Dict[str, Any]]
    created_at: float
    expires_at: float
    prompt: str

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.monotonic()) > self.expires_at

    def public(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "tool": self.tool_name,
            "commands": list(self.commands),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "prompt": self.prompt,
        }


@dataclass
class AgentTurn:
    text: str
    needs_confirmation: bool = False
    plan_id: Optional[str] = None
    executed: bool = False
    outcomes: List[ToolOutcome] = field(default_factory=list)
    error_code: Optional[str] = None
    tool_rounds: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "needs_confirmation": self.needs_confirmation,
            "plan_id": self.plan_id,
            "executed": self.executed,
            "error_code": self.error_code,
            "tool_rounds": self.tool_rounds,
            "outcomes": [
                {
                    "tool": outcome.name,
                    "status": outcome.status,
                    "ok": outcome.ok,
                    "error_code": outcome.error_code,
                }
                for outcome in self.outcomes
            ],
        }


class DeviceCatalog:
    """缓存 list_devices 的返回，用于把 device_id 说成人话。"""

    def __init__(self) -> None:
        self._devices: Dict[str, Dict[str, Any]] = {}

    def update(self, payload: Mapping[str, Any]) -> None:
        devices = payload.get("devices")
        if not isinstance(devices, list):
            return
        for device in devices:
            if not isinstance(device, Mapping):
                continue
            device_id = device.get("device_id")
            if isinstance(device_id, str) and device_id.strip():
                self._devices[device_id] = dict(device)

    def name(self, device_id: str) -> str:
        device = self._devices.get(device_id) or {}
        name = device.get("name")
        return name if isinstance(name, str) and name.strip() else device_id

    def label(self, device_id: str, capability: str) -> str:
        device = self._devices.get(device_id) or {}
        capabilities = device.get("capabilities")
        if isinstance(capabilities, list):
            for item in capabilities:
                if not isinstance(item, Mapping):
                    continue
                if item.get("name") == capability:
                    label = item.get("label")
                    if isinstance(label, str) and label.strip():
                        return label
        return capability


def classify_confirmation(text: str) -> str:
    """把一句语音归到 confirm / cancel / unclear / new。"""
    normalized = "".join(str(text or "").split())
    stripped = normalized.strip("。，,.!！?？~～　 ")
    if not stripped:
        return "unclear"
    if any(word in stripped for word in CANCEL_WORDS):
        return "cancel"
    if stripped in UNCLEAR_WORDS:
        return "unclear"
    if any(word in stripped for word in CONFIRM_WORDS):
        return "confirm"
    return "new"


class PetAgent:
    """一次唤醒之后的所有对话轮次都由它处理。"""

    FORBIDDEN_ARGUMENT_KEYS = frozenset(
        {
            "request_id",
            "conversation_id",
            "user_request",
            "plan_id",
            "tenant_id",
            "actor_ref",
            "api_key",
            "AGENTOS_COMMAND_API_KEY",
            "rk3588_base_url",
            "RK3588_BASE_URL",
        }
    )

    def __init__(
        self,
        model: ChatModel,
        client: McpStdioClient,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_tool_rounds: int = 4,
        confirmation_ttl: float = 60.0,
        history_limit: int = 8,
        narrate_with_model: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.model = model
        self.client = client
        self.system_prompt = system_prompt
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.confirmation_ttl = max(5.0, float(confirmation_ttl))
        self.history_limit = max(2, int(history_limit))
        self.narrate_with_model = narrate_with_model
        self._logger = logger or LOGGER
        self.catalog = DeviceCatalog()
        self._histories: Dict[str, Deque[Dict[str, Any]]] = {}
        self._pending: Dict[str, PendingPlan] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._tools_cache: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def handle_turn(
        self,
        conversation_id: str,
        user_text: str,
        request_id: Optional[str] = None,
    ) -> AgentTurn:
        text = (user_text or "").strip()
        with self._conversation_lock(conversation_id):
            if not text:
                return AgentTurn("我在，需要我做什么？")

            pending = self._pending.get(conversation_id)
            if pending is not None:
                decision = classify_confirmation(text)
                if pending.expired():
                    self._pending.pop(conversation_id, None)
                    if decision == "confirm":
                        return AgentTurn("刚才那个计划已经过期了，请再说一次要做什么。")
                elif decision == "confirm":
                    return self._execute_plan(pending)
                elif decision == "cancel":
                    self._pending.pop(conversation_id, None)
                    self._remember(conversation_id, text, "好的，已取消。")
                    return AgentTurn("好的，已取消。")
                elif decision == "unclear":
                    reminder = f"{pending.prompt} 请说“执行”或者“取消”。"
                    return AgentTurn(reminder, needs_confirmation=True, plan_id=pending.plan_id)
                else:
                    # 用户换了话题，丢弃待执行计划（文档第 4 节）。
                    self._pending.pop(conversation_id, None)

            return self._run_tool_loop(conversation_id, text, request_id)

    def pending_plan(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        plan = self._pending.get(conversation_id)
        return plan.public() if plan is not None else None

    def close(self) -> None:
        try:
            self.client.close()
        except Exception as error:  # pragma: no cover - 关闭路径保持安静
            self._logger.debug("关闭硬件插件失败: %s", error)

    # ------------------------------------------------------------------
    # 工具循环
    # ------------------------------------------------------------------
    def _run_tool_loop(
        self,
        conversation_id: str,
        text: str,
        request_id: Optional[str],
    ) -> AgentTurn:
        tools = self._model_tools()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        messages.extend(self._history(conversation_id))
        messages.append({"role": "user", "content": text})

        rounds = 0
        while rounds < self.max_tool_rounds:
            try:
                reply = self.model.complete(messages, tools)
            except ChatModelError as error:
                self._logger.warning("模型调用失败: %s", error)
                return AgentTurn(
                    "我这会儿连不上大脑，稍后再问我一次。",
                    error_code="MODEL_UNAVAILABLE",
                )
            tool_calls: List[ToolCall] = list(reply.get("tool_calls") or [])
            content = str(reply.get("content") or "").strip()

            if not tool_calls:
                answer = content or "我没听清，可以再说一次吗？"
                self._remember(conversation_id, text, answer)
                return AgentTurn(answer, tool_rounds=rounds)

            write_call = next(
                (call for call in tool_calls if call.name in WRITE_TOOL_NAMES), None
            )
            if write_call is not None:
                plan = self._build_plan(conversation_id, text, request_id, write_call)
                if plan is None:
                    return AgentTurn(
                        "我没能确定要操作哪个设备，可以说得再具体一点吗？",
                        error_code="PLAN_INCOMPLETE",
                        tool_rounds=rounds,
                    )
                self._pending[conversation_id] = plan
                self._remember(conversation_id, text, plan.prompt)
                self._logger.info(
                    "待确认计划 conversation=%s request=%s tool=%s commands=%d",
                    conversation_id,
                    plan.request_id,
                    plan.tool_name,
                    len(plan.commands),
                )
                return AgentTurn(
                    plan.prompt,
                    needs_confirmation=True,
                    plan_id=plan.plan_id,
                    tool_rounds=rounds,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(
                                    call.arguments, ensure_ascii=False
                                ),
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                outcome = self._call_query_tool(call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": outcome.to_model_content(),
                    }
                )
            rounds += 1

        self._logger.warning("工具循环超过 %d 轮，已停止", self.max_tool_rounds)
        return AgentTurn(
            "这个需求我得多想一步，稍后再试一次好吗？",
            error_code="TOOL_ROUND_LIMIT",
            tool_rounds=rounds,
        )

    def _model_tools(self) -> List[Dict[str, Any]]:
        if self._tools_cache:
            return list(self._tools_cache)
        try:
            tools = self.client.list_tools()
        except (McpProcessError, McpProtocolError, McpCallTimeout, OSError) as error:
            self._logger.warning("获取硬件工具清单失败: %s", error)
            return []
        self._tools_cache = to_openai_tools(tools, exclude=sorted(ADMIN_TOOL_NAMES))
        return list(self._tools_cache)

    def _known_tool_names(self) -> set:
        return {
            str(tool.get("name"))
            for tool in self.client.tools
            if isinstance(tool, Mapping) and tool.get("name")
        }

    def _call_query_tool(self, call: ToolCall) -> ToolOutcome:
        if call.name in ADMIN_TOOL_NAMES:
            return _synthetic(call.name, "ADMIN_ONLY")
        known = self._known_tool_names()
        if known and call.name not in known:
            return _synthetic(call.name, "UNKNOWN_TOOL")
        arguments = self._sanitize_arguments(call.arguments)
        if call.name not in WRITE_TOOL_NAMES:
            # 查询工具只接受空参数，避免模型塞进不相关字段。
            arguments = {}
        try:
            outcome = self.client.call_tool(call.name, arguments)
        except McpCallTimeout as error:
            self._logger.warning("工具调用超时: %s (sent=%s)", call.name, error.sent)
            return _synthetic(call.name, "COMMAND_TIMEOUT")
        except McpProcessError as error:
            self._logger.warning("硬件插件不可用: %s", error)
            return _synthetic(call.name, "PLUGIN_UNAVAILABLE")
        except McpProtocolError as error:
            self._logger.warning("硬件插件协议错误: %s", error)
            return _synthetic(call.name, error.error_code or "PLUGIN_PROTOCOL_ERROR")
        if call.name == "hardware_agent_list_devices" and outcome.ok:
            self.catalog.update(outcome.payload)
        return outcome

    def _sanitize_arguments(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        """模型只能给 device_id / capability / params / commands，其余一律丢弃。"""
        clean: Dict[str, Any] = {}
        for key, value in (arguments or {}).items():
            if key in self.FORBIDDEN_ARGUMENT_KEYS:
                self._logger.warning("模型尝试传入受限字段 %s，已丢弃", key)
                continue
            if key == "commands" and isinstance(value, list):
                clean["commands"] = [
                    self._sanitize_command(item) for item in value[:MAX_COMMANDS_PER_CALL]
                ]
            elif key == "params" and isinstance(value, Mapping):
                clean["params"] = dict(value)
            elif key in {"device_id", "capability"} and isinstance(value, str):
                clean[key] = value.strip()
            elif key in {"device_id", "capability", "params", "commands"}:
                continue
            else:
                clean[key] = value
        return clean

    @staticmethod
    def _sanitize_command(item: Any) -> Dict[str, Any]:
        if not isinstance(item, Mapping):
            return {}
        command: Dict[str, Any] = {}
        device_id = item.get("device_id")
        capability = item.get("capability")
        if isinstance(device_id, str):
            command["device_id"] = device_id.strip()
        if isinstance(capability, str):
            command["capability"] = capability.strip()
        params = item.get("params")
        command["params"] = dict(params) if isinstance(params, Mapping) else {}
        return command

    # ------------------------------------------------------------------
    # 确认与执行
    # ------------------------------------------------------------------
    def _build_plan(
        self,
        conversation_id: str,
        user_request: str,
        request_id: Optional[str],
        call: ToolCall,
    ) -> Optional[PendingPlan]:
        if call.name in ADMIN_TOOL_NAMES:
            return None
        arguments = self._sanitize_arguments(call.arguments)
        if call.name == "hardware_agent_execute_commands":
            raw_commands = arguments.get("commands")
            if not isinstance(raw_commands, list):
                return None
            commands = [
                command
                for command in raw_commands
                if command.get("device_id") and command.get("capability")
            ]
            if not commands:
                return None
        else:
            commands = [
                {
                    "device_id": arguments.get("device_id"),
                    "capability": arguments.get("capability"),
                    "params": arguments.get("params") or {},
                }
            ]
            if not commands[0]["device_id"] or not commands[0]["capability"]:
                return None

        now = time.monotonic()
        plan = PendingPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:12]}",
            request_id=(request_id or f"req-{uuid.uuid4().hex[:12]}")[
                :MAX_REQUEST_ID_LEN
            ],
            # 宿主内部仍用原始 conversation_id 做 key，发给插件时才截断。
            conversation_id=conversation_id,
            user_request=user_request[:MAX_USER_REQUEST_LEN],
            tool_name=call.name,
            commands=commands,
            created_at=now,
            expires_at=now + self.confirmation_ttl,
            prompt="",
        )
        plan.prompt = self._compose_confirmation(plan)
        return plan

    def _compose_confirmation(self, plan: PendingPlan) -> str:
        actions = []
        for command in plan.commands:
            device_name = self.catalog.name(str(command.get("device_id")))
            label = self.catalog.label(
                str(command.get("device_id")), str(command.get("capability"))
            )
            actions.append(f"对「{device_name}」执行「{label}」")
        if len(actions) == 1:
            return f"我准备{actions[0]}，要执行吗？"
        joined = "、".join(actions)
        return f"我准备执行 {len(actions)} 个动作：{joined}。要执行吗？"

    def _execute_plan(self, plan: PendingPlan) -> AgentTurn:
        self._pending.pop(plan.conversation_id, None)
        arguments: Dict[str, Any] = {
            "request_id": plan.request_id[:MAX_REQUEST_ID_LEN],
            # 插件 inputSchema 对 conversation_id 限长 120，超长会被判 INVALID_PARAMS。
            "conversation_id": plan.conversation_id[:MAX_CONVERSATION_ID_LEN],
            "user_request": plan.user_request[:MAX_USER_REQUEST_LEN],
            "plan_id": plan.plan_id[:MAX_REQUEST_ID_LEN],
        }
        if plan.tool_name == "hardware_agent_execute_commands":
            arguments["commands"] = [dict(command) for command in plan.commands]
        else:
            command = plan.commands[0]
            arguments.update(
                {
                    "device_id": command.get("device_id"),
                    "capability": command.get("capability"),
                    "params": command.get("params") or {},
                }
            )
        try:
            outcome = self.client.call_tool(plan.tool_name, arguments)
        except McpCallTimeout as error:
            self._logger.warning(
                "执行超时 request=%s sent=%s", plan.request_id, error.sent
            )
            outcome = _synthetic(plan.tool_name, "DEVICE_TIMEOUT")
        except McpProcessError as error:
            self._logger.warning("执行时插件不可用: %s", error)
            outcome = _synthetic(plan.tool_name, "PLUGIN_UNAVAILABLE")
        except McpProtocolError as error:
            self._logger.warning("执行时协议错误: %s", error)
            outcome = _synthetic(
                plan.tool_name, error.error_code or "PLUGIN_PROTOCOL_ERROR"
            )

        if outcome.name == "hardware_agent_list_devices" and outcome.ok:
            self.catalog.update(outcome.payload)

        text = self._phrase_execution(plan, outcome)
        self._remember(plan.conversation_id, None, text)
        self._logger.info(
            "执行完成 conversation=%s request=%s status=%s code=%s",
            plan.conversation_id,
            plan.request_id,
            outcome.status,
            outcome.error_code or "-",
        )
        return AgentTurn(
            text,
            executed=True,
            plan_id=plan.plan_id,
            outcomes=[outcome],
            error_code=None if outcome.ok else (outcome.error_code or outcome.status),
        )

    def _phrase_execution(self, plan: PendingPlan, outcome: ToolOutcome) -> str:
        baseline = self._deterministic_reply(plan, outcome)
        if not self.narrate_with_model:
            return baseline
        try:
            reply = self.model.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "你在播报一次硬件执行结果。只能用给定的执行结果 JSON，"
                            "成功才能说已完成，失败必须说明原因。用 1 到 2 句口语化中文，"
                            "不要念参数名、不要用列表、不要编造。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "user_request": plan.user_request,
                                "actions": plan.commands,
                                "result": {
                                    "status": outcome.status,
                                    "error_code": outcome.error_code,
                                    "message": outcome.message,
                                    "payload": outcome.payload,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                None,
            )
        except ChatModelError as error:
            self._logger.debug("结果播报模型不可用，回退固定话术: %s", error)
            return baseline

        candidate = str(reply.get("content") or "").strip()
        if not candidate:
            return baseline
        if not outcome.ok and any(word in candidate for word in SUCCESS_CLAIMS):
            self._logger.warning("模型播报与实际结果不符，已回退固定话术")
            return baseline
        return candidate

    def _deterministic_reply(self, plan: PendingPlan, outcome: ToolOutcome) -> str:
        if outcome.ok:
            if len(plan.commands) == 1:
                device_name = self.catalog.name(str(plan.commands[0].get("device_id")))
                label = self.catalog.label(
                    str(plan.commands[0].get("device_id")),
                    str(plan.commands[0].get("capability")),
                )
                return f"已经完成：{device_name}的「{label}」。"
            return f"已经完成 {len(plan.commands)} 个动作。"
        code = outcome.error_code or ""
        reason = ERROR_PHRASES.get(code) or outcome.message or "执行失败。"
        if code == "DUPLICATE_REQUEST":
            return f"{reason}"
        if outcome.status in {"queued", "executing", "pending", "unknown", ""}:
            return f"这次没有拿到确认结果。{reason}"
        return reason

    # ------------------------------------------------------------------
    # 会话记忆
    # ------------------------------------------------------------------
    def _conversation_lock(self, conversation_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(conversation_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[conversation_id] = lock
            return lock

    def _history(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._guard:
            history = self._histories.get(conversation_id)
            return list(history) if history is not None else []

    def _remember(
        self,
        conversation_id: str,
        user_text: Optional[str],
        assistant_text: str,
    ) -> None:
        with self._guard:
            history = self._histories.get(conversation_id)
            if history is None:
                history = deque(maxlen=self.history_limit)
                self._histories[conversation_id] = history
            if user_text:
                history.append({"role": "user", "content": user_text})
            if assistant_text:
                history.append({"role": "assistant", "content": assistant_text})


def _synthetic(tool_name: str, error_code: str) -> ToolOutcome:
    payload: Dict[str, Any] = {"status": "failed", "error_code": error_code}
    if error_code in UNCERTAIN_ERROR_CODES:
        # 传输失败/超时：设备可能已经执行，也可能没有，不能算完成。
        payload["pending"] = True
    return ToolOutcome(
        name=tool_name,
        status="failed",
        ok=False,
        payload=payload,
        error_code=error_code,
        message=ERROR_PHRASES.get(error_code, "执行失败。"),
        raw_text=json.dumps(payload, ensure_ascii=False),
        is_error=True,
    )


def build_agent_from_env(
    environ: Optional[Mapping[str, str]] = None,
    *,
    model: Optional[ChatModel] = None,
    client: Optional[McpStdioClient] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[PetAgent]:
    """按环境变量组装桌宠大脑；配置不全时返回 None，让宿主退回旧链路。"""
    source = dict(os.environ if environ is None else environ)
    log = logger or LOGGER
    if str(source.get("PET_AGENT_ENABLED", "1")).strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        log.info("PET_AGENT_ENABLED=0，桌宠大脑未启用")
        return None

    try:
        allow_missing_identity = str(
            source.get("HARDWARE_AGENT_ALLOW_MISSING_IDENTITY", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        config = HardwareAgentConfig.from_env(
            source, require_identity=not allow_missing_identity
        )
    except HardwareAgentConfigError as error:
        log.warning("桌宠大脑未启用：%s", error)
        return None

    resolved_model = model or OpenAICompatibleChatModel.from_env(source)
    if resolved_model is None:
        log.warning("桌宠大脑未启用：未配置 PET_AGENT_CHAT_BASE_URL / PET_AGENT_CHAT_MODEL")
        return None

    narrate = str(source.get("PET_AGENT_NARRATE", "model")).strip().lower() != "local"
    agent = PetAgent(
        resolved_model,
        client or McpStdioClient(config, logger=log),
        narrate_with_model=narrate,
        max_tool_rounds=int(
            str(source.get("PET_AGENT_MAX_TOOL_ROUNDS") or "4").strip() or 4
        ),
        confirmation_ttl=float(
            str(source.get("PET_AGENT_CONFIRM_TTL") or "60").strip() or 60
        ),
        logger=log,
    )
    log.info("桌宠大脑已启用：%s", config.describe().get("command"))
    return agent
