"""把桌宠的一轮对话转发到自建 sw-bot（bot.vx123.xin）上的某个 Bot。

sw-bot 的协议（从 web 前端与 shared 包读出来的）：
- `POST /api/auth/login` 用邮箱密码换 cookie，之后所有 `/api/*` 都靠这个 cookie；
- `GET /api/bots` 列出 Bot，名字在 `name`；
- `POST /api/bots/{id}/messages` 发一条消息，`text` + `fileIds` + `requestId`，
  正常返回 `text/event-stream`，事件见 `CHAT_EVENT_TYPES`：
  `delta`/`thinking`/`reset`/`tool`/`file`/`done`/`error`，
  其中 `done.text` 是这一轮最终提交的完整回复；
- 非流式时返回 `{"delivery": "accepted", ...}`，回复要再靠 `GET /api/bots/{id}/messages` 轮询。

本模块只做网络收发，不关心语音：上层拿到文本后自己合成 TTS。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

DEFAULT_BASE_URL = "https://bot.vx123.xin"
DEFAULT_COOKIE_FILE = Path.home() / ".sparkbot" / "swbot_cookies.txt"

# 站点 WAF 会拦掉非浏览器 UA（表现为登录 403 FORBIDDEN），这里伪装成 Chrome。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# 一句话的语音输入，Bot 默认会当成「写代码/查资料」来答，
# 出来的长文和 markdown 没法朗读。默认加一句口语约束，可用环境变量关掉。
DEFAULT_VOICE_PREFIX = "（语音输入，请用口语、一两句话回答，不要用 markdown）"


class SwBotError(RuntimeError):
    """sw-bot 请求失败（登录过期、Bot 忙、流中断等）。"""


@dataclass
class SwBotBot:
    id: str
    name: str
    status: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SwBotBot":
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            status=str(payload.get("status") or ""),
        )


def spoken_text(text: str) -> str:
    """把回复压成能朗读的纯文本。

    Bot 的回复通常带 markdown、代码块、链接，直接丢给 TTS 会念出一堆星号和反引号。
    """
    value = str(text or "")
    if not value.strip():
        return ""
    value = re.sub(r"```.*?```", "（代码略）", value, flags=re.S)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"^#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"^\s*[-*+]\s+", "", value, flags=re.M)
    value = re.sub(r"^\s*\d+[.、]\s+", "", value, flags=re.M)
    value = value.replace("**", "").replace("__", "").replace("~~", "")
    value = re.sub(r"\n{2,}", "\n", value).strip()
    return value


class SwBotClient:
    """带登录态的 sw-bot 客户端（cookie 落盘，重启不用重新登录）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        email: Optional[str] = None,
        password: Optional[str] = None,
        bot: Optional[str] = None,
        cookie_file: Optional[Path] = None,
        timeout_seconds: float = 120.0,
        voice_prefix: str = DEFAULT_VOICE_PREFIX,
        logger: Any = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.email = email or os.environ.get("SWBOT_EMAIL")
        self.password = password or os.environ.get("SWBOT_PASSWORD")
        self.bot_hint = bot or os.environ.get("SWBOT_BOT") or ""
        self.cookie_file = Path(cookie_file or os.environ.get("SWBOT_COOKIE_FILE") or DEFAULT_COOKIE_FILE)
        self.timeout_seconds = timeout_seconds
        self.voice_prefix = voice_prefix
        self.logger = logger
        self._lock = threading.RLock()
        self._bot_id: Optional[str] = None
        self._opener: Optional[urllib.request.OpenerDirector] = None

    # ---------------- 基础设施 ----------------

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            self.logger.info(message, *args)

    def _build_opener(self) -> urllib.request.OpenerDirector:
        jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        # 本机环境有 HTTP_PROXY，且站点可能是自签证书：一律直连、不校验证书。
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=context),
        )

    @property
    def opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            self._opener = self._build_opener()
        return self._opener

    def _save_cookies(self) -> None:
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            for handler in self.opener.handlers:
                if isinstance(handler, urllib.request.HTTPCookieProcessor):
                    handler.cookiejar.save(ignore_discard=True, ignore_expires=True)
                    return
        except OSError:
            pass

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        accept: str = "application/json",
    ) -> urllib.request.Request:
        url = self.base_url + path
        body = None
        # 站点前面有一层 WAF：只认 Origin/Referer + 浏览器 UA。
        # 少了这三个头，连正确密码的登录也会被拒成
        # 403 {"code":"FORBIDDEN","message":"当前操作不可用，请联系管理员。"}
        headers = {
            "Accept": accept,
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(url, data=body, headers=headers, method=method)

    def _open(self, request: urllib.request.Request) -> Any:
        return self.opener.open(request, timeout=self.timeout_seconds)

    # ---------------- 登录 ----------------

    def login(self) -> None:
        if not self.email or not self.password:
            raise SwBotError("缺少 sw-bot 账号：请配置 SWBOT_EMAIL / SWBOT_PASSWORD")
        request = self._request(
            "POST",
            "/api/auth/login",
            {"email": self.email, "password": self.password},
        )
        try:
            with self._open(request) as response:
                json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            raise SwBotError(f"sw-bot 登录失败（HTTP {error.code}）：{detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise SwBotError(f"sw-bot 登录请求失败：{error}") from error
        self._save_cookies()
        self._log("sw-bot 登录成功：%s", self.email)

    def ensure_session(self) -> None:
        """有 cookie 就复用，401 再登一次。"""
        try:
            with self._open(self._request("GET", "/api/auth/me", timeout=15)) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            if payload.get("user") or payload.get("email") or payload.get("name"):
                return
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise SwBotError(f"sw-bot 会话检查失败（HTTP {error.code}）") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        self.login()

    # ---------------- Bot ----------------

    def list_bots(self) -> List[SwBotBot]:
        self.ensure_session()
        try:
            with self._open(self._request("GET", "/api/bots")) as response:
                payload = json.loads(response.read().decode("utf-8") or "[]")
        except urllib.error.HTTPError as error:
            raise SwBotError(f"读取 Bot 列表失败（HTTP {error.code}）") from error
        items = payload.get("bots") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        return [SwBotBot.from_payload(item) for item in items if isinstance(item, dict)]

    def resolve_bot_id(self, hint: Optional[str] = None) -> str:
        """按名字或 id 找 Bot；不给提示就用配置里的，再不行取第一个。"""
        with self._lock:
            if self._bot_id and hint in (None, "", self.bot_hint):
                return self._bot_id
            wanted = (hint or self.bot_hint or "").strip()
            bots = self.list_bots()
            if not bots:
                raise SwBotError("sw-bot 账号下没有可用的 Bot")
            picked = None
            if wanted:
                for item in bots:
                    if item.id == wanted or item.name == wanted:
                        picked = item
                        break
            if picked is None:
                picked = bots[0]
                if wanted:
                    self._log("sw-bot 没找到「%s」，改用「%s」", wanted, picked.name)
            self._bot_id = picked.id
            self._log("sw-bot 使用 Bot：%s (%s)", picked.name, picked.id)
            return picked.id

    # ---------------- 消息 ----------------

    def send(
        self,
        text: str,
        bot_id: Optional[str] = None,
        *,
        raw: bool = False,
        conversation_id: Optional[str] = None,
    ) -> str:
        """发一条消息，返回最终回复文本。

        ``raw=True`` 时不加「口语回答」前缀 —— 意图路由要的是严格 JSON，
        前缀里那句「用口语回答」会把模型带偏。
        ``conversation_id`` 是给直连模型客户端准备的会话记忆键；sw-bot 自己
        维护会话，这里只为了两个客户端形状一致，收到就忽略。
        """
        _ = conversation_id
        target = bot_id or self.resolve_bot_id()
        message = text
        if not raw and self.voice_prefix:
            message = f"{self.voice_prefix}\n{text}"
        payload = {
            "text": message,
            "fileIds": [],
            "requestId": uuid.uuid4().hex,
        }
        request = self._request(
            "POST",
            f"/api/bots/{urllib.parse.quote(target)}/messages",
            payload,
            accept="text/event-stream",
        )
        try:
            response = self._open(request)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            if error.code == 401:
                self.login()
                raise SwBotError("sw-bot 会话过期，已重新登录，请再说一次") from error
            if error.code == 409:
                raise SwBotError("Bot 正在回复上一条消息，请稍后") from error
            raise SwBotError(f"发送失败（HTTP {error.code}）：{detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise SwBotError(f"连不上 sw-bot：{error}") from error

        with response:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                return self._reply_from_history(target)
            reply = self._consume_events(response)
        return reply

    def _consume_events(self, response: Any) -> str:
        buffer = ""
        committed = ""
        pieces: Dict[str, str] = {}
        dropped: set[str] = set()
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            chunk = response.readline()
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                data = "\n".join(
                    line[5:].strip()
                    for line in frame.split("\n")
                    if line.startswith("data:")
                )
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                attempt = str(event.get("attempt") or "")
                if kind == "delta":
                    pieces[attempt] = pieces.get(attempt, "") + str(event.get("text") or "")
                elif kind == "reset":
                    dropped.add(attempt)
                elif kind == "done":
                    committed = str(event.get("text") or "")
                    return committed
                elif kind == "error":
                    raise SwBotError(f"Bot 回复失败：{event.get('message') or '未知错误'}")
        if committed:
            return committed
        streamed = "".join(text for attempt, text in pieces.items() if attempt not in dropped)
        if streamed.strip():
            return streamed
        raise SwBotError("sw-bot 的回复流没有返回内容")

    def _reply_from_history(self, bot_id: str) -> str:
        """非流式受理时，轮询历史消息等到这条回复落地。"""
        deadline = time.monotonic() + self.timeout_seconds
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                with self._open(
                    self._request("GET", f"/api/bots/{urllib.parse.quote(bot_id)}/messages")
                ) as response:
                    payload = json.loads(response.read().decode("utf-8") or "[]")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                continue
            items = payload.get("messages") if isinstance(payload, dict) else payload
            if not isinstance(items, list) or not items:
                continue
            for item in reversed(items):
                if not isinstance(item, dict) or item.get("role") != "assistant":
                    continue
                if item.get("status") == "streaming":
                    break
                content = str(item.get("content") or "").strip()
                if content:
                    return content
                break
        raise SwBotError(f"等待 sw-bot 回复超时：{last_error or '没有新消息'}")


class SwBotAgent:
    """和 PetAgent 同形状的适配器，直接塞给 PortableRequestService(agent=...)。"""

    def __init__(self, client: SwBotClient) -> None:
        self.client = client

    def handle_turn(self, conversation_id: str, user_text: str, request_id: str) -> Any:
        from agent_gateway.pet_agent import AgentTurn

        # conversation_id 必须传下去：不传的话每一句都是一次全新的请求，
        # 用户刚说完「叫麦德」，下一句问名字它就答「你还没给我起名字」。
        text = spoken_text(
            self.client.send(user_text, conversation_id=conversation_id)
        )
        if not text:
            text = "我这边没有收到回复。"
        return AgentTurn(text=text)


class SwBotIntentClassifier:
    """让 sw-bot 承担中枢第③步：一次调用同时给出意图和要念的回复。

    只发一次请求 —— 先问「是什么意图」再问「怎么回答」会让设备每轮多等
    一次模型首字，实测体感从「有点慢」变成「明显卡」。
    """

    def __init__(self, client: SwBotClient, *, default_intent: str = "chat") -> None:
        self.client = client
        self.default_intent = default_intent

    def classify(
        self,
        text: str,
        catalog: str,
        device_context: str = "",
        conversation_id: Optional[str] = None,
    ) -> Optional[Any]:
        from agent_gateway.intent import build_intent_prompt, parse_intent_reply

        # chat 的回复就是这一次调用产出的，所以上下文必须在这里就带上；
        # 只给 agent 加历史没用 —— 那条路在意图判定为 chat 后根本不会被走到。
        history = ""
        reader = getattr(self.client, "history_text", None)
        if conversation_id and callable(reader):
            history = reader(conversation_id)
        prompt = build_intent_prompt(
            text, catalog, device_context=device_context, history=history
        )
        raw = self.client.send(prompt, raw=True)
        parsed = parse_intent_reply(
            raw, default_intent=self.default_intent, source="swbot"
        )
        # 这条路径绕过了 agent，历史得自己补记，否则下一句照样失忆。
        recorder = getattr(self.client, "remember", None)
        if conversation_id and parsed is not None and callable(recorder):
            recorder(conversation_id, text, parsed.reply or "")
        return parsed

    def close(self) -> None:
        return None


def build_client_from_env(environ: Optional[Dict[str, str]] = None) -> Optional[SwBotClient]:
    source = dict(os.environ if environ is None else environ)
    base_url = (source.get("SWBOT_BASE_URL") or "").strip()
    email = (source.get("SWBOT_EMAIL") or "").strip()
    if not base_url or not email:
        return None
    prefix = source.get("SWBOT_VOICE_PREFIX")
    return SwBotClient(
        base_url=base_url,
        email=email,
        password=(source.get("SWBOT_PASSWORD") or "").strip() or None,
        bot=(source.get("SWBOT_BOT") or "").strip() or None,
        voice_prefix=DEFAULT_VOICE_PREFIX if prefix is None else prefix,
    )
