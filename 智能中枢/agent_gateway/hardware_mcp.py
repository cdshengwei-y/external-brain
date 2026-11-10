"""智能硬件体插件的 MCP 客户端。

一个协议、两种传输：

* **远端接口**（``HARDWARE_AGENT_MCP_URL``）—— 插件已经跑在别处，宿主只发
  HTTP 请求。这是默认推荐形态：不需要本地可执行文件，跨平台，也不用管
  对方的 exe 是 Windows 还是 Linux。
* **本地子进程**（``HARDWARE_AGENT_MCP_COMMAND``）—— 插件由宿主按会话拉起，
  stdin/stdout 走 JSON-RPC，stderr 单独收日志。

协议契约来自硬件插件团队交接文档 v0.1.1：

    initialize -> notifications/initialized -> tools/list -> tools/call

远端传输按 MCP Streamable HTTP 实现：POST JSON-RPC，响应可以是
``application/json``，也可以是 ``text/event-stream``（逐条 ``data:`` 解析）；
服务端下发 ``Mcp-Session-Id`` 时后续请求自动带上。

身份与密钥只由宿主通过环境变量/请求头注入，不进模型上下文、不进日志。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger("agent_gateway.hardware_mcp")

DEFAULT_PROTOCOL_VERSION = "2025-03-26"

ENV_TENANT_ID = "HARDWARE_AGENT_TENANT_ID"
ENV_ACTOR_REF = "HARDWARE_AGENT_ACTOR_REF"
ENV_RK3588_BASE_URL = "RK3588_BASE_URL"
ENV_COMMAND_API_KEY = "AGENTOS_COMMAND_API_KEY"
ENV_HTTP_TIMEOUT = "RK3588_HTTP_TIMEOUT"
ENV_DATA_DIR = "VHA_DATA_DIR"
ENV_MCP_COMMAND = "HARDWARE_AGENT_MCP_COMMAND"
ENV_MCP_URL = "HARDWARE_AGENT_MCP_URL"
ENV_MCP_TOKEN = "HARDWARE_AGENT_MCP_TOKEN"
ENV_MCP_TOKEN_HEADER = "HARDWARE_AGENT_MCP_TOKEN_HEADER"
ENV_MCP_TOKEN_PREFIX = "HARDWARE_AGENT_MCP_TOKEN_PREFIX"

REQUIRED_ENV_KEYS = (
    ENV_TENANT_ID,
    ENV_ACTOR_REF,
    ENV_RK3588_BASE_URL,
    ENV_COMMAND_API_KEY,
)

#: 只读查询，失败后允许在新进程上重试一次。
QUERY_TOOL_NAMES = frozenset(
    {
        "hardware_agent_list_devices",
        "hardware_agent_get_device_status",
        "hardware_agent_verify_device",
    }
)

#: 会改变设备状态，必须用户确认，且不得自动换 request_id 重发。
WRITE_TOOL_NAMES = frozenset(
    {
        "hardware_agent_execute_command",
        "hardware_agent_execute_commands",
    }
)

#: 设置页/管理员流程专用，不允许在普通聊天里触发。
ADMIN_TOOL_NAMES = frozenset(
    {
        "hardware_agent_register_device",
        "hardware_agent_unregister_device",
    }
)

MAX_COMMANDS_PER_CALL = 8

#: 插件侧字段长度上限（见插件 tools/list 的 inputSchema）。
MAX_REQUEST_ID_LEN = 120
MAX_CONVERSATION_ID_LEN = 120
MAX_USER_REQUEST_LEN = 2000

#: 非终态：设备还没给出结果，绝不能当成完成。
#: 插件回复里 ``isError`` 只对 ``failed`` 置 true，``timed_out`` 不会，
#: 所以必须继续解析内层 ``status``。
INCOMPLETE_STATUSES = frozenset({"queued", "executing", "pending", "timed_out", "unknown"})


class HardwareAgentError(RuntimeError):
    """硬件插件相关错误的基类。"""


class HardwareAgentConfigError(HardwareAgentError):
    """环境变量缺失或配置非法。"""


class McpProcessError(HardwareAgentError):
    """子进程启动失败或已退出。"""


class McpProtocolError(HardwareAgentError):
    """JSON-RPC 层错误（工具不存在、参数格式错误、协议错误等）。

    插件会把业务错误码塞在 ``error.data.error_code`` 里（交接回复 4.4 的
    ``INVALID_PARAMS`` 就是这样），所以这里把它带出来，宿主才能给用户
    一句人话，而不是"返回错误(-32000)"。
    """

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_code = error_code


class McpCallTimeout(HardwareAgentError):
    """调用超时。

    ``sent`` 表示请求是否已经写入管道：对写操作而言，已写入但没收到响应
    属于结果未知，绝不能再生成新的 ``request_id`` 重发。
    """

    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


@dataclass(frozen=True)
class HardwareAgentConfig:
    """插件连接配置：``url`` 有值走远端接口，否则启动本地子进程。"""

    command: Tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    startup_timeout: float = 5.0
    call_timeout: float = 20.0
    stderr_log_path: Optional[str] = None
    protocol_version: str = DEFAULT_PROTOCOL_VERSION
    url: Optional[str] = None
    token: Optional[str] = None
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def describe(self) -> Dict[str, Any]:
        """只含键名与地址，用于日志，不含任何密钥值。"""
        return {
            "transport": self.transport,
            "command": list(self.command),
            "url": _redact_url(self.url),
            "has_token": bool(self.token),
            "env_keys": sorted(self.env),
            "cwd": self.cwd,
            "startup_timeout": self.startup_timeout,
            "call_timeout": self.call_timeout,
        }

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        command: Optional[Sequence[str]] = None,
        require_identity: bool = True,
        url: Optional[str] = None,
    ) -> "HardwareAgentConfig":
        source = dict(os.environ if environ is None else environ)

        resolved_url = str(url or source.get(ENV_MCP_URL) or "").strip() or None
        if resolved_url and not resolved_url.lower().startswith(("http://", "https://")):
            raise HardwareAgentConfigError(
                f"{ENV_MCP_URL} 必须以 http:// 或 https:// 开头，当前为 {resolved_url!r}"
            )

        if resolved_url:
            # 远端模式：身份和密钥都由服务端持有，宿主只需要地址和访问令牌。
            token_header = (
                str(source.get(ENV_MCP_TOKEN_HEADER) or "").strip() or "Authorization"
            )
            token_prefix = str(source.get(ENV_MCP_TOKEN_PREFIX) or "").strip()
            if not token_prefix and token_header.lower() == "authorization":
                token_prefix = "Bearer "
            return cls(
                command=(),
                env={},
                url=resolved_url,
                token=str(source.get(ENV_MCP_TOKEN) or "").strip() or None,
                token_header=token_header,
                token_prefix=token_prefix,
                startup_timeout=_positive_float(
                    source.get("HARDWARE_AGENT_MCP_STARTUP_TIMEOUT"), default=10.0
                ),
                call_timeout=max(
                    20.0,
                    _positive_float(
                        source.get("HARDWARE_AGENT_MCP_CALL_TIMEOUT"),
                        default=60.0,
                    ),
                ),
            )

        resolved_command = _resolve_command(command or source.get(ENV_MCP_COMMAND))
        if not resolved_command:
            raise HardwareAgentConfigError(
                "未配置硬件插件：请设置远端接口地址 "
                f"{ENV_MCP_URL}，或本地可执行文件 {ENV_MCP_COMMAND}"
            )

        missing = [
            key
            for key in REQUIRED_ENV_KEYS
            if require_identity and not str(source.get(key) or "").strip()
        ]
        if missing:
            raise HardwareAgentConfigError(
                "硬件插件运行配置缺少必填环境变量: " + ", ".join(missing)
            )

        child_env: Dict[str, str] = {}
        for key in REQUIRED_ENV_KEYS:
            value = str(source.get(key) or "").strip()
            if value:
                child_env[key] = value

        http_timeout = _positive_float(source.get(ENV_HTTP_TIMEOUT), default=15.0)
        child_env[ENV_HTTP_TIMEOUT] = _format_number(http_timeout)

        data_dir = str(source.get(ENV_DATA_DIR) or "").strip() or str(
            Path.home() / ".smart-hub" / "hardware-agent"
        )
        try:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
        except OSError as error:  # 目录不可写时仍然继续，交给插件自己报错
            LOGGER.warning("硬件插件数据目录不可写: %s (%s)", data_dir, error)
        child_env[ENV_DATA_DIR] = data_dir

        return cls(
            command=resolved_command,
            env=child_env,
            cwd=str(source.get("HARDWARE_AGENT_MCP_CWD") or "").strip() or None,
            startup_timeout=_positive_float(
                source.get("HARDWARE_AGENT_MCP_STARTUP_TIMEOUT"), default=5.0
            ),
            call_timeout=max(
                20.0,
                _positive_float(
                    source.get("HARDWARE_AGENT_MCP_CALL_TIMEOUT"),
                    default=http_timeout + 5.0,
                ),
            ),
            stderr_log_path=str(
                source.get("HARDWARE_AGENT_MCP_STDERR_LOG") or ""
            ).strip()
            or None,
        )


def _raise_protocol_error(error: Mapping[str, Any]) -> None:
    """把 JSON-RPC ``error`` 对象转成带业务错误码的异常。"""
    code = error.get("code")
    message = error.get("message") or "未知错误"
    data = error.get("data")
    error_code: Optional[str] = None
    if isinstance(data, Mapping):
        raw = data.get("error_code")
        if raw:
            error_code = str(raw).strip()
    raise McpProtocolError(
        f"硬件插件返回错误({code}): {message}",
        code=code if isinstance(code, int) else None,
        error_code=error_code,
    )


def _error_from_body(text: str) -> Optional[Mapping[str, Any]]:
    """HTTP 错误体里如果带 JSON-RPC error，就把它当协议层错误处理。"""
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"]
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("error"), dict):
                return item["error"]
    return None


def _redact_url(url: Optional[str]) -> Optional[str]:
    """日志里只留 scheme/host/path，去掉 query（可能带 token）。"""
    if not url:
        return None
    base, _, _query = url.partition("?")
    return base


def _resolve_command(command: Any) -> Tuple[str, ...]:
    if command is None:
        return ()
    if isinstance(command, str):
        parts = tuple(shlex.split(command))
    else:
        parts = tuple(str(part) for part in command)
    if not parts:
        return ()
    # 开发期直接用 mock 脚本时自动补上解释器。
    if parts[0].endswith(".py"):
        return (sys.executable, *parts)
    return parts


def _positive_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


@dataclass
class ToolOutcome:
    """工具调用结果的归一化视图（业务层，不是 JSON-RPC 层）。"""

    name: str
    status: str
    ok: bool
    payload: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    message: Optional[str] = None
    raw_text: str = ""
    is_error: bool = False

    @property
    def duplicate(self) -> bool:
        return bool(self.payload.get("duplicate"))

    @property
    def pending(self) -> bool:
        """设备还没确认结果（queued/executing/pending/timed_out 或 pending=true）。"""
        if self.ok:
            return False
        return bool(self.payload.get("pending")) or self.status in INCOMPLETE_STATUSES

    def to_model_content(self) -> str:
        """回填给模型的内容，保持插件原始文本。"""
        return self.raw_text or json.dumps(
            {"status": self.status, "message": self.message or ""},
            ensure_ascii=False,
        )


def parse_tool_result(name: str, result: Mapping[str, Any]) -> ToolOutcome:
    """解析 ``tools/call`` 的 ``result``。

    文档第 5 节：业务失败时 ``result.isError`` 为 true，文本里仍然带
    ``error_code`` 和 ``message``，所以必须两层都看。
    """
    is_error = bool(result.get("isError"))
    text = _first_text_content(result.get("content"))
    payload: Dict[str, Any] = {}
    if text:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed

    status = str(payload.get("status") or "").strip().lower()
    if not status:
        status = "failed" if is_error else "unknown"
    ok = (not is_error) and status == "succeeded"
    error_code = payload.get("error_code")
    message = payload.get("message") or payload.get("error")
    return ToolOutcome(
        name=name,
        status=status,
        ok=ok,
        payload=payload,
        error_code=str(error_code).strip() if error_code else None,
        message=str(message).strip() if message else None,
        raw_text=text,
        is_error=is_error,
    )


def _first_text_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            value = item.get("text")
            if isinstance(value, str):
                return value
    return ""


def to_openai_tools(
    tools: Sequence[Mapping[str, Any]],
    exclude: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """把 MCP 工具清单转成 OpenAI function calling 的 tools 参数。

    文档第 2.2 节：必须原样使用插件返回的 name / description / inputSchema，
    不在宿主侧维护第二份能力表。

    ``exclude`` 用于把管理员类工具（注册/注销设备）从模型可见清单里摘掉。
    插件方已确认 ``tools/list`` 仍会返回这两个工具、且插件不会代宿主拒绝调用，
    所以在送模型前过滤是宿主自己的责任。
    """
    blocked = {str(name) for name in (exclude or ())}
    converted: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if name in blocked:
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, Mapping):
            schema = {"type": "object", "properties": {}}
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": dict(schema),
                },
            }
        )
    return converted


class McpStdioClient:
    """MCP 客户端：``config.url`` 有值走远端 HTTP，否则走本地子进程 stdio。

    会话长驻；连接断开后在下次调用前重建并重跑握手。
    """

    def __init__(
        self,
        config: HardwareAgentConfig,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._logger = logger or LOGGER
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._pending: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._next_request_id = 0
        self._proc: Optional[subprocess.Popen] = None
        self._tools: List[Dict[str, Any]] = []
        self._server_info: Dict[str, Any] = {}
        self._negotiated_version: str = ""
        self._stderr_tail: List[str] = []
        self._closing = False
        self._generation = 0
        self._session_id: str = ""
        self._ready = False
        self._http_log: List[str] = []

    @property
    def transport(self) -> str:
        return self.config.transport

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """建立连接并完成 initialize / notifications/initialized / tools/list。

        stdio 模式握手期间不能持有 ``_lock``：等待响应的是本线程，投递响应
        的是 stdout 读取线程，持锁等待会直接死锁。
        """
        with self._start_lock:
            with self._lock:
                self._ready = False
            if self.config.url:
                with self._lock:
                    # 重连必须换新会话，旧 session id 服务端可能已经回收。
                    self._session_id = ""
                self._handshake()
                return
            self._stop_process()
            self._spawn()
            self._handshake()

    def ensure_started(self) -> None:
        with self._lock:
            if self._is_alive():
                return
        self._logger.info("硬件插件连接不可用，按需重连 (%s)", self.transport)
        self.start()

    def close(self) -> None:
        if self.config.url:
            self._close_http_session()
            return
        with self._lock:
            self._closing = True
            self._stop_process()
            self._closing = False

    def __enter__(self) -> "McpStdioClient":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return list(self._tools)

    @property
    def server_info(self) -> Dict[str, Any]:
        return dict(self._server_info)

    @property
    def negotiated_version(self) -> str:
        return self._negotiated_version

    @property
    def stderr_tail(self) -> List[str]:
        with self._lock:
            return list(self._stderr_tail) + list(self._http_log)

    # ------------------------------------------------------------------
    # MCP 方法
    # ------------------------------------------------------------------
    def ping(self, timeout: Optional[float] = None) -> None:
        """MCP 层探活：插件返回空 result 即通路正常（替代 HTTP /health）。"""
        self.ensure_started()
        self._request(
            "ping", {}, timeout=timeout or min(5.0, self.config.call_timeout)
        )

    def list_tools(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if not refresh and self._tools:
            return list(self._tools)
        self.ensure_started()
        return self._fetch_tools()

    def _fetch_tools(self) -> List[Dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpProtocolError("tools/list 返回的 tools 不是数组")
        with self._lock:
            self._tools = [dict(item) for item in tools if isinstance(item, dict)]
            return list(self._tools)

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> ToolOutcome:
        self.ensure_started()
        params = {"name": name, "arguments": dict(arguments)}
        try:
            result = self._request("tools/call", params, timeout=timeout)
        except McpProcessError:
            # 进程在调用途中死掉：查询类允许换新进程重试一次，
            # 写操作不在这里重试（调用方可能已经承担副作用语义）。
            if name not in QUERY_TOOL_NAMES:
                raise
            self._logger.info("查询工具 %s 因进程退出失败，重启后重试一次", name)
            self.start()
            result = self._request("tools/call", params, timeout=timeout)
        return parse_tool_result(name, result)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _spawn(self) -> None:
        if not self.config.command:
            raise McpProcessError("硬件插件启动命令为空")
        env = dict(os.environ)
        env.update({key: str(value) for key, value in self.config.env.items()})
        try:
            proc = subprocess.Popen(
                list(self.config.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.config.cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as error:
            raise McpProcessError(f"无法启动硬件插件进程: {error}") from error

        with self._lock:
            generation = self._generation
            self._proc = proc
            self._stderr_tail = []
        self._logger.info(
            "硬件插件进程已启动 pid=%s generation=%s", proc.pid, generation
        )
        self._start_reader(proc, generation)

    def _start_reader(self, proc: subprocess.Popen, generation: int) -> None:
        threading.Thread(
            target=self._read_stdout,
            args=(proc, generation),
            name=f"mcp-stdout-{generation}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(proc, generation),
            name=f"mcp-stderr-{generation}",
            daemon=True,
        ).start()

    def _read_stdout(self, proc: subprocess.Popen, generation: int) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                if generation != self._generation:
                    return
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._logger.warning(
                        "硬件插件 stdout 出现非 JSON 行: %s", line[:200]
                    )
                    continue
                if not isinstance(message, dict):
                    self._logger.warning("硬件插件 stdout 出现非对象消息")
                    continue
                self._dispatch(message)
        except (ValueError, OSError) as error:
            self._logger.debug("stdout 读取结束: %s", error)
        finally:
            self._on_process_gone(proc, generation)

    def _read_stderr(self, proc: subprocess.Popen, generation: int) -> None:
        stream = proc.stderr
        if stream is None:
            return
        path = self.config.stderr_log_path
        try:
            for raw_line in stream:
                if generation != self._generation:
                    return
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                self._logger.debug("硬件插件 stderr: %s", line)
                with self._lock:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 40:
                        del self._stderr_tail[: len(self._stderr_tail) - 40]
                if path:
                    self._append_stderr_file(path, line)
        except (ValueError, OSError) as error:
            self._logger.debug("stderr 读取结束: %s", error)

    def _append_stderr_file(self, path: str, line: str) -> None:
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError as error:
            self._logger.debug("写入插件 stderr 日志失败: %s", error)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is None:
            # 服务端主动通知，当前协议没有需要处理的场景。
            self._logger.debug("硬件插件通知: %s", message.get("method"))
            return
        with self._lock:
            entry = self._pending.pop(message_id, None)
        if entry is None:
            self._logger.warning("收到未知 id 的响应: %s", message_id)
            return
        event, box = entry
        box["message"] = message
        event.set()

    def _handshake(self) -> None:
        deadline = self.config.startup_timeout
        result = self._request(
            "initialize",
            {
                "protocolVersion": self.config.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "desktop-pet", "version": "0.1.0"},
            },
            timeout=deadline,
        )
        with self._lock:
            self._server_info = dict(result.get("serverInfo") or {})
            self._negotiated_version = str(result.get("protocolVersion") or "")
        self._notify("notifications/initialized", {})
        tools = self._fetch_tools()
        with self._lock:
            self._ready = True
        self._logger.info(
            "硬件插件已就绪 transport=%s server=%s tools=%d",
            self.transport,
            self._server_info.get("name") or "unknown",
            len(tools),
        )

    def _request(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if self.config.url:
            return self._http_rpc(method, params, timeout=timeout)

        request_id = self._allocate_id()
        event = threading.Event()
        box: Dict[str, Any] = {}
        with self._lock:
            self._pending[request_id] = (event, box)
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = dict(params)
        sent = self._write_line(payload)
        if not sent:
            with self._lock:
                self._pending.pop(request_id, None)
            raise McpProcessError(f"写入硬件插件失败: {method}")

        wait_seconds = timeout or self.config.call_timeout
        if not event.wait(wait_seconds):
            with self._lock:
                self._pending.pop(request_id, None)
            raise McpCallTimeout(
                f"硬件插件调用超时({wait_seconds}s): {method}", sent=True
            )

        response = box.get("message") or {}
        return self._unwrap_response(method, response)

    def _unwrap_response(
        self, method: str, response: Mapping[str, Any]
    ) -> Dict[str, Any]:
        if "error" in response:
            _raise_protocol_error(response.get("error") or {})
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(f"硬件插件返回结果格式不正确: {method}")
        return dict(result)

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        if self.config.url:
            self._http_post(
                payload, timeout=self.config.startup_timeout, want_response=False
            )
            return
        if not self._write_line(payload):
            raise McpProcessError(f"写入通知失败: {method}")

    def _write_line(self, payload: Mapping[str, Any]) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        data = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(data + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as error:
                self._logger.warning("写入硬件插件 stdin 失败: %s", error)
                return False
        return True

    def _allocate_id(self) -> int:
        with self._lock:
            self._next_request_id += 1
            return self._next_request_id

    # ------------------------------------------------------------------
    # 远端传输（MCP Streamable HTTP）
    # ------------------------------------------------------------------
    def _http_rpc(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._allocate_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = dict(params)
        response = self._http_post(
            payload, timeout=timeout or self.config.call_timeout, want_response=True
        )
        return self._unwrap_response(method, response or {})

    def _http_post(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        want_response: bool,
    ) -> Dict[str, Any]:
        url = self.config.url or ""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        session = self._session_id
        if session:
            request.add_header("Mcp-Session-Id", session)
        if self.config.token:
            request.add_header(
                self.config.token_header,
                f"{self.config.token_prefix}{self.config.token}",
            )

        # 内网/本机插件必须绕过 HTTP_PROXY，否则请求会被代理劫持成 502。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        method = str(payload.get("method") or "")
        try:
            with opener.open(request, timeout=timeout) as response:
                session_id = (response.headers.get("Mcp-Session-Id") or "").strip()
                if session_id:
                    with self._lock:
                        self._session_id = session_id
                content_type = (response.headers.get("Content-Type") or "").lower()
                status = int(getattr(response, "status", 200) or 200)
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = self._read_error_body(error)
            self._record_http_error(f"HTTP {error.code} {method}: {detail}")
            rpc_error = _error_from_body(detail)
            if rpc_error is not None:
                _raise_protocol_error(rpc_error)
            if error.code in (401, 403):
                raise McpProcessError(
                    f"硬件插件拒绝访问(HTTP {error.code})，请检查 "
                    f"{ENV_MCP_TOKEN} 与 {ENV_MCP_TOKEN_HEADER}: {detail}"
                ) from error
            raise McpProcessError(
                f"硬件插件接口返回 HTTP {error.code}: {detail}"
            ) from error
        except (TimeoutError, socket.timeout) as error:
            self._record_http_error(f"超时 {timeout}s {method}")
            raise McpCallTimeout(
                f"硬件插件调用超时({timeout}s): {method}", sent=True
            ) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                self._record_http_error(f"超时 {timeout}s {method}")
                raise McpCallTimeout(
                    f"硬件插件调用超时({timeout}s): {method}", sent=True
                ) from error
            self._record_http_error(f"连接失败 {url}: {reason}")
            raise McpProcessError(f"连接硬件插件失败({url}): {reason}") from error
        except OSError as error:
            self._record_http_error(f"网络异常 {method}: {error}")
            raise McpProcessError(f"调用硬件插件失败({url}): {error}") from error

        if status in (202, 204) or not body.strip():
            return {}
        if "text/event-stream" in content_type:
            return self._parse_sse(body, payload.get("id"))
        try:
            message = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as error:
            self._record_http_error(f"响应不是 JSON {method}: {body[:200]!r}")
            raise McpProtocolError(
                f"硬件插件响应不是合法 JSON: {body[:200]!r}"
            ) from error
        if isinstance(message, list):
            # 批量响应：取 id 匹配的那条，否则取最后一条。
            target = payload.get("id")
            for item in message:
                if isinstance(item, dict) and item.get("id") == target:
                    return item
            message = message[-1] if message else {}
        if not isinstance(message, dict):
            raise McpProtocolError(f"硬件插件响应格式不正确: {method}")
        if want_response and "result" not in message and "error" not in message:
            raise McpProtocolError(f"硬件插件响应缺少 result: {method}")
        return message

    def _parse_sse(self, body: bytes, request_id: Any) -> Dict[str, Any]:
        """解析 SSE 流，取 id 与请求匹配的 JSON-RPC 响应。"""
        events: List[str] = []
        buffer: List[str] = []
        for raw_line in body.decode("utf-8", errors="replace").splitlines():
            if not raw_line.strip():
                if buffer:
                    events.append("\n".join(buffer))
                    buffer = []
                continue
            if raw_line.startswith(":"):
                continue
            if raw_line.startswith("data:"):
                buffer.append(raw_line[5:].lstrip())
        if buffer:
            events.append("\n".join(buffer))

        fallback: Optional[Dict[str, Any]] = None
        for chunk in events:
            if chunk.strip() in {"", "[DONE]"}:
                continue
            try:
                message = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                return message
            if "result" in message or "error" in message:
                fallback = message
        if fallback is not None:
            return fallback
        self._record_http_error("SSE 流中没有匹配的响应")
        raise McpProtocolError("硬件插件 SSE 流里没有返回 JSON-RPC 响应")

    def _read_error_body(self, error: urllib.error.HTTPError) -> str:
        try:
            raw = error.read()
        except Exception:  # noqa: BLE001 - 读错误体失败不该盖住原始错误
            return error.reason or "无响应体"
        text = raw.decode("utf-8", errors="replace").strip()
        return text[:400] or str(error.reason or "无响应体")

    def _record_http_error(self, message: str) -> None:
        with self._lock:
            self._http_log.append(message)
            del self._http_log[:-20]

    def _close_http_session(self) -> None:
        with self._lock:
            session = self._session_id
            self._session_id = ""
            self._ready = False
        if not session or not self.config.url:
            return
        request = urllib.request.Request(self.config.url, method="DELETE")
        request.add_header("Mcp-Session-Id", session)
        if self.config.token:
            request.add_header(
                self.config.token_header,
                f"{self.config.token_prefix}{self.config.token}",
            )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=2.0):
                pass
        except Exception:  # noqa: BLE001 - 关闭会话是 best-effort
            pass

    def _is_alive(self) -> bool:
        if self.config.url:
            return self._ready
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _on_process_gone(self, proc: subprocess.Popen, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._proc = None
            pending = list(self._pending.values())
            self._pending.clear()
        for event, box in pending:
            box["message"] = {
                "error": {"code": -32000, "message": "硬件插件进程已退出"}
            }
            event.set()
        _reap(proc)
        if not self._closing:
            self._logger.warning("硬件插件进程已退出")

    def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None
        with self._lock:
            self._generation += 1
            pending = list(self._pending.values())
            self._pending.clear()
        for event, box in pending:
            box["message"] = {
                "error": {"code": -32000, "message": "硬件插件连接已关闭"}
            }
            event.set()
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        _reap(proc)


def _reap(proc: subprocess.Popen) -> None:
    """回收管道与僵尸进程，避免反复重启插件时泄漏 fd。"""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
