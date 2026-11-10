"""意图路由层：决定「这句话想干什么」，再把活派给对应插件。

改造前，第③步「判定意图」写死在 ``portable.py`` 的关键词里，只有开发者改代码
才能新增一种能力。现在拆成三层：

1. **快通道（本地规则）**：唤醒词、退下吧、噪声残片、播放中的暂停/继续。
   这些必须 0 延迟且断网可用 —— 交给大模型会出现「喊不醒」和「暂停要等两秒」。
2. **主通道（分类器）**：sw-bot 或模型一次调用，同时给出 ``intent`` + ``slots``
   + 已经写好的 ``reply``。注意只调一次，不要「先问意图再问答案」，
   否则设备每轮要多等一次首字。
3. **插件分发**：intent 命中哪个插件就由它决定 ``session_action`` /
   ``device_action`` / 要不要合成语音。

分类器不可用、超时、返回的不是 JSON 时，路由器返回 ``None``，调用方退回
改造前的关键词分支，行为与以前完全一致。
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .chat_llm import ChatLLMError

DEFAULT_SESSION_ACTION = "continue"
DEFAULT_FALLBACK_INTENT = "chat"

# bot 常见的写法差异，统一抹平后再查表
INTENT_ALIASES: Dict[str, str] = {
    "music.play": "music_play",
    "music.pause": "music_pause",
    "music.resume": "music_resume",
    "music.replay": "music_replay",
    "music.next": "music_next",
    "music.stop": "music_stop",
    "music.volume_up": "music_volume_up",
    "music.volume_down": "music_volume_down",
    "play_music": "music_play",
    "pause_music": "music_pause",
    "resume_music": "music_resume",
    "next_music": "music_next",
    "stop_music": "music_stop",
    "fountain.start": "fountain_start",
    "fountain.stop": "fountain_stop",
    "start_fountain": "fountain_start",
    "stop_fountain": "fountain_stop",
    # 录音豆（AgentOS 协议侧习惯用点分写法）
    "record.start": "record_start",
    "record.stop": "record_stop",
    "record.status": "record_status",
    "start_recording": "record_start",
    "stop_recording": "record_stop",
    "recording_status": "record_status",
    "开始录音": "record_start",
    "停止录音": "record_stop",
    "录音状态": "record_status",
    "闲聊": "chat",
    "聊天": "chat",
    "问答": "chat",
    "知识问答": "chat",
    "搜索": "search",
    "查询": "search",
    "点歌": "music_play",
    "暂停": "music_pause",
    "继续播放": "music_resume",
    "下一首": "music_next",
    "停止播放": "music_stop",
    "开喷泉": "fountain_start",
    "关喷泉": "fountain_stop",
    "退下": "sleep",
    "待机": "sleep",
}


@dataclass
class IntentResult:
    """一次判定的完整结果：想干什么、该说什么、设备该怎么动。"""

    intent: str
    reply: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    session_action: str = DEFAULT_SESSION_ACTION
    device_action: Optional[Dict[str, Any]] = None
    source: str = "bot"
    # True 表示插件已经自己写库（比如音乐要落库再等设备拉流），调用方别再写一遍
    handled: bool = False

    def public(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "intent": self.intent,
            "source": self.source,
            "session_action": self.session_action,
        }
        if self.slots:
            payload["slots"] = dict(self.slots)
        return payload


@dataclass
class PluginContext:
    """插件执行时能用到的所有东西，由调用方填好。

    插件不直接碰数据库和硬件，只调这里给的回调，方便单测。
    """

    text: str
    request_id: str
    device_id: str = ""
    session_mode: str = "conversation"
    session_active: bool = False
    synthesize: Optional[Callable[[str], Optional[Tuple[bytes, str, str]]]] = None
    chat: Optional[Callable[[], None]] = None
    music: Optional[Callable[[Tuple[str, str]], None]] = None
    # 设备实时状态，形如「说话音量 3/5；音乐没在放；屏幕亮」。会注入提示词，
    # 让模型回答「现在音量多少」时有数据可用，而不是现编。
    device_context: str = ""
    # 会话 id：分类器用它取前几轮对话，否则每句都是全新的一轮。
    conversation_id: str = ""


class Plugin:
    """一个意图插件的骨架：声明能处理哪些 intent，拿到结果就办事。"""

    intents: Sequence[str] = (DEFAULT_FALLBACK_INTENT,)
    description: str = ""
    slots: Mapping[str, str] = {}

    @property
    def intent(self) -> str:
        return self.intents[0]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        raise NotImplementedError

    def catalog_lines(self) -> List[str]:
        """喂给分类器的能力清单，一条 intent 一行。"""
        lines: List[str] = []
        for name in self.intents:
            line = f"- {name}：{self.description}"
            if self.slots:
                pairs = ", ".join(f'"{key}": "{value}"' for key, value in self.slots.items())
                line += f"；slots: {{{pairs}}}"
            lines.append(line)
        return lines


class IntentRouter:
    """把分类结果分发到插件；分类器缺席或失败时返回 None 让调用方兜底。"""

    def __init__(
        self,
        plugins: Sequence[Plugin],
        classifier: Optional[Any] = None,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.classifier = classifier
        self.logger = logger or logging.getLogger("agent_gateway.intent")
        # 分类器可能是外部注入的假实现，只认 (text, catalog) 两个参数。
        # 探一次签名，别让设备状态把老分类器顶崩。
        self._accepts_device_context = _accepts_kwarg(
            getattr(classifier, "classify", None), "device_context"
        )
        self._accepts_conversation = _accepts_kwarg(
            getattr(classifier, "classify", None), "conversation_id"
        )
        self._plugins: Dict[str, Plugin] = {}
        self._order: List[Plugin] = []
        for plugin in plugins:
            self._order.append(plugin)
            for name in plugin.intents:
                self._plugins[normalize_intent(name)] = plugin

    # ---------------- 对外 ----------------

    @property
    def intents(self) -> List[str]:
        return list(self._plugins)

    def catalog(self) -> str:
        lines: List[str] = []
        for plugin in self._order:
            lines.extend(plugin.catalog_lines())
        return "\n".join(lines)

    def resolve(self, text: str, ctx: PluginContext) -> Optional[IntentResult]:
        """分类 → 分发。任何一环出问题都返回 None，由调用方走旧关键词分支。"""
        if self.classifier is None or not str(text or "").strip():
            return None
        try:
            # 分类器签名是外部注入的，可能只认 (text, catalog)；逐个探过再用。
            optional = {}
            if self._accepts_device_context and ctx.device_context:
                optional["device_context"] = ctx.device_context
            if self._accepts_conversation and ctx.conversation_id:
                optional["conversation_id"] = ctx.conversation_id
            intent = self.classifier.classify(text, self.catalog(), **optional)
        except ChatLLMError:
            # 主备端点全挂了。这里绝不能返回 None —— 调用方会把它当成
            # 「分类器不灵」再走一遍 agent 路径，于是又白等一整个预算：
            # 2026-11-06 实测一句 16 秒，就是两个 8 秒预算叠起来的。
            # 端点已经判定不可用，重试没有意义，直接交给上层说人话。
            raise
        except Exception as error:  # noqa: BLE001 - 分类失败必须降级而不是报错
            self.logger.warning("意图分类失败，退回关键词规则：%s", error)
            return None
        if intent is None:
            return None
        intent.intent = normalize_intent(intent.intent)
        plugin = self._plugins.get(intent.intent) or self._plugins.get(
            DEFAULT_FALLBACK_INTENT
        )
        if plugin is None:
            return intent
        try:
            return plugin.handle(intent, ctx)
        except Exception as error:  # noqa: BLE001 - 插件炸了也要让设备有回复
            self.logger.warning("意图插件执行失败（%s）：%s", intent.intent, error)
            return IntentResult(
                intent=intent.intent,
                reply="这个操作我没做成，再说一遍试试。",
                source="plugin-error",
            )


def _accepts_kwarg(func: Any, name: str) -> bool:
    """classify 是否接受某个关键字参数（兼容只吃两个参数的旧实现）。"""
    if not callable(func):
        return False
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def normalize_intent(name: Any) -> str:
    """把 bot 可能写歪的 intent 名收敛成 snake_case。"""
    key = str(name or "").strip()
    if not key:
        return DEFAULT_FALLBACK_INTENT
    direct = INTENT_ALIASES.get(key)
    if direct:
        return direct
    lowered = key.lower().replace("-", "_").replace(".", "_").replace(" ", "_")
    while "__" in lowered:
        lowered = lowered.replace("__", "_")
    return INTENT_ALIASES.get(lowered, lowered) or DEFAULT_FALLBACK_INTENT


def _extract_json_object(text: str) -> Optional[str]:
    """从一段话里抠出第一个括号配平的 JSON 对象。

    bot 经常会写成「好的，这是结果：{...}」或者带 ``` 围栏，一律容错。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    cleaned = cleaned[3:]
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:]
    if "```" in cleaned:
        cleaned = cleaned.split("```", 1)[0]
    return cleaned.strip()


def parse_intent_reply(
    raw: str,
    *,
    default_intent: str = DEFAULT_FALLBACK_INTENT,
    source: str = "bot",
) -> Optional[IntentResult]:
    """把 bot 的原始回复解析成 IntentResult。

    解析不出 JSON 不算失败：整段话就当 chat 的回答，至少能念给用户听。
    """
    cleaned = _strip_fences(str(raw or ""))
    if not cleaned:
        return None
    candidate = _extract_json_object(cleaned)
    data: Optional[Dict[str, Any]] = None
    if candidate:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    if data is None:
        return IntentResult(
            intent=normalize_intent(default_intent),
            reply=cleaned,
            source=f"{source}-text",
        )

    intent_name = data.get("intent") or data.get("action") or data.get("intent_name")
    reply = data.get("reply") or data.get("answer") or data.get("say") or ""
    slots = data.get("slots") or data.get("params") or {}
    if not isinstance(slots, dict):
        slots = {}
    return IntentResult(
        intent=normalize_intent(intent_name or default_intent),
        reply=str(reply).strip(),
        slots=dict(slots),
        source=source,
    )


def build_intent_prompt(
    text: str,
    catalog: str,
    device_context: str = "",
    history: str = "",
    identity: Optional[str] = None,
) -> str:
    """给分类模型的任务书：一次调用同时要 intent 和最终口语回复。

    ``device_context`` 是设备的实时状态（音量、屏幕、喷泉…）。放在独立段落
    并明确标注「不是 intent 候选」，避免模型把它当成可选项。
    ``history`` 是这次对话前几轮的纯文本摘要。用纯文本而不是 messages 数组，
    是为了让输出仍然只有一个 JSON —— 混进 role 历史后模型很容易跟着聊天。
    ``identity`` 是桌宠当前的名字（见 ``pet_identity``）。传 ``None`` 表示
    现取一遍；传空串表示不要这段（测试用）。
    """
    if identity is None:
        try:
            from agent_gateway.pet_identity import identity_line

            identity = identity_line()
        except Exception:  # noqa: BLE001 - 身份读不到不影响出意图
            identity = ""
    identity_block = ""
    if identity.strip():
        identity_block = f"【你的身份】{identity.strip()}\n\n"
    state_block = ""
    if device_context.strip():
        state_block = (
            "【设备实时状态】以下是从设备读到的真实数据，只用于回答用户问题，"
            "不是 intent 候选，也不要把它念出来：\n"
            f"{device_context.strip()}\n"
            "用户问到这些数值（比如音量多少）必须照实回答，查不到就说查不到，"
            "绝对不要猜。\n\n"
        )
    history_block = ""
    if history.strip():
        history_block = (
            "【本次对话的前几轮】只用来理解「它 / 刚才 / 那个」指什么，"
            "不要复述这段内容，也不要因为这段就改变输出格式：\n"
            f"{history.strip()}\n\n"
        )
    return (
        "你是桌宠的意图路由器。只输出一行 JSON，不要解释、不要代码块、不要前后缀。\n"
        '格式：{"intent":"...","slots":{},"reply":"..."}\n'
        "intent 只能取下面这些值：\n"
        f"{catalog}\n"
        "要求：\n"
        "1. 闲聊、知识问答、出主意 → chat，reply 直接给答案；\n"
        "2. 要查实时信息（天气、新闻、价格、比分）→ search，reply 直接给结论；\n"
        "   没有实时数据时，就说「我这边看不到实时数据」，"
        "绝对不要说「我这就去查」「稍等一下」这种空承诺，"
        "也绝对不要编数字（气温、价格、比分一律不许现编）；\n"
        "3. 要操作设备 → 用对应动作 intent，reply 只说一句确认，不要解释原理；\n"
        "4. 问设备自身状态（音量、屏幕、有没有在放歌）→ device_query；"
        "要改设置（调音量）→ device_set，slots 写 capability 和 value/delta；\n"
        "5. reply 是能直接朗读的中文口语，不带 markdown、不带括号注释，不超过 40 字；\n"
        "6. 用户这句话是语音转文字来的，常常答非所问、词序乱掉、或者根本不成句"
        "（例如「那你他们呢？那你来听。」）。遇到这种听着像识别错了的句子，"
        "reply 只回一句「你刚说什么？我没听清」，intent 仍给 chat；"
        "**绝对不要**用「我听着呢，你说吧」这种敷衍话硬接，那会让用户以为你听懂了。\n\n"
        f"{identity_block}"
        f"{history_block}"
        f"{state_block}"
        f"用户说：{text}"
    )
