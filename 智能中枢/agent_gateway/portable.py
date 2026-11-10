from __future__ import annotations

import base64
import io
import math
import os
import re
import struct
import threading
import time
import uuid
import wave
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Tuple

from hub_core.errors import ConflictError, HubError, ValidationError
from hub_core.service import HubService

from .intent import IntentResult, IntentRouter, Plugin, PluginContext
from . import host_control
from .actions import build_dispatcher_from_env
from .chat_llm import ChatLLMError
from .device import DEFAULT_VOLUME_STEP, VOLUME_GAINS, DeviceRegistry, clamp_volume_step
from .peripherals import RecordingBeanPlugin
from .qwen_asr import QwenAsrClient
from .music import MusicError, MusicService
from .rk3588 import RK3588AgentClient
from .service import AgentGatewayError, TextAgent
from .tts import RK3588TtsClient
from .wake_audio import prepare_wake_wav, wav_peak


class HardwarePlanProvider(Protocol):
    def plan_hub_command(
        self,
        request_id: str,
        conversation_id: str,
        input_text: str,
        target_device_id: str,
        source: str,
    ) -> Dict[str, Any]:
        ...

    def dispatch_hub_command(
        self,
        request_id: str,
        conversation_id: str,
        plan_id: str,
        target_device_id: str,
        source: str,
    ) -> Dict[str, Any]:
        ...


class AudioTranscriptionProvider(Protocol):
    def transcribe(self, audio_data_url: str) -> Dict[str, Any]:
        ...


class MessageRouteProvider(Protocol):
    def route_message(
        self,
        request_id: str,
        conversation_id: str,
        input_text: str,
        target_device_id: Optional[str],
        source: str,
    ) -> Dict[str, Any]:
        ...


class SpeechSynthesisProvider(Protocol):
    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        ...


class RK3588SpeechProvider:
    def __init__(self, client: RK3588AgentClient) -> None:
        self.client = RK3588TtsClient(client)

    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        audio_path = self.client.synthesize(text)
        try:
            audio = audio_path.read_bytes()
        finally:
            self.client.cleanup(audio_path)
        normalized = _normalize_wav_for_wearable(audio)
        return normalized, "wav", "audio/wav"


class PortableAudioError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# ---------------- 意图插件 ----------------
# 每个插件只负责一种能力：决定 session_action / device_action / 要不要合成语音。
# 新增能力时只要再写一个 Plugin 并注册进 _build_intent_router，不用改第③步的 if 链。


class ChatPlugin(Plugin):
    """闲聊、知识问答、实时信息：回复直接用分类器给的那句话。"""

    intents = ("chat", "search")

    def catalog_lines(self):
        return [
            "- chat：闲聊、知识问答、出主意、安慰人；reply 直接给答案",
            "- search：查实时信息（天气、新闻、价格、比分、汇率）；reply 给结论，查不到就直说",
        ]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        if intent.reply:
            return IntentResult(
                intent=intent.intent,
                reply=intent.reply,
                slots=intent.slots,
                source=intent.source,
            )
        if ctx.chat is None:
            return IntentResult(
                intent=intent.intent,
                reply="我暂时没想好怎么回你。",
                source="fallback",
            )
        # 分类器只给了意图没给话：退回完整对话链路，让它正经答一轮
        ctx.chat()
        return IntentResult(intent=intent.intent, handled=True)


class FountainPlugin(Plugin):
    """屏幕上的音乐喷泉开关，纯设备本地动作。"""

    intents = ("fountain_start", "fountain_stop")

    def catalog_lines(self):
        return [
            "- fountain_start：打开音乐喷泉；reply 只一句确认",
            "- fountain_stop：关闭音乐喷泉；reply 只一句确认",
        ]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        turning_on = intent.intent == "fountain_start"
        return IntentResult(
            intent=intent.intent,
            reply=intent.reply
            or (
                "好的，音乐喷泉启动，跟着音乐跳起来。"
                if turning_on
                else "音乐喷泉已关闭。"
            ),
            session_action=intent.intent,
            source=intent.source,
        )


class MusicPlugin(Plugin):
    """点歌与播放控制：真正的取流由设备直连中枢的 Range 接口完成。"""

    intents = (
        "music_play",
        "music_pause",
        "music_resume",
        "music_replay",
        "music_next",
        "music_stop",
        "music_volume_up",
        "music_volume_down",
    )

    def catalog_lines(self):
        return [
            '- music_play：点歌或换歌；slots: {"query": "歌名或歌手，随便放就留空"}',
            "- music_pause / music_resume：暂停、继续",
            "- music_replay / music_next / music_stop：重播、下一首、停止",
            "- music_volume_up / music_volume_down：调音量",
        ]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        if ctx.music is None:
            return IntentResult(intent=intent.intent, reply="现在没法放歌。")
        query = str(intent.slots.get("query") or intent.slots.get("song") or "").strip()
        ctx.music((intent.intent, query))
        return IntentResult(intent=intent.intent, handled=True)


class SleepPlugin(Plugin):
    """「退下吧」类指令：熄屏进待机。"""

    intents = ("sleep",)

    def catalog_lines(self):
        return ["- sleep：用户让你退下、休息、别说话时用；reply 一句告别"]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        return IntentResult(
            intent="sleep",
            reply=intent.reply or "好，我先待命。",
            session_action="sleep",
            source=intent.source,
        )


class HostControlPlugin(Plugin):
    """本机控制：桌宠说「打开WPS」就让这台 Mac 真的去打开。

    和外设插件的区别：动作执行在**中枢所在的这台电脑**上（open -a / osascript），
    不走设备分发器。session_action 必须是 continue——固件只对 continue
    播回复音频，未知的 action 设备会当没听见。
    """

    intents = ("app_open", "app_quit")

    def __init__(self, executor: Optional[Any] = None) -> None:
        # executor 可注入假实现做单测；默认真打 host_control
        self._execute = executor or host_control.execute

    def catalog_lines(self):
        return [
            '- app_open：打开/启动这台电脑上的应用或网站；slots: {"app": "应用名或网址，如 WPS、微信、bilibili.com"}',
            '- app_quit：退出/关闭这台电脑上的应用；slots: {"app": "应用名"}',
        ]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        slots = intent.slots or {}
        target = ""
        for key in ("app", "target", "name", "url", "site", "website"):
            value = str(slots.get(key) or "").strip()
            if value:
                target = value
                break
        if not target:
            return IntentResult(
                intent=intent.intent,
                reply="你要打开哪个应用？说一下名字。",
                source=intent.source,
            )
        action = "open_app" if intent.intent == "app_open" else "quit_app"
        result = self._execute(action, target)
        return IntentResult(
            intent=intent.intent,
            reply=str(result.get("message") or "这个操作我没做成。"),
            session_action="continue",
            source=intent.source,
        )


_CN_VOLUME_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}

# 说话音量的本地快通道：只有出现这些词才认为「在调音量」，
# 避免把「你声音好听」这类闲聊当成指令。
_VOLUME_NOUNS = ("音量", "声音", "响度", "喇叭", "外放")
_VOLUME_PHRASES = (
    "大点声",
    "小点声",
    "大声点",
    "小声点",
    "轻一点",
    "响一点",
    "太吵",
    "好吵",
    "太响",
    "太大声",
    "太闹",
)
_VOLUME_QUESTION_WORDS = ("多少", "几档", "几级", "多大")


def _is_volume_question(text: str) -> bool:
    """「现在音量多少」这类问句：答案就在中枢里，不该去问模型。"""
    normalized = "".join(str(text or "").lower().split())
    if not normalized:
        return False
    if not any(noun in normalized for noun in _VOLUME_NOUNS):
        return False
    return any(word in normalized for word in _VOLUME_QUESTION_WORDS)


_SPEECH_RATE_SUBJECTS = ("说", "讲", "语速", "念", "读")
# 音量、音乐语境一律让开：「声音小一点」是调音量，「放慢歌」是找歌。
_SPEECH_RATE_SKIP = (
    "音量", "声音", "大声", "小声", "响", "歌", "音乐", "视频", "录音",
)
# 「再慢一点」「慢点」这种省略主语的跟进说法也要认 —— 用户第一次说完
# 「说话慢一点」之后，后面往往只补一句「再慢一点」。要求整句就这么几个字，
# 「快点放歌」「慢慢走」不算。
_SPEECH_RATE_BARE = re.compile(
    r"^[再稍微]{0,2}(慢|快)(一点|一点儿|点|些|一些)?[啊呀吧呢。！!？?]*$"
)


def _speech_rate_direction(text: str) -> Optional[str]:
    """这句话是在要求「说得快/慢」吗？返回 "slower" / "faster" / None。

    「慢」有两种意思，必须分开：「说慢一点」是让我放慢，
    「你讲得太快了」是抱怨快（同样是让我放慢）；「说得太慢了」才是要我快。
    所以先认「太X／这么X」这种抱怨句式，再看裸的「慢/快」。
    """
    normalized = "".join(str(text or "").lower().split())
    if not normalized:
        return None
    has_subject = any(word in normalized for word in _SPEECH_RATE_SUBJECTS)
    # 「太慢了」这种纯抱怨也可以单独成句，不要求带「说」。
    bare_complaint = any(word in normalized for word in ("太慢", "好慢", "太快", "好快"))
    if not has_subject and not bare_complaint and not _SPEECH_RATE_BARE.match(normalized):
        return None
    if any(word in normalized for word in _SPEECH_RATE_SKIP):
        return None
    if any(word in normalized for word in ("太快", "过快", "这么快", "那么快")):
        return "slower"
    if any(word in normalized for word in ("太慢", "过慢", "这么慢", "那么慢")):
        return "faster"
    if "慢" in normalized:
        return "slower"
    if "快" in normalized:
        return "faster"
    return None


def _device_volume_slots(text: str) -> Optional[Dict[str, Any]]:
    """纯本地判断这句话是不是在改说话音量，返回 slots 或 None。

    问句（「现在音量多少」）一律返回 None，交给 bot / device_query，
    否则会把提问当成「调大」。
    """
    normalized = "".join(str(text or "").lower().split())
    if not normalized:
        return None
    if not (
        any(noun in normalized for noun in _VOLUME_NOUNS)
        or any(phrase in normalized for phrase in _VOLUME_PHRASES)
    ):
        return None
    if any(word in normalized for word in _VOLUME_QUESTION_WORDS):
        return None
    match = re.search(r"(?:调到|调成|设为|设成|改到|到)\s*([0-9一二两三四五])", normalized)
    if match:
        return {"capability": "volume", "value": _as_int(match.group(1))}
    if any(word in normalized for word in ("最大", "最响", "最高")):
        return {"capability": "volume", "value": len(VOLUME_GAINS)}
    if any(word in normalized for word in ("最小", "最轻", "静音")):
        return {"capability": "volume", "value": 1}
    if any(word in normalized for word in ("太吵", "太响", "太大", "太大声")):
        return {"capability": "volume", "delta": -1}
    direction = _volume_direction(normalized)
    if direction:
        return {"capability": "volume", "delta": direction}
    return None


# ---------------------------------------------------------------- 点歌查询
# 用户点歌时会夹一堆「说明」：「单字一个枫，枫叶的枫，周杰伦的枫」。
# 整句丢给搜索是一条垃圾 query，只会搜出《枫 - 全网找歌君》这种翻唱。
_MUSIC_CLAUSE_SPLIT = re.compile(r"[，,。.、！!？?；;：:~～…\s]+")
_MUSIC_FILLERS = (
    "的音乐吧",
    "的音乐",
    "的歌",
    "的歌曲",
    "歌曲",
    "音乐",
    "好吗",
    "好不好",
    "可以吗",
    "行吗",
    "谢谢",
    "单字一个",
    "单字是",
    "一个字",
    "歌名是",
    "歌名叫",
    "名字叫",
    "名字是",
    "就是那个",
    "那首",
    "一首",
    "一首歌",
    "吧",
    "呗",
    "呀",
    "啊",
    "听",
)


def _song_query(text: str) -> str:
    """把口语收敛成能拿去搜索的「歌名 歌手」。

    "我要听单字一个枫，枫叶的枫，周杰伦的枫"
      -> 分句 ["枫", "枫叶的枫", "周杰伦的枫"]
      -> 歌名取最短的 "枫"，再把 "周杰伦的枫" 里的 "周杰伦" 当歌手
      -> "枫 周杰伦"
    """
    clauses: List[str] = []
    for clause in _MUSIC_CLAUSE_SPLIT.split(str(text or "")):
        piece = clause
        for filler in _MUSIC_FILLERS:
            piece = piece.replace(filler, "")
        # ASR 会把歌名括起来：「《晴天》」「“枫”」，书名号引号不能进 query
        piece = "".join(
            ch for ch in piece if ch not in "《》〈〉“”\"'‘’【】[]()（）"
        )
        piece = piece.strip().strip("的")
        if piece:
            clauses.append(piece)
    if not clauses:
        return ""
    title = min(clauses, key=len)
    artist = ""
    for piece in clauses:
        head, sep, tail = piece.rpartition("的")
        if not sep or not tail or len(head) < 2:
            continue
        # 「枫叶的枫」「名字的的」这类是在拆字解释，不是「歌手的歌」
        if head.endswith(("叶", "字", "写", "说", "拼")):
            continue
        if tail not in title and title not in tail:
            continue
        artist = head
        if len(tail) < len(title):
            title = tail
        break
    return f"{title} {artist}".strip()


# ---------------------------------------------------------------- 语音门控
# 有意图词的句子才可能是对桌宠说的。没有唤醒词（KWS）时，这是把
# 「房间里的闲谈」和「跟桌宠说话」分开的最便宜办法。
_VOICE_INTENT_WORDS = (
    "我要听", "要听", "听首", "放一首", "放个", "放点", "播放", "点歌",
    "来一首", "来首", "想听", "听一首", "唱一首", "歌", "音乐", "暂停", "继续", "重播", "下一首", "上一首", "换一首",
    "别放", "停止", "音量", "声音", "大声", "小声", "大点声", "小点声", "几档",
    "喷泉", "退下", "待命", "睡觉", "屏幕", "电量", "天气", "几点", "时间",
    # 「今天会下雨吗」不含「天气」二字，实测被语音门控当环境音拦掉
    # （2026-11-03）：补 上雨/气温 这类天气问法的自然说法。
    "下雨", "气温", "多少度", "几度",
    "日期", "闹钟", "提醒", "查一下", "搜索", "帮我", "你好", "在吗",
    "亮度", "静音", "录音", "灯", "舵机", "开启", "关闭",
    # 祈使句没有疑问词也没有问号，光靠上面的词表认不出来：
    # 2026-11-06 实测「给我讲个笑话」在唤醒后被门控丢掉。
    "讲个", "说个", "来段", "笑话", "故事", "聊聊", "陪我", "翻译", "念一",
)

# 疑问词：中文问句靠这几个词标记，比继续往意图词表里加词抗漂移。
# 2026-11-06 实测：「成都今天的温度怎么样？」不含任何意图词，被门控当环境音
# 丢掉，用户看到的就是「说了没反应」。而加「温度」只是补一次漏 —— 下一句
# 「这附近有什么好吃的」还得再补。改成认问句形态，一次覆盖一整类。
_VOICE_QUESTION_WORDS = (
    "什么", "怎么", "为什么", "为啥", "咋样", "哪个", "哪里", "哪儿",
    "多少", "多久", "多长", "多远", "多大",
    "是不是", "有没有", "能不能", "会不会", "要不要", "可不可以", "行不行",
)
# 「是吧？」「对吧？」这类也带问号，但只有四个字，仍是语气词不是提问；
# 要求去掉标点后至少这么长，才把问号当成「在问桌宠」。
_VOICE_QUESTION_MIN_CHARS = 6

# 「退下」这两个字 ASR 认不住：2026-11-06 实测「行，那你退下吧」被写成
# 「行的，那你对去吧。」—— 首字退→对、「下」也滑成了「去」。逐字列别名
# 补不完（退/推/腿/褪/跪/对…），所以把首字统一收敛成「退」、「去」收敛成
# 「下」，再看句子里有没有「退下」。标点不动，所以「对，下面」不会被拼成
# 「退下」；只要两个字的同音变体连在一起，就是这一族收口指令。
_EXIT_HOMOPHONE = str.maketrans(
    {
        "对": "退",
        "队": "退",
        "推": "退",
        "腿": "退",
        "褪": "退",
        "跪": "退",
        "怼": "退",
        "蜕": "退",
        "去": "下",
    }
)


def voice_gate_allows(text: str, last_active_at: float, window: float, now: float) -> bool:
    """这句话该不该应答。纯函数，方便单测。

    * 含意图词 → 应答，并刷新会话窗口；
    * 是完整问句（有疑问词，或够长且带问号）→ 应答，并刷新会话窗口；
    * 距上次交互不到 ``window`` 秒 → 应答（正常连续对话不该被打断）；
    * 其余（房间里的闲谈、电视声）→ 静默丢弃。
    """
    normalized = "".join(str(text or "").lower().split())
    if any(word in normalized for word in _VOICE_INTENT_WORDS):
        return True
    if _looks_like_question(normalized):
        return True
    if last_active_at and (now - last_active_at) <= window:
        return True
    return False


def _looks_like_question(normalized: str) -> bool:
    """这句话是不是在提问。纯函数，输入已去掉空白并转小写。"""
    bare = normalized.strip("。.！!？?，,、~～…—-《》〈〉()（）\"'“” ")
    if not bare:
        return False
    # 疑问词是明确信号，不受长度限制：2026-11-06 实测「为啥不讲？」只有四个字，
    # 被下面的 6 字门槛挡在门外当成了环境音。四字问句在口语里非常常见
    # （「你去吗」「几点啊」「能听见吗」），拿长度一票否决反而误杀。
    if any(word in normalized for word in _VOICE_QUESTION_WORDS):
        return True
    # 「吗」结尾几乎只能是问句（能听见吗 / 你去吗 / 弄好了吗），三字以上就算。
    # 卡在 3 字是为了让「好吗？」「是么？」这类两字附和继续当环境闲聊丢掉。
    if bare.endswith("吗") and len(bare) >= 3:
        return True
    # 只剩「问号」这一个信号时才要求够长：「是吧？」「对吧？」是语气词不是提问。
    if len(bare) < _VOICE_QUESTION_MIN_CHARS:
        return False
    return "？" in normalized or "?" in normalized


# ---------------------------------------------------------------- 唤醒词
# 唤醒是「上传一小段 → 云端 ASR → 关键词匹配」，ASR 对三字专名极不稳定：
# 实测「贾维斯」被写成「查维斯」「贾文思」「假维基」，逐字对齐必然漏。
# 所以既给完整别名，也给「尾音 + 近音首字」的兜底。
#
# 2026-11-02：用户明确只保留「贾维斯」，原来的「你好小智 / 小智同学 / 晓智」
# 那一族已全部删除 —— 桌宠不再响应任何「小智」叫法。
_WAKE_BASE_ALIASES = (
    "贾维斯",
    "贾文思",
    "贾维",
    "查维",
    "javis",
    "jarvis",
)
# 「X维斯」这类尾音在中文里没有别的语义，尾音对上就该认。
_WAKE_TAILS = (
    "维斯",
    "维思",
    "威斯",
    "威思",
    "维丝",
    "威丝",
    "维司",
    "伟斯",
    "维基",
    "维琪",
)
# 首字近音集合：贾/加/假/家/嘉/佳/查/扎/杰/雅…都可能是 ASR 对 jia 的写法。
_WAKE_HEADS = "贾加假家嘉佳查扎杰洁捷雅亚甲价驾"


def _wake_aliases() -> Tuple[str, ...]:
    """内置别名 + 用户自定义（``PET_WAKE_WORDS=小豆,豆豆``）。

    用户改唤醒词不该改代码，所以这里读环境变量；配置在 ``config/plugin.env``。
    """
    raw = os.environ.get("PET_WAKE_WORDS", "")
    custom = tuple(
        word for word in re.split(r"[,，;；\s]+", raw.lower()) if word.strip()
    )
    return _WAKE_BASE_ALIASES + custom


# 网易云不认「周杰伦」这种写法，同一批歌里常出现 Jay / JAY(周杰伦) 等别名
_ARTIST_ALIASES = {
    "周杰伦": ("周杰伦", "jay", "jvr", "周董"),
    "林俊杰": ("林俊杰", "jj", "jj lin"),
    "陈奕迅": ("陈奕迅", "eason"),
    "邓紫棋": ("邓紫棋", "gem"),
    "薛之谦": ("薛之谦", "joker xue"),
}


def _split_song_query(query: str) -> Tuple[str, str]:
    """「枫 周杰伦」-> ("枫", "周杰伦")；只有歌名时歌手为空。"""
    parts = [part for part in str(query or "").split() if part]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return (parts[0] if parts else ""), ""


def _norm(text: str) -> str:
    """只留中文和字母数字，便于比较歌手名（'Jay' / 'jay ' / 'J A Y' 等价）。"""
    return "".join(
        ch
        for ch in str(text or "").lower()
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    )


def _artist_matches(artist: str, hint: str) -> bool:
    haystack = _norm(artist)
    needle = _norm(hint)
    if not haystack or not needle:
        return False
    aliases = _ARTIST_ALIASES.get(hint, (hint,))
    return any(_norm(alias) and _norm(alias) in haystack for alias in aliases)


def _normalize_capability(slots: Dict[str, Any]) -> str:
    """把 bot 写的 capability 收敛成内部名字。缺省按 volume 处理。"""
    raw = str(
        slots.get("capability")
        or slots.get("device_capability")
        or slots.get("name")
        or slots.get("target")
        or "volume"
    ).strip().lower()
    if any(key in raw for key in ("volume", "sound", "音量", "声音")):
        return "volume"
    return raw


def _as_int(value: Any) -> Optional[int]:
    """从 3 / "3" / "3档" / "三" 里取数，取不到返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit() or ch == "-")
    if digits not in {"", "-"}:
        try:
            return int(digits)
        except ValueError:
            return None
    for char, number in _CN_VOLUME_DIGITS.items():
        if char in text:
            return number
    return None


def _volume_direction(text: str) -> int:
    """模型没给具体数值时，从原话判断要变大还是变小。"""
    normalized = str(text or "")
    if any(word in normalized for word in ("大", "高", "响", "加", "增", "up")):
        return 1
    if any(word in normalized for word in ("小", "低", "轻", "减", "降", "down")):
        return -1
    return 0


def _requested_volume_step(
    slots: Dict[str, Any], text: str, current: int
) -> int:
    """算出目标档位（0 基）。优先用 slots 的绝对档位，其次相对量，最后看原话。"""
    for key in ("value", "level", "to", "step", "volume"):
        if key in slots:
            raw = _as_int(slots[key])
            if raw is not None:
                # bot 按提示词给的是 1-5（用户视角），转成 0 基
                if 1 <= raw <= len(VOLUME_GAINS):
                    return raw - 1
                return clamp_volume_step(raw)
    for key in ("delta", "change", "by", "amount"):
        if key in slots:
            raw = _as_int(slots[key])
            if raw is not None:
                return clamp_volume_step(current + raw)
    return clamp_volume_step(current + _volume_direction(text))


class DevicePlugin(Plugin):
    """设备自身的能力：查状态、改设置（目前是音量）。

    和 ``FountainPlugin`` / ``MusicPlugin`` 的区别：那两个是「让设备做事」，
    这个是「问设备自己怎么样」以及改设备自己的参数。

    关键设计：这类插件的回复**不让模型写**。提示词里虽然注入了实时状态，
    模型仍可能说错数字（幻觉、单位混乱）。所以查询一律由 ``DeviceRegistry``
    生成确定文案，动作执行完也用确定文案回执 —— 数字只有一处来源。
    """

    intents = ("device_query", "device_set")

    def __init__(self, devices: DeviceRegistry) -> None:
        self.devices = devices

    def catalog_lines(self):
        return [
            "- device_query：问设备自身状态（现在音量多少、屏幕亮不亮、在不在放歌、"
            "喷泉开没开）；reply 只说一句，中枢会用真实数值覆盖",
            '- device_set：调说话音量；slots: {"capability":"volume","value":1-5} '
            '（1 最小、5 最大）或 {"capability":"volume","delta":1}',
        ]

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        capability = _normalize_capability(intent.slots)
        if capability != "volume":
            return IntentResult(
                intent=intent.intent,
                reply=intent.reply or "这个设置我暂时改不了。",
                source=intent.source,
            )
        if intent.intent == "device_query":
            profile = self.devices.get(ctx.device_id)
            reply = profile.volume_sentence()
            if "音乐" in ctx.text:
                extra = profile.music_sentence()
                if extra:
                    reply = f"{reply}{extra}"
            return IntentResult(
                intent="device_query",
                reply=reply,
                slots={"capability": "volume", "value": profile.volume_step + 1},
                session_action="continue",
                source=intent.source,
            )
        return self._set_volume(intent, ctx)

    def _set_volume(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        profile = self.devices.get(ctx.device_id)
        before = clamp_volume_step(profile.volume_step)
        target = _requested_volume_step(intent.slots, ctx.text, before)
        after = self.devices.set_volume(ctx.device_id, target)
        step = clamp_volume_step(after.volume_step)
        if step == before:
            reply = (
                "已经是最大声了。"
                if step == len(VOLUME_GAINS) - 1
                else "已经是最小声了。"
            )
        else:
            reply = f"音量调到 {step + 1} 档，{after.volume_label}。"
        return IntentResult(
            intent="device_set",
            reply=reply,
            slots={"capability": "volume", "value": step + 1},
            session_action="continue",
            # 固件目前不消费这个字段（说话音量由中枢缩放），留着给将来同步
            # 设备音乐档位用：字段形状定下来，固件一加解析就能用。
            device_action={"type": "volume_set", "value": step, "gain": round(after.volume_gain, 2)},
            source=intent.source,
        )


class PortableRequestService:
    PLAN_TTL_SECONDS = 300

    """Accepts portable-device requests and processes them asynchronously."""

    MAX_MEDIA_BYTES = 10 * 1024 * 1024
    SUPPORTED_INPUT_TYPES = {"text", "audio"}

    def __init__(
        self,
        hub: HubService,
        plan_provider: Optional["HardwarePlanProvider"] = None,
        transcription_provider: Optional["AudioTranscriptionProvider"] = None,
        route_provider: Optional["MessageRouteProvider"] = None,
        speech_provider: Optional["SpeechSynthesisProvider"] = None,
        max_workers: int = 4,
        agent: Optional[Any] = None,
        music: Optional[MusicService] = None,
        intent_classifier: Optional[Any] = None,
        devices: Optional[DeviceRegistry] = None,
    ) -> None:
        self.hub = hub
        # RK3588 旧链路已停用：路由和 TTS 都不再默认指向 192.168.0.140。
        # 硬件动作统一走桌面大脑（agent + MCP 插件），语音合成走 speech_provider。
        self.plan_provider = plan_provider
        self.transcription_provider = transcription_provider
        self.route_provider = route_provider or (
            plan_provider
            if callable(getattr(plan_provider, "route_message", None))
            else None
        )
        self.speech_provider = speech_provider
        # 桌宠大脑：唤醒后的每一轮对话都交给它（模型工具循环 + MCP）。
        # 为 None 时退回原来的 RK3588 自动路由。
        self.agent = agent
        # 点歌服务：本地音乐库 + B 站在线搜索，统一转 16k 单声道 WAV 供设备拉流。
        self.music = music if music is not None else MusicService()
        # 第③步「判定意图」的承担者：配了分类器就交给 bot / 模型，没配就退回关键词。
        self.intent_classifier = intent_classifier
        # 设备影子：问「现在音量多少」时唯一的数据来源。默认落盘到
        # ~/.sparkbot/devices.json，重启不丢。
        self.devices = devices if devices is not None else DeviceRegistry()
        # 统一外设动作分发器：录音豆等外设插件的命令都从这里执行。
        # 缺密钥时是 disabled 态（插件会回「还没配置」），服务照常起。
        self.action_dispatcher = build_dispatcher_from_env()
        self.intent_router = self._build_intent_router(intent_classifier)
        # 快通道优先级：默认 bot 先判（新增能力只改提示词和插件），
        # 置 1 则先跑关键词，"启动喷泉 / 暂停" 这类动作 0 延迟生效。
        self._rules_first = str(
            os.environ.get("PET_INTENT_RULES_FIRST") or "0"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self._lock = threading.RLock()
        # 语音门控：没唤醒词的情况下，房间里的闲谈也会被 VAD 当成指令上传，
        # 中枢一接就回一句「嗯，我听着呢」，听起来像在插话。
        # 规则：句子里有意图词（放/听/音量/喷泉/退下…）或刚交互过 N 秒内才应答，
        # 其余静默丢弃（不回话、不合成语音）。PET_VOICE_GATE=0 可整机关掉。
        self._voice_gate = str(
            os.environ.get("PET_VOICE_GATE") or "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        try:
            self._voice_window_seconds = float(
                os.environ.get("PET_VOICE_WINDOW") or 45
            )
        except ValueError:
            self._voice_window_seconds = 45.0
        self._voice_last_active: Dict[str, float] = {}
        # TTS 结果缓存：桌宠的固定话术（「好的，我先待命。」「音乐喷泉已关闭。」）
        # 每次都要 0.7 秒重新合成，缓存后重复句子直接命中，省掉这一整段。
        self._tts_cache: Dict[str, Tuple[bytes, str, str]] = {}
        self._tts_cache_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="portable-request",
        )
        self._futures: Dict[str, Future[Any]] = {}

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
        if self.agent is not None:
            try:
                self.agent.close()
            except Exception:  # pragma: no cover - 关闭路径保持安静
                pass
        if self.intent_classifier is not None:
            try:
                closer = getattr(self.intent_classifier, "close", None)
                if callable(closer):
                    closer()
            except Exception:  # pragma: no cover
                pass

    def _build_intent_router(
        self, classifier: Optional[Any]
    ) -> Optional[IntentRouter]:
        """把插件注册表建起来。分类器缺席就不建路由，全部退回关键词。"""
        if classifier is None:
            return None
        plugins = [
            ChatPlugin(),
            FountainPlugin(),
            MusicPlugin(),
            DevicePlugin(self.devices),
            # 本机控制：桌宠指挥这台 Mac（打开WPS/微信/网站…）
            HostControlPlugin(),
            # 录音豆：第一个走统一动作分发器的外设，加新外设照它抄
            RecordingBeanPlugin(dispatcher=self.action_dispatcher),
            SleepPlugin(),
        ]
        router = IntentRouter(plugins, classifier)
        try:
            print(f"意图插件已注册：{', '.join(router.intents)}")
        except Exception:  # pragma: no cover - 打印失败不影响主流程
            pass
        return router

    def _route_intent(
        self,
        request_id: str,
        request: Dict[str, Any],
        text: str,
        session_mode: str,
    ) -> bool:
        """第③步：让分类器判意图，再派给对应插件。返回 True 表示已处理。"""
        if self.intent_router is None:
            return False
        device_id = str(request.get("portable_device_id") or "")
        started = time.perf_counter()
        ctx = PluginContext(
            text=text,
            request_id=request_id,
            device_id=device_id,
            session_mode=session_mode,
            session_active=self.music.has_session(device_id),
            synthesize=self._synthesize,
            chat=lambda: self._run_agent_turn(request_id, request, text),
            music=lambda pair: self._handle_music_action(request_id, device_id, pair),
            # 注入设备实时状态：模型答「现在音量多少」时才有据可依
            device_context=self.devices.summary(device_id),
            # 会话 id 交给分类器：chat 的回复是它一次调用出的，
            # 不在这儿带上历史，多轮对话就会一句一失忆。
            conversation_id=str(request.get("conversation_id") or ""),
        )
        result = self.intent_router.resolve(text, ctx)
        print(
            f"[TIMING] intent={(time.perf_counter() - started) * 1000:.0f}ms "
            f"result={'fallback' if result is None else result.intent}",
            flush=True,
        )
        if result is None:
            # 分类器超时 / 不是 JSON / 没有分类器：交给调用方跑旧关键词分支
            return False
        if result.handled:
            return True
        reply = result.reply or ""
        audio = self._synthesize(reply) if reply else None
        self._update(
            request_id,
            state="completed",
            response_text=reply,
            session_action=result.session_action,
            device_action=result.device_action,
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
            result={"intent": result.public()},
        )
        return True

    def upload_media(
        self,
        portable_device_id: str,
        media_type: str,
        audio_format: Optional[str],
        content_type: Optional[str],
        data: bytes,
    ) -> Dict[str, Any]:
        if not isinstance(portable_device_id, str) or not portable_device_id.strip():
            raise ValidationError("X-Portable-Device-Id 必须是非空字符串")
        if media_type != "audio":
            raise ValidationError("X-Media-Type 当前必须是 audio")
        if not isinstance(audio_format, str) or not audio_format.strip():
            raise ValidationError("X-Audio-Format 必须是非空字符串")
        if not data:
            raise ValidationError("音频内容不能为空")
        if len(data) > self.MAX_MEDIA_BYTES:
            raise ValidationError(
                f"音频不能超过 {self.MAX_MEDIA_BYTES // (1024 * 1024)} MB"
            )
        media_id = f"media-{uuid.uuid4()}"
        media = self.hub.store.create_portable_media(
            media_id=media_id,
            portable_device_id=portable_device_id.strip(),
            media_type=media_type,
            audio_format=audio_format.strip().lower(),
            content_type=content_type,
            data=data,
        )
        self.hub.event_bus.publish(
            self.hub.store.add_event(
                event_type="portable.media.stored",
                payload=self._public_media(media),
            )
        )
        return self._public_media(media)

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._validate_payload(payload)
        # device_state 只进设备影子，不进请求记录（库里没这个字段）
        device_state = normalized.pop("device_state", None)
        if device_state:
            self.devices.update_from_state(
                str(normalized.get("portable_device_id") or ""), device_state
            )
        request_id = normalized["request_id"]
        with self._lock:
            existing = self.hub.store.get_portable_request(request_id)
            if existing is not None:
                self._ensure_same_request(existing, normalized)
                return self._public_request(existing)
            request = self.hub.store.create_portable_request(**normalized)
            self._emit_request_event(request)
            self._futures[request_id] = self._executor.submit(
                self._process_request,
                request_id,
            )
            return self._public_request(request)

    def get(self, request_id: str) -> Dict[str, Any]:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        # 设备每 60~150ms 来查一次，顺手做「卡死超时」兜底：处理线程阻塞在
        # 某个外部调用上时 finally 永远不会执行，只能靠这里把请求推进终态，
        # 否则设备会一直等这条永远不结束的请求，整机等于聋了。
        self._finalize_if_stuck(request_id)
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        return self._public_request(request)

    def get_audio(self, request_id: str) -> Tuple[bytes, str]:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        audio = request.get("response_audio")
        content_type = request.get("response_audio_content_type") or "audio/wav"
        if not isinstance(audio, bytes) or not audio:
            raise HubError("请求还没有可播放的回复音频", status_code=404)
        # 说话音量在这里生效：TTS 缓存存的是原始电平，每次下发按当前档位缩放，
        # 所以改完音量下一句就听得出来，不用重新合成，也不用改固件。
        device_id = str(request.get("portable_device_id") or "")
        return self.devices.apply_volume(device_id, audio), str(content_type)

    def confirm(self, request_id: str, plan_id: str) -> Dict[str, Any]:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        if request.get("state") != "planned":
            raise ConflictError("当前请求不处于待确认状态")
        if request.get("plan_id") != plan_id:
            raise ConflictError("plan_id 与当前计划不匹配")
        if self._plan_expired(request):
            self._update(
                request_id,
                state="failed",
                error_code="PLAN_EXPIRED",
                error_message="计划已过期，请重新发起请求。",
                response_text="计划已过期，请重新发起请求。",
            )
            raise ConflictError("计划已过期，请重新发起请求")

        self._update(
            request_id,
            state="confirming",
            response_text="已确认，正在执行计划…",
        )
        with self._lock:
            self._futures[request_id] = self._executor.submit(
                self._execute_planned_request,
                request_id,
            )
        return self._public_request(
            self.hub.store.get_portable_request(request_id)  # type: ignore[arg-type]
        )

    def _process_request(self, request_id: str, depth: int = 0) -> None:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            return
        try:
            session_mode = request.get("session_mode") or "conversation"
            if request["input_type"] == "audio":
                self._update(request_id, state="transcribing")
                # 唤醒走专门通路：先剪掉首尾静音再送 ASR，识别不出再拿原音频
                # 补一次。实测同一条录音「原样→查维斯」「去静音→贾维斯」。
                if session_mode == "wake":
                    text = self._transcribe_wake_request(request_id, request)
                else:
                    text = self._transcribe_request(request_id, request)
            else:
                text = request.get("text") or ""
            if session_mode == "wake":
                hit = self._is_wake_phrase(text)
                # 唤醒失败没有任何其他痕迹（设备只收到 sleep，不发声），
                # 必须留一行日志，否则用户说「叫不醒」时无从判断是 VAD 没抓到
                # 还是 ASR 听错、还是关键词表漏了。
                print(f"[WAKE] 转写={text!r} -> {'唤醒' if hit else '未命中'}", flush=True)
                if hit:
                    # 2026-11-06 实测：唤醒「维斯。」成功，紧接着说的「给我讲个笑话」
                    # 被门控当环境语音丢掉，用户看到的症状就是「只听懂唤醒和结束」。
                    # 逐词补表治不了根 —— 唤醒本身就是最强的一次「我在跟你说话」，
                    # 命中唤醒就该顺手把会话窗口打开，接下来那句话不再走门控。
                    self._mark_voice_active(str(request.get("portable_device_id") or ""))
                    print(
                        f"[WAKE] 已开启会话窗口 {int(self._voice_window_seconds)}s"
                        "（期间不再过环境音门控）",
                        flush=True,
                    )
                    # 连锁指令：「贾维斯放首歌」「贾维斯今天天气怎么样」。
                    # 剥掉唤醒词后还有正文就把这条请求就地改写成普通对话
                    # 重走管线 —— 设备拿到的是 music_play / continue 这些
                    # 真实动作，待机状态直接执行，不用先「我在」再等用户
                    # 复述一遍。纯唤醒（后面没跟话）才回「我在」。
                    remainder = self._strip_wake_phrase(text)
                    if self._is_chain_command(remainder) and not self._is_noise_transcript(remainder):
                        print(f"[WAKE] 连锁指令：{remainder!r}", flush=True)
                        self._update(
                            request_id,
                            text=remainder,
                            input_type="text",
                            session_mode="conversation",
                        )
                        self._process_request(request_id, depth + 1)
                        return
                    self._update(
                        request_id,
                        state="completed",
                        response_text="我在",
                        session_action="awake",
                    )
                else:
                    self._update(
                        request_id,
                        state="completed",
                        response_text="",
                        session_action="sleep",
                    )
                return
            if self._is_exit_phrase(text):
                # 熄屏是设备侧行为（收到 session_action=sleep 才关背光），
                # 用户看不到中枢这边做了什么，所以这行日志要留——「说了退下但
                # 屏幕还亮着」时，先看这里有没有下发，再看设备串口有没有
                # [VOICE] exit phrase recognised。
                print(f"[SLEEP] 下发熄屏：{text!r}", flush=True)
                response_text = "好的，我先待命。"
                audio = self._synthesize(response_text)
                self._update(
                    request_id,
                    state="completed",
                    response_text=response_text,
                    session_action="sleep",
                    response_audio=audio[0] if audio else None,
                    response_audio_format=audio[1] if audio else None,
                    response_audio_content_type=audio[2] if audio else None,
                )
                return
            # 表情态（亮屏）下用户也会先喊一声唤醒词再说话。
            # 以前这种句子走普通闲聊 → 没有意图词 → 被语音门控当环境音静默丢掉，
            # 用户的感觉就是「喊了它不理」。这里明确应一声「我在」。
            # 连锁指令与 wake 分支对齐（2026-11-03）：剥掉唤醒词后还有正文
            # 就把这条请求改写成普通对话重走管线；纯唤醒才回「我在」。
            # 之前这里不剥词，表情态下「贾维斯，今天会下雨吗」只换来一句
            # 「我在」，后半句被整个丢掉。
            # 播放中（music_control）不处理：放歌时插一句话不该打断音乐。
            if session_mode != "music_control" and self._is_wake_phrase(text):
                remainder = self._strip_wake_phrase(text)
                if self._is_chain_command(remainder) and not self._is_noise_transcript(remainder):
                    print(f"[WAKE] 连锁指令（表情态）：{remainder!r}", flush=True)
                    self._update(
                        request_id,
                        text=remainder,
                        input_type="text",
                        session_mode="conversation",
                    )
                    self._process_request(request_id, depth + 1)
                    return
                response_text = "我在"
                audio = self._synthesize(response_text)
                self._update(
                    request_id,
                    state="completed",
                    response_text=response_text,
                    session_action="awake",
                    response_audio=audio[0] if audio else None,
                    response_audio_format=audio[1] if audio else None,
                    response_audio_content_type=audio[2] if audio else None,
                )
                return
            # 噪声残片：什么都不做，不回话也不合成语音。
            # 注意这里必须是 "idle" 而不是 "sleep"：设备只在收到 sleep 时才熄屏，
            # 把噪声当成 sleep 会让桌宠在有人聊天的房间里反复黑屏。
            if self._is_noise_transcript(text):
                self._update(
                    request_id,
                    state="completed",
                    response_text="",
                    session_action="idle",
                )
                return
            # 语音门控：房间里别人聊天、电视声也会被 VAD 当成一句话传上来。
            # 只对**语音**输入生效 —— 文本输入是有人主动发的指令，不能丢。
            if (
                request.get("source") == "wearable"
                and request.get("input_type") == "audio"
                and session_mode != "music_control"
            ):
                gate_device = str(request.get("portable_device_id") or "")
                if not self._voice_gate_allows(gate_device, text):
                    print(f"[GATE] 忽略环境语音：{text}", flush=True)
                    self._update(
                        request_id,
                        state="completed",
                        response_text="",
                        session_action="idle",
                    )
                    return
            # 音乐喷泉 / 点歌：设备本地的显示与播放动作，不需要中枢下发硬件命令。
            # 只对桌宠（source=wearable）生效。
            if request.get("source") == "wearable":
                device_id_hint = str(request.get("portable_device_id") or "")
                # 播放中说的话只可能是控制指令：延迟优先，绝不排队等模型。
                if session_mode == "music_control":
                    fountain_action = self._fountain_action(text)
                    if fountain_action is not None:
                        self._finish_fountain(request_id, fountain_action)
                        return
                    music_action = self._music_action(text, True)
                    if music_action is not None:
                        self._handle_music_action(
                            request_id, device_id_hint, music_action
                        )
                        return
                    # 没被识别成控制指令：别进大模型，让设备接着放
                    self._update(
                        request_id,
                        state="completed",
                        response_text="",
                        session_action="music_ignore",
                    )
                    return
                # 改名：「以后叫你小灰」「你应该叫幻形之了」。名字要落盘，
                # 否则重启回到默认名，下一句「你叫什么」又自相矛盾。
                if self._try_local_rename(
                    request_id,
                    text,
                    conversation_id=str(request.get("conversation_id") or ""),
                ):
                    return
                # 「说慢点」改的是中枢 TTS 的语速，比设备音量更接近用户本意，
                # 必须排在 device 之前：模型会把「讲慢一点」误判成调音量。
                if self._try_local_speech_rate(request_id, text):
                    return
                # 设备状态与音量：答案就在中枢里，两种优先级下都走快通道。
                # 播放中的「大点声」已在上面 music_control 分支被 MusicPlugin 接走。
                if self._try_local_device(request_id, device_id_hint, text):
                    return
                if self._try_local_facts(request_id, text):
                    return
                # 第③步：意图判定。默认交给 bot / 模型，失败或无分类器时退回关键词。
                if not self._rules_first and self._route_intent(
                    request_id, request, text, session_mode
                ):
                    return
                fountain_action = self._fountain_action(text)
                if fountain_action is not None:
                    self._finish_fountain(request_id, fountain_action)
                    return
                # 点歌 / 播放控制：确定性识别，播放中不必等同一次模型
                music_action = self._music_action(
                    text, self.music.has_session(device_id_hint)
                )
                if music_action is not None:
                    self._handle_music_action(
                        request_id, device_id_hint, music_action
                    )
                    return
                if self._rules_first and self._route_intent(
                    request_id, request, text, session_mode
                ):
                    return
            self._update(request_id, state="understanding")
            if self.agent is not None:
                self._run_agent_turn(request_id, request, text)
                return
            if TextAgent._is_list_devices_request(text):
                response_text = TextAgent._format_devices(self.hub.list_devices())
                audio = self._synthesize(response_text)
                self._update(
                    request_id,
                    state="completed",
                    response_text=response_text,
                    session_action="continue",
                    response_audio=audio[0] if audio else None,
                    response_audio_format=audio[1] if audio else None,
                    response_audio_content_type=audio[2] if audio else None,
                )
                return
            target_device_id = request.get("target_device_id")
            if self.route_provider is not None:
                response = self.route_provider.route_message(
                    request_id=request_id,
                    conversation_id=request["conversation_id"],
                    input_text=text,
                    target_device_id=target_device_id,
                    source=request["source"],
                )
                route = str(response.get("route") or "").strip().lower()
                if route in {"chat", "clarification", "assistant"}:
                    response_text = self._route_reply(
                        response,
                        "我暂时没有可用的回复。",
                    )
                    audio = self._synthesize(response_text)
                    self._update(
                        request_id,
                        state="completed",
                        response_text=response_text,
                        session_action="continue",
                        response_audio=audio[0] if audio else None,
                        response_audio_format=audio[1] if audio else None,
                        response_audio_content_type=audio[2] if audio else None,
                    )
                    return
                if not self._is_plan_response(response, route):
                    if self._route_reply(response, ""):
                        response_text = self._route_reply(response, "")
                        audio = self._synthesize(response_text)
                        self._update(
                            request_id,
                            state="completed",
                            response_text=response_text,
                            session_action="continue",
                            response_audio=audio[0] if audio else None,
                            response_audio_format=audio[1] if audio else None,
                            response_audio_content_type=audio[2] if audio else None,
                        )
                        return
                    raise HubError(
                        "RK3588 自动路由没有返回聊天回复或结构化计划",
                        status_code=502,
                    )
                device_id = target_device_id or self._resolve_device_id()
            else:
                device_id = target_device_id or self._resolve_device_id()
                if not device_id:
                    raise HubError("当前没有可用的真实设备", status_code=503)
                if self.plan_provider is None:
                    raise HubError(
                        "没有可用的硬件执行通道：请启用桌宠大脑（run.py --agent）",
                        status_code=503,
                    )
                response = self.plan_provider.plan_hub_command(
                    request_id=request_id,
                    conversation_id=request["conversation_id"],
                    input_text=text,
                    target_device_id=device_id,
                    source="portable",
                )
            if not device_id:
                raise HubError("当前没有可用的真实设备", status_code=503)
            plan_id = response.get("plan_id")
            command = response.get("command") or {}
            if not isinstance(command, dict) and isinstance(response.get("plan"), dict):
                command = response["plan"].get("command") or {}
            if (
                not self._is_plan_response(response, str(response.get("route") or ""))
                or not isinstance(plan_id, str)
                or not isinstance(command, dict)
                or not isinstance(command.get("capability"), str)
            ):
                raise HubError("RK3588 未返回有效的结构化计划", status_code=502)
            intent = {
                "capability": command["capability"],
                "device_id": command.get("device_id") or device_id,
                "params": command.get("params") or {},
            }
            auto_execute = request["source"] == "wearable"
            self._update(
                request_id,
                state="confirming" if auto_execute else "planned",
                intent=intent,
                plan_id=plan_id,
                response_text=self._response_message(
                    response,
                    "计划已生成，请确认执行。",
                ),
                session_action="continue",
            )
            if auto_execute:
                self._execute_planned_request(request_id)
        except (ValidationError, ConflictError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="COMMAND_FAILED",
                error_message=error.message,
                response_text=error.message,
            )
        except (AgentGatewayError, HubError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="HUB_UNAVAILABLE",
                error_message=str(error),
                response_text=str(error),
                retryable=True,
            )
        except PortableAudioError as error:
            self._update(
                request_id,
                state="failed",
                error_code=error.code,
                error_message=error.message,
                response_text=error.message,
                retryable=error.retryable,
            )
        except ChatLLMError as error:
            # 主备两个端点都没给出回复。以前这里会落到下面的通用兜底，
            # 设备端念出来的是「中枢处理请求时发生内部错误」—— 一句用户听不懂、
            # 也没告诉他接下来该怎么办的话，而且往往已经等了 30 秒。
            # 桌宠该说的是人话：认个错、请他再说一遍。
            reply = "我这边网络有点卡，你再说一遍好吗？"
            print(f"[CHAT] 所有端点都没回复：{error}", flush=True)
            try:
                audio = self._synthesize(reply)
            except Exception:  # noqa: BLE001 - 合成失败就别再往下炸
                audio = None
            self._update(
                request_id,
                state="completed",
                error_code="CHAT_UNAVAILABLE",
                error_message=str(error),
                response_text=reply,
                session_action="continue",
                response_audio=audio[0] if audio else None,
                response_audio_format=audio[1] if audio else None,
                response_audio_content_type=audio[2] if audio else None,
                result={"intent": {"intent": "chat", "source": "fallback_text"}},
            )
        except Exception as error:  # pragma: no cover - defensive worker boundary
            self._update(
                request_id,
                state="failed",
                error_code="HUB_UNAVAILABLE",
                error_message=str(error),
                response_text="中枢处理请求时发生内部错误。",
                retryable=True,
            )
        finally:
            with self._lock:
                self._futures.pop(request_id, None)
            # 走到这里说明处理函数已经返回。若它没有把请求推进到终态
            # （例如被剥成残片后重走管线、中途某个分支直接 return），
            # 设备就会永远轮询同一条请求、再也回不到采集循环。
            # 只在最外层收尾：递归内层还在跑的时候不能替它收。
            if depth == 0:
                self._finalize_if_stuck(request_id, force=True)

    # 设备侧没有超时机制：只要拿到非终态就一直轮询同一条请求。任何请求在
    # 「中间态」停留超过这么久就强制收尾，否则整机会卡死在一次失败的唤醒上。
    # 实机出现过：唤醒词被 ASR 写成「夏威斯家。」，剥词只剩一个「家」被当成
    # 连锁指令重走管线，中途没落地 → 设备无限轮询 → 用户怎么喊都叫不醒。
    _STUCK_REQUEST_TIMEOUT_S = 90.0
    _INTERIM_STATES = ("transcribing", "understanding")

    def _finalize_if_stuck(self, request_id: str, force: bool = False) -> None:
        request = self.hub.store.get_portable_request(request_id)
        if request is None:
            return
        state = str(request.get("state") or "")
        if state not in self._INTERIM_STATES:
            return
        age = 0.0
        try:
            started = datetime.fromisoformat(
                str(request.get("created_at") or "").replace("Z", "+00:00")
            )
            age = (datetime.now(timezone.utc) - started).total_seconds()
        except (TypeError, ValueError):
            age = 0.0
        if not force and age < self._STUCK_REQUEST_TIMEOUT_S:
            return
        print(
            f"[REQUEST] 收尾：{request_id} 停在 {state}（{age:.0f}s），"
            "强制标记失败让设备脱身",
            flush=True,
        )
        self._update(
            request_id,
            state="failed",
            error_code="REQUEST_STUCK",
            error_message="请求处理未正常结束，已强制收尾。",
            response_text="",
        )

    def _run_agent_turn(
        self,
        request_id: str,
        request: Dict[str, Any],
        text: str,
    ) -> None:
        """把一轮对话交给桌宠大脑，回复文本与合成语音照旧回传设备。"""
        turn = self.agent.handle_turn(
            conversation_id=request["conversation_id"],
            user_text=text,
            request_id=request_id,
        )
        audio = self._synthesize(turn.text)
        self._update(
            request_id,
            state="completed",
            response_text=turn.text,
            session_action="continue",
            plan_id=turn.plan_id,
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
            result=turn.public(),
        )

    def _execute_planned_request(self, request_id: str) -> None:
        try:
            request = self.hub.store.get_portable_request(request_id)
            if request is None or not isinstance(request.get("intent"), dict):
                raise HubError("计划不存在或内容无效", status_code=409)
            intent = request["intent"]
            device_id = str(intent.get("device_id") or "")
            if self.plan_provider is None:
                raise HubError(
                    "没有可用的硬件执行通道：请启用桌宠大脑（run.py --agent）",
                    status_code=503,
                )
            response = self.plan_provider.dispatch_hub_command(
                request_id=f"execute-{uuid.uuid4()}",
                conversation_id=request["conversation_id"],
                plan_id=str(request["plan_id"]),
                target_device_id=device_id,
                source="portable",
            )
            state = str(response.get("state") or response.get("status") or "")
            success = bool(response.get("ok")) or state == "succeeded"
            request_state = self._request_state_from_response(response, success)
            response_text = self._execution_response_message(
                response,
                intent,
                request_state,
            )
            audio = self._synthesize(response_text)
            self._update(
                request_id,
                state=request_state,
                command_id=response.get("command_id"),
                response_text=response_text,
                session_action="continue",
                response_audio=audio[0] if audio else None,
                response_audio_format=audio[1] if audio else None,
                response_audio_content_type=audio[2] if audio else None,
                result=response.get("result"),
                error_code=("COMMAND_FAILED" if not success else None),
                error_message=(
                    self._response_message(response, "设备执行失败。")
                    if not success
                    else None
                ),
                retryable=request_state in {"queued", "executing", "timed_out"},
            )
        except (ValidationError, ConflictError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="COMMAND_FAILED",
                error_message=error.message,
                response_text=error.message,
            )
        except (AgentGatewayError, HubError) as error:
            self._update(
                request_id,
                state="failed",
                error_code="HUB_UNAVAILABLE",
                error_message=str(error),
                response_text=str(error),
                retryable=True,
            )
        finally:
            with self._lock:
                self._futures.pop(request_id, None)

    def _update(self, request_id: str, **changes: Any) -> Dict[str, Any]:
        request = self.hub.store.update_portable_request(request_id, **changes)
        if request is None:
            raise HubError(f"请求不存在: {request_id}", status_code=404)
        self._emit_request_event(request)
        return request

    def _emit_request_event(self, request: Dict[str, Any]) -> None:
        self.hub.event_bus.publish(
            self.hub.store.add_event(
                event_type="portable.request.updated",
                payload=self._public_request(request),
            )
        )

    def _resolve_device_id(self) -> Optional[str]:
        devices = self.hub.list_devices()
        online = [device for device in devices if device.get("status") == "online"]
        if online:
            return online[0]["device_id"]
        if devices:
            return devices[0]["device_id"]
        return None

    @staticmethod
    def _request_state_from_response(
        response: Dict[str, Any],
        success: bool,
    ) -> str:
        state = str(response.get("state") or response.get("status") or "")
        if response.get("timed_out") and not success:
            return "timed_out"
        if success:
            return "succeeded"
        if state in {"queued", "accepted", "running", "pending"}:
            return "queued"
        return "failed"

    @staticmethod
    def _response_message(response: Dict[str, Any], fallback: str) -> str:
        for key in ("reply", "message", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    @classmethod
    def _execution_response_message(
        cls,
        response: Dict[str, Any],
        intent: Dict[str, Any],
        request_state: str,
    ) -> str:
        if request_state != "succeeded":
            return cls._response_message(response, "执行结果已返回。")

        for key in ("message", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        success_messages = {
            "record.start": "已经开始录音。",
            "record.stop": "已经停止录音。",
            "status.read": "设备状态查询完成。",
        }
        capability = intent.get("capability")
        if isinstance(capability, str) and capability in success_messages:
            return success_messages[capability]
        return "设备执行成功。"

    @staticmethod
    def _route_reply(response: Dict[str, Any], fallback: str) -> str:
        value = response.get("reply")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    @staticmethod
    def _is_plan_response(response: Dict[str, Any], route: str) -> bool:
        return response.get("status") == "planned" or route in {
            "command",
            "hardware",
            "tool",
        }

    def _request_media_bytes(self, request: Dict[str, Any]) -> bytes:
        media_id = request.get("media_id")
        media = self.hub.store.get_portable_media(str(media_id or ""))
        if media is None:
            return b""
        data = media.get("data")
        return bytes(data) if isinstance(data, (bytes, bytearray)) else b""

    def _transcribe_wake_request(
        self,
        request_id: str,
        request: Dict[str, Any],
    ) -> str:
        """唤醒专用转写：先赌最干净的一版，不行再把其余版本并发补一遍。

        「贾维斯」这类三字专名对 ASR 极度敏感：同一段真机录音，整段送进去是
        「咋回事？」，只裁最响的那一簇是「查维斯。」，窗口再宽一点又变成别的。
        所以不赌单次结果，而是按「越干净越先试」的顺序准备几个版本：

        1. `wake-trim`：最响簇 + 前 0.2 / 后 0.3 秒气口（默认，最常命中）；
        2. `wake-wide`：气口放到 0.3 / 0.45 秒，兜住「裁剪削掉音头」；
        3. `wake-raw`：原音频，兜住裁剪逻辑本身不适用的情况。

        第一版单独跑，命中就直接返回（快路径）。没命中才把 2、3 两版**并发**
        跑——串行要 1.0~1.5 秒，而唤醒失败拖得越久，设备「聋」着的时间越长，
        用户重复喊就被漏掉。2、3 只在录音确实有人声能量（peak ≥ 1200，房间
        底噪大约 800）时才跑，避免噪声片段白花两三次 ASR 时间。
        """
        raw = self._request_media_bytes(request)
        if not raw:
            return self._transcribe_request(request_id, request, tag="wake-raw")

        loud_enough = wav_peak(raw) >= 1200
        tight = prepare_wake_wav(raw)
        variants: list = [("wake-trim", tight)]
        if loud_enough:
            wide = prepare_wake_wav(raw, pad_head=0.30, pad_tail=0.45)
            if wide is not None and wide != tight:
                variants.append(("wake-wide", wide))
            variants.append(("wake-raw", None))

        first_tag, first_audio = variants[0]
        first_text = ""
        first_error: Optional[PortableAudioError] = None
        try:
            first_text = self._transcribe_payload(request, first_audio, first_tag)
        except PortableAudioError as error:
            first_error = error
        if first_text and self._is_wake_phrase(first_text):
            self._update(request_id, transcript=first_text)
            return first_text

        rest = variants[1:]
        if not rest:
            if not first_text and first_error is not None:
                raise first_error
            self._update(request_id, transcript=first_text)
            return first_text

        pool = ThreadPoolExecutor(
            max_workers=len(rest),
            thread_name_prefix="wake-variant",
        )
        best = first_text
        try:
            futures = {
                pool.submit(self._transcribe_payload, request, audio, tag): tag
                for tag, audio in rest
            }
            for future in as_completed(futures):
                tag = futures[future]
                try:
                    text = future.result()
                except PortableAudioError:
                    continue
                if not best:
                    best = text
                if text and self._is_wake_phrase(text):
                    print(
                        f"[WAKE] {tag} 命中（首轮为 {first_text!r}）",
                        flush=True,
                    )
                    self._update(request_id, transcript=text)
                    return text
        finally:
            # 命中后不必等其余版本跑完，直接放手（它们不写库，只是白跑一次）
            pool.shutdown(wait=False)
        if not best and first_error is not None:
            raise first_error
        self._update(request_id, transcript=best)
        return best

    def _transcribe_request(
        self,
        request_id: str,
        request: Dict[str, Any],
        audio_override: Optional[bytes] = None,
        tag: str = "",
    ) -> str:
        transcript = self._transcribe_payload(request, audio_override, tag)
        self._update(request_id, transcript=transcript)
        return transcript

    def _transcribe_payload(
        self,
        request: Dict[str, Any],
        audio_override: Optional[bytes] = None,
        tag: str = "",
    ) -> str:
        """只跑一次 ASR，不写库。

        唤醒失败时会拿同一段录音的几种裁剪版本再试一遍，那几个变体现在
        是并发跑的；并发线程不能各自写同一条请求记录（顺序会乱、还会互相
        覆盖），所以把「跑 ASR」和「落库」拆开：线程只跑 ASR，命中判定和
        写库留给调用方那一个线程做。
        """
        provider = self.transcription_provider
        if provider is None:
            raise PortableAudioError(
                "AUDIO_UNDERSTANDING_NOT_CONFIGURED",
                "音频已收到，但固定式中枢尚未配置音频理解能力。",
                retryable=True,
            )

        media_id = request.get("media_id")
        media = self.hub.store.get_portable_media(str(media_id or ""))
        if media is None or media.get("media_type") != "audio":
            raise PortableAudioError(
                "AUDIO_MEDIA_NOT_FOUND",
                "找不到请求对应的音频内容。",
            )
        if media.get("portable_device_id") != request.get("portable_device_id"):
            raise PortableAudioError(
                "AUDIO_MEDIA_DEVICE_MISMATCH",
                "音频所属随身设备与当前请求不一致。",
            )
        data = media.get("data")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise PortableAudioError(
                "AUDIO_MEDIA_EMPTY",
                "请求对应的音频内容为空。",
            )

        content_type = str(media.get("content_type") or "").split(";", 1)[0].strip()
        content_type = content_type or self._audio_content_type(media.get("audio_format"))
        payload_bytes = bytes(audio_override) if audio_override else bytes(data)
        audio_data_url = (
            f"data:{content_type};base64,"
            f"{base64.b64encode(payload_bytes).decode('ascii')}"
        )
        started = time.perf_counter()
        # 只有底噪就别送 ASR 了：白花 0.7 秒和一次调用，还会幻听出
        # 「咳。」「朝鲜族女性。」这种句子去干扰语音门控。
        silence_note = audio_silence_note(payload_bytes)
        if silence_note:
            print(f"[MIC] 跳过识别：{silence_note} {tag}".rstrip(), flush=True)
            return ""
        level_note = describe_audio_level(payload_bytes)
        if level_note:
            print(f"[MIC] {level_note} {tag}".rstrip(), flush=True)
        try:
            response = provider.transcribe(audio_data_url)
        except Exception as error:
            raise PortableAudioError(
                "AUDIO_TRANSCRIPTION_FAILED",
                f"音频转写失败：{error}",
                retryable=True,
            ) from error
        print(
            f"[TIMING] asr={(time.perf_counter() - started) * 1000:.0f}ms "
            f"in_bytes={len(payload_bytes)} {tag}".rstrip(),
            flush=True,
        )

        transcript = self._extract_transcript(response)
        if not transcript:
            raise PortableAudioError(
                "AUDIO_TRANSCRIPTION_EMPTY",
                "音频转写未返回可用文字。",
                retryable=True,
            )
        return transcript

    @staticmethod
    def _audio_content_type(audio_format: Any) -> str:
        formats = {
            "wav": "audio/wav",
            "wave": "audio/wav",
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4",
            "aac": "audio/aac",
            "ogg": "audio/ogg",
            "opus": "audio/opus",
            "webm": "audio/webm",
            "pcm": "audio/pcm",
        }
        normalized = str(audio_format or "").strip().lower().lstrip(".")
        return formats.get(normalized, "application/octet-stream")

    @classmethod
    def _extract_transcript(cls, response: Any) -> str:
        if isinstance(response, str):
            return response.strip()
        if not isinstance(response, dict):
            return ""
        for key in ("transcript", "text", "recognized_text", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("result", "data", "output", "response"):
            nested = response.get(key)
            transcript = cls._extract_transcript(nested)
            if transcript:
                return transcript
        return ""

    def _plan_expired(self, request: Dict[str, Any]) -> bool:
        updated_at = request.get("updated_at")
        if not isinstance(updated_at, str):
            return True
        created = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() > self.PLAN_TTL_SECONDS

    @staticmethod
    def _validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        required = [
            "portable_device_id",
            "conversation_id",
            "request_id",
            "input_type",
        ]
        values: Dict[str, Any] = {}
        for key in required:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{key} 必须是非空字符串")
            values[key] = value.strip()
        protocol_version = payload.get("protocol_version", "1.0")
        if protocol_version != "1.0":
            raise ValidationError("protocol_version 必须是 1.0")
        source = payload.get("source", "portable")
        if not isinstance(source, str) or not source.strip():
            raise ValidationError("source 必须是非空字符串")
        values["protocol_version"] = "1.0"
        values["source"] = source.strip()
        session_mode = payload.get("session_mode", "conversation")
        if session_mode not in {"wake", "conversation", "music_control"}:
            raise ValidationError(
                "session_mode 必须是 wake、conversation 或 music_control"
            )
        values["session_mode"] = session_mode
        text = payload.get("text")
        media_id = payload.get("media_id")
        if values["input_type"] == "text":
            if not isinstance(text, str) or not text.strip():
                raise ValidationError("text 必须是非空字符串")
        elif not isinstance(media_id, str) or not media_id.strip():
            raise ValidationError("音频请求必须提供 media_id")
        values.update(
            {
                "text": text.strip() if isinstance(text, str) else None,
                "media_id": media_id.strip() if isinstance(media_id, str) else None,
                "target_device_id": (
                    payload.get("target_device_id").strip()
                    if isinstance(payload.get("target_device_id"), str)
                    and payload.get("target_device_id").strip()
                    else None
                ),
                # 设备上报的自身状态（可选）：音量档位、屏幕、喷泉、音乐。
                # 中枢只用来「回答用户提问」，不写回请求记录。
                "device_state": (
                    payload.get("device_state")
                    if isinstance(payload.get("device_state"), dict)
                    else None
                ),
            }
        )
        return values

    @staticmethod
    def _ensure_same_request(
        existing: Dict[str, Any],
        normalized: Dict[str, Any],
    ) -> None:
        for key in (
            "portable_device_id",
            "conversation_id",
            "protocol_version",
            "source",
            "input_type",
            "session_mode",
            "text",
            "media_id",
            "target_device_id",
        ):
            if existing.get(key) != normalized.get(key):
                raise ConflictError("request_id 已存在，但请求内容不一致")

    @staticmethod
    def _public_request(request: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in request.items()
            if key not in {"media_id", "response_audio"}
        }

    TTS_CACHE_MAX_ENTRIES = 64
    # 语速档位：1.0 是音色本身的自然速度，0.74 已经明显放慢但还不发飘，
    # 0.62 以下 Piper 会把字拖散；上限 1.45 再往上就听不清字了。
    SPEECH_RATE_MIN = 0.62
    SPEECH_RATE_MAX = 1.45
    SPEECH_RATE_STEP = 0.12

    def _synthesize(self, text: str) -> Optional[Tuple[bytes, str, str]]:
        if self.speech_provider is None or not text.strip():
            return None
        key = text.strip()
        with self._tts_cache_lock:
            cached = self._tts_cache.get(key)
        if cached is not None:
            print(f"[TIMING] tts=0ms (cached) chars={len(text)}", flush=True)
            return cached
        started = time.perf_counter()
        try:
            result = self.speech_provider.synthesize(text)
        except Exception as error:
            self.hub.event_bus.publish(
                self.hub.store.add_event(
                    event_type="portable.tts.failed",
                    payload={"message": str(error)},
                )
            )
            return None
        print(
            f"[TIMING] tts={(time.perf_counter() - started) * 1000:.0f}ms "
            f"chars={len(text)} bytes={len(result[0]) if result else 0}",
            flush=True,
        )
        if result is not None:
            with self._tts_cache_lock:
                if len(self._tts_cache) >= self.TTS_CACHE_MAX_ENTRIES:
                    self._tts_cache.pop(next(iter(self._tts_cache)), None)
                self._tts_cache[key] = result
        return result

    @staticmethod
    def _is_wake_phrase(text: str) -> bool:
        raw = "".join(str(text).lower().split())
        # 标点先剥掉：「贾维斯。」「贾维斯！」和「贾维斯」必须等价。
        # ASR 给中文短句普遍带句号，不剥会在子串匹配上白白丢掉。
        bare = raw.strip("。.！!？?，,、~～…—-·\"'“”‘’《》〈〉【】[]()（）")
        if not bare:
            return False
        if any(alias in bare for alias in _wake_aliases()):
            return True
        # 「X维斯」的尾音组合在中文里几乎没有别的语义，而首字 ASR 十次错八次：
        # 实测「贾维斯」被转写成「查维斯」，逐字对齐等于永远唤不醒。
        if len(bare) <= 12 and any(tail in bare for tail in _WAKE_TAILS):
            return True
        # 尾字被吃掉的情况（只剩「贾维」「查维」）：有「维」且首字是近音。
        if 2 <= len(bare) <= 6 and "维" in bare and any(
            head in bare for head in _WAKE_HEADS
        ):
            return True
        # 「小智」那一族已按用户要求下线（2026-11-02）：桌宠只认「贾维斯」。
        # 所以这里不再放行「小/晓」+「智/志/至/之」的组合，也不再放行
        # 「你好」「在吗」这类为小智准备的短招呼 —— 它们现在只是普通聊天。
        return False

    @staticmethod
    def _strip_wake_phrase(text: str) -> str:
        """剥掉句首的唤醒词，返回剩下的指令部分（没有就返回空串）。

        支持「贾维斯」「贾维斯，今天天气怎么样」「贾维斯放首歌」这类连锁
        说法：设备只喊唤醒词时应一声「我在」；后面还跟着话就把后半截直接
        当指令执行，省掉「我在 → 你说 → 再说一遍」一轮往返。

        匹配顺序和 ``_is_wake_phrase`` 保持一致，但按**最具体的形态优先**：
        先找「维斯」尾音（覆盖 贾维斯/查维斯/加维斯 各种首字写法），再试
        完整别名（jarvis / 贾文思），最后才是「近音首字 + 维」的残缺形态。
        不能反过来先试短别名 ——「查维」会抢在「查维斯」前面命中，把
        「斯放首歌」的「斯」留在指令里。剥完为空说明是纯唤醒。
        """
        raw = "".join(str(text).lower().split())
        bare = raw.strip("。.！!？?，,、~～…—-·\"'“”‘’《》〈〉【】[]()（）")
        if not bare:
            return ""

        remainder: Optional[str] = None
        # 1) 尾音优先：「维斯」在中文里没有别的语义，唤醒词到尾音结束。
        #    允许唤醒词前面有 0~1 个语气垫字（哎/嗯）。
        for tail in _WAKE_TAILS:
            pos = bare.find(tail)
            if 0 <= pos <= 2:
                remainder = bare[pos + len(tail):]
                break
        # 2) 完整别名（不含尾音的那些）：jarvis / javis / 贾文思。
        if remainder is None:
            for alias in sorted(_wake_aliases(), key=len, reverse=True):
                pos = bare.find(alias)
                if 0 <= pos <= 2:
                    remainder = bare[pos + len(alias):]
                    break
        # 3) 尾字被吃掉（「贾维放首歌」）：「维」+ 近音首字，剥到「维」为止。
        if remainder is None:
            pos = bare.find("维")
            if 0 <= pos <= 2 and (pos == 0 or bare[pos - 1] in _WAKE_HEADS):
                remainder = bare[pos + 1:]
        if remainder is None:
            return ""
        if not remainder:
            return ""

        # 剥掉唤醒词后紧跟着的标点和语气垫字，剩下的才是真正的指令。
        remainder = remainder.strip("。.！!？?，,、~～…—-·\"'“”‘’ ")
        while remainder[:2] in ("帮我", "麻烦", "请问", "就是", "那个") or remainder[:1] in (
            "哎", "嗯", "诶", "喂", "请", "那",
        ):
            remainder = remainder[
                2 if remainder[:2] in ("帮我", "麻烦", "请问", "就是", "那个") else 1:
            ].lstrip("。.！!？?，,、~～…—-·\"'“”‘’ ")
        return remainder

    @staticmethod
    def _is_chain_command(remainder: str) -> bool:
        """剥掉唤醒词后剩下的那截，够不够当一条完整指令。

        实测 ASR 会把「贾维斯」写成「夏威斯家。」，剥词后只剩一个「家」。
        这种单字残片以前被当成连锁指令重走管线，走到半路没落地，设备就
        一直轮询同一条请求——现象是「喊了待命以后怎么都叫不醒」。
        一个字不可能是完整指令，直接回「我在」让它重新说。
        """
        return len(str(remainder or "").strip("。.！!？?，,、~～…—-· ")) >= 2

    @staticmethod
    def _is_exit_phrase(text: str) -> bool:
        """识别「退下吧」这类「你可以休息了」的收口指令。

        ASR 对两三个字的短句很不可靠：实测「退下吧」会被写成「跪下吧」「推下吧」
        「退下把」。这类结果如果当普通聊天处理，桌宠就永远退不下去，
        所以第二个字放宽成同音/近音，只要求「X下(吧)」的形状。
        """
        normalized = "".join(str(text).lower().split())
        normalized = normalized.strip("。.！!？?，,、~～…—-")
        if not normalized:
            return False
        if any(
            word in normalized
            for word in ("回到待机", "进入待机", "可以休息", "休息吧", "睡吧", "待命")
        ):
            return True
        aliases = ("退下", "跪下", "推下", "腿下", "褪下", "退夏", "退虾")
        if any(alias in normalized for alias in aliases):
            return True
        if "退下" in normalized.translate(_EXIT_HOMOPHONE):
            return True
        # 「关闭 / 关掉 / 熄屏」：用户说「你先关闭吧」是想让桌宠熄屏待命，
        # 不是关某个外设。2026-11-06 实测这句被模型判成 fountain_stop，
        # 回了「音乐喷泉已经关掉了」，而喷泉根本没开过。
        # 句子里提到喷泉/音乐/灯这些具体对象时才让开，交给对应插件。
        screen_words = ("关闭", "关掉", "关屏", "熄屏", "黑屏", "屏幕关")
        other_targets = ("喷泉", "音乐", "歌", "灯", "录音", "闹钟", "音量", "声音")
        if any(word in normalized for word in screen_words) and not any(
            word in normalized for word in other_targets
        ):
            return True
        # 「X 下吧」这种短句也放行（退下把 / 跪下吧 / 推下呗）。
        # 2026-11-06 现场踩坑：这里原本只判「≤5 字且以 下吧 结尾」，
        # 于是「等一下吧」「你放下吧」「你配一下吧」全被判成收口指令，
        # 桌宠当场熄屏。收口必须先是「退」这一族（含同音字），再看尾巴，
        # 光凭「下吧」两个字不够 —— 那只是「一下」的正常口语。
        translated = normalized.translate(_EXIT_HOMOPHONE)
        if len(normalized) <= 5 and translated.endswith(
            ("退下吧", "退下把", "退下呗", "退下拜")
        ):
            return True
        return False

    @staticmethod
    def _is_noise_transcript(text: str) -> bool:
        """判断这次识别结果是不是「环境噪声/尾音」而不是一句话。

        ASR 对风扇声、衣服摩擦、喇叭余音经常返回「嗯。」「啊」「《》」这种残片。
        会话模式下如果照单全收，中枢就会回一句固定长语音，喇叭的声音再被麦克风
        拾回来，下一轮又识别成「嗯。」——形成「自己说、自己听、自己回」的死循环，
        听感上就是每隔十几秒重复一遍同样的鬼畜语音。
        """
        normalized = "".join(str(text or "").lower().split())
        bare = normalized.strip("。.！!？?，,、~～…—-《》〈〉()（）\"'“” ")
        if not bare:
            return True
        if bare in {
            "嗯", "恩", "啊", "哦", "噢", "呃", "唉", "诶", "哎", "唔", "哼", "咳",
            "嗯嗯", "啊啊", "哦哦", "呃呃", "哈哈哈", "呵呵", "谢谢", "嗯哼",
        }:
            return True
        return False

    @staticmethod
    def _fountain_action(text: str) -> Optional[str]:
        """识别「音乐喷泉」的开关意图。

        返回 "fountain_start" / "fountain_stop" / None。确定性关键词匹配，
        不走大模型：设备端要立刻动作，不能被闲聊或计划确认流程吞掉。
        """
        normalized = "".join(str(text).lower().split())
        if "喷泉" not in normalized and "fountain" not in normalized:
            return None
        stop_words = (
            "关", "停", "结束", "退出", "别放", "收起来", "收起",
            "off", "stop", "close",
        )
        # 疑问句一律不当作动作：「喷泉的原理是什么」「讲讲喷泉」
        start_words = ("启动", "打开", "开始", "开启", "来一个", "启动一下", "放一个")
        question_words = ("原理", "讲讲", "介绍", "是什么", "为什么", "怎么", "多少")
        if any(word in normalized for word in question_words) and not any(
            word in normalized for word in start_words
        ):
            return None
        if any(word in normalized for word in stop_words):
            return "fountain_stop"
        return "fountain_start"

    @staticmethod
    def _music_action(text: str, session_active: bool) -> Optional[Tuple[str, str]]:
        """识别点歌与播放控制意图，返回 (action, query) 或 None。

        确定性关键词匹配（不走大模型）：设备端要立刻动作，播放中的
        「暂停 / 重播」更不能被大模型排队吞掉。返回的 action 直接当
        session_action 下发给固件。
        """
        normalized = "".join(str(text).lower().split())
        if not normalized:
            return None
        # 「音乐喷泉」由 _fountain_action 负责，这里排除，避免抢走喷泉指令
        if "喷泉" in normalized:
            return None

        has_music_word = any(
            word in normalized
            for word in ("音乐", "歌", "曲", "声音", "音量", "music", "song", "播放")
        )
        triggers = (
            "我要听",
            "要听",
            "放一首",
            "放一个",
            "放个",
            "放点",
            "来一首",
            "来一个",
            "来点",
            "来首",
            "唱一首",
            "唱首",
            "点歌",
            "点一首",
            "播放",
            "放一下",
            "给我放",
            "我想听",
            "想听",
            "听一首",
            "听首",
        )
        trigger = next((word for word in triggers if word in normalized), None)
        # 「单字一个X」「歌名叫X」这种报歌名的句式，本身就是点歌意图
        song_meta = any(
            word in normalized
            for word in ("单字一个", "一个字", "歌名叫", "歌名是", "名字叫")
        )
        allow_bare = session_active or has_music_word or trigger is not None or song_meta
        if not allow_bare:
            return None

        volume_up_words = (
            "大声",
            "声音大",
            "调大",
            "大点声",
            "音量加",
            "音量大",
            "太小",
            "听不清",
        )
        volume_down_words = (
            "小声",
            "声音小",
            "调小",
            "小点声",
            "音量减",
            "音量小",
            "太吵",
            "太响",
            "太大",
            "轻一点",
        )
        # 播放中的「大点声」才指音乐；没在放音乐时它是「说话音量」，
        # 交给 DevicePlugin 处理，别在这里吞掉再回一句「现在没有在放音乐」。
        if session_active and any(word in normalized for word in volume_up_words):
            return ("music_volume_up", "")
        if session_active and any(word in normalized for word in volume_down_words):
            return ("music_volume_down", "")
        if (
            "暂停" in normalized
            or "停一下" in normalized
            or "停一会儿" in normalized
            or "等一下再放" in normalized
        ):
            return ("music_pause", "")
        replay_words = (
            "重播",
            "重放",
            "再来一遍",
            "再放一遍",
            "再听一遍",
            "从头放",
            "从头再来",
            "重新放",
            "重新播放",
        )
        next_words = ("下一首", "换一首", "换一个", "换首歌", "切歌")
        resume_words = ("继续播放", "继续放", "接着放", "恢复播放", "继续", "接着")
        stop_words = (
            "停止播放",
            "停止音乐",
            "关闭音乐",
            "关掉音乐",
            "关了音乐",
            "别放了",
            "不听了",
            "关掉歌",
            "退出播放",
            "别唱了",
            "别放",
            # 「不要听了」不能掉进点歌分支（它含「要听」）
            "不要听",
            "不想听",
            "别听",
            "不用放",
        )
        if any(word in normalized for word in replay_words):
            return ("music_replay", "")
        if any(word in normalized for word in next_words):
            return ("music_next", "")
        if any(word in normalized for word in stop_words) or (
            has_music_word
            and any(word in normalized for word in ("关", "停止", "停", "别", "不用放"))
        ):
            return ("music_stop", "")
        if any(word in normalized for word in resume_words):
            return ("music_resume", "")

        # 点歌：需要明确的「放 / 唱 / 听」触发词，避免把闲聊当点歌
        if trigger is None:
            # 例外：用户先报歌名再解释（「单字一个枫，枫叶的枫，周杰伦的枫」），
            # ASR 有时会把这句里的触发词吃掉，只剩描述，这里补一条兜底。
            if not song_meta:
                return None
            return ("music_play", _song_query(normalized))
        # 触发词后面那一串口语（单字一个X、X叶的X、周杰伦的X）交给
        # _song_query 收敛成「歌名 歌手」，否则整句拿去做 query 只会搜出翻唱。
        rest = normalized.split(trigger, 1)[1] if trigger in normalized else ""
        return ("music_play", _song_query(rest))

    def _voice_gate_allows(self, device_id: str, text: str) -> bool:
        """见 ``voice_gate_allows``：允许就顺手刷新会话窗口。"""
        if not self._voice_gate:
            return True
        key = str(device_id or "default")
        now = time.monotonic()
        last = self._voice_last_active.get(key, 0.0)
        allowed = voice_gate_allows(text, last, self._voice_window_seconds, now)
        if allowed:
            self._voice_last_active[key] = now
        return allowed

    def _mark_voice_active(self, device_id: str) -> None:
        """手动开一次会话窗口。

        唤醒命中时调用：设备接下来采到的那句必然是「叫醒我之后说的话」，
        不该再过一遍环境音门控。门控关掉时什么都不做。
        """
        if not self._voice_gate:
            return
        self._voice_last_active[str(device_id or "default")] = time.monotonic()

    def _try_local_rename(
        self, request_id: str, text: str, conversation_id: str = ""
    ) -> bool:
        """「以后叫你小灰」这类改名。必须是确定性快通道。

        2026-11-06 现场：用户说「你应该叫幻形之了」，模型回「好呀，那我以后
        就叫幻形之了」—— 嘴上答应了，提示词里还是旧名字，重启更是彻底失忆，
        下一句「你叫什么」又答「小桌」。名字是长期状态，交给模型即兴记是不可靠的：
        让规则层认句式、写盘，模型只负责把结果说出口。
        """
        from .pet_identity import (
            detect_rename,
            detect_reset,
            reset_assistant_name,
            set_assistant_name,
            wake_word,
        )

        if detect_reset(text):
            # 「改回名字」必须先判：名字能改，就必须能改回来。
            changed = reset_assistant_name()
            name = wake_word()
            reply = (
                f"好，我改回{name}了。" if changed else f"我一直叫{name}呀。"
            )
            print(f"[IDENTITY] 恢复默认名「{name}」（changed={changed}）", flush=True)
            return self._finish_rename(request_id, name, reply, conversation_id, text)

        name = detect_rename(text)
        if not name:
            return False
        changed = set_assistant_name(name)
        reply = (
            f"好，以后我就叫{name}。" if changed else f"我现在就叫{name}呀。"
        )
        if changed and name != wake_word():
            # 唤醒词在固件侧，改不了；不说明的话用户改完名就再也叫不醒了。
            reply += f"不过喊我还是喊{wake_word()}。"
        print(f"[IDENTITY] 改名为「{name}」（changed={changed}）", flush=True)
        return self._finish_rename(request_id, name, reply, conversation_id, text)

    def _finish_rename(
        self,
        request_id: str,
        name: str,
        reply: str,
        conversation_id: str = "",
        user_text: str = "",
    ) -> bool:
        audio = self._synthesize(reply)
        self._update(
            request_id,
            state="completed",
            response_text=reply,
            session_action="continue",
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
            result={
                "intent": {"intent": "rename", "source": "rules"},
                "assistant_name": name,
            },
        )
        # 改名走的是规则快通道，绕过了模型 —— 历史得自己补记，
        # 否则下一句模型还不知道刚发生过改名（「我不是让你叫我小灰吗」）。
        if conversation_id and user_text:
            recorder = getattr(
                getattr(getattr(self, "intent_classifier", None), "client", None),
                "remember",
                None,
            )
            if callable(recorder):
                recorder(conversation_id, user_text, reply)
        return True

    def _try_local_speech_rate(self, request_id: str, text: str) -> bool:
        """「说话慢一点 / 语速快一点」——这是中枢自己的 TTS 参数，不是设备能力。

        2026-11-06 实测：用户连说两遍「你讲话讲慢一点」，模型都判成 device_set，
        回了一句「音量调到 1 档，很小」，完全答非所问；用户第二次还得纠正
        「我说你讲话讲慢一点，不是说你名字」。
        语速本来就是 ``LocalNeuralSpeechProvider.speed``，本地改一个浮点数就行，
        不该排几秒的模型队列，更不该被理解成音量。
        """
        direction = _speech_rate_direction(text)
        if direction is None:
            return False
        provider = self.speech_provider
        if provider is None or not hasattr(provider, "speed"):
            # 走系统 say / 云端 TTS 的实现没有统一语速接口，别假装能调。
            return False
        try:
            current = float(getattr(provider, "speed", 1.0) or 1.0)
        except (TypeError, ValueError):
            return False

        step = -self.SPEECH_RATE_STEP if direction == "slower" else self.SPEECH_RATE_STEP
        target = round(max(self.SPEECH_RATE_MIN, min(self.SPEECH_RATE_MAX, current + step)), 3)
        if abs(target - current) < 1e-3:
            reply = "已经是最慢的了，再慢就听不清了。" if direction == "slower" else "已经是最快的了。"
        else:
            setattr(provider, "speed", target)
            # 缓存里的音频是按旧语速合成的，不清掉这句会照旧速念。
            with self._tts_cache_lock:
                self._tts_cache.clear()
            reply = "好，我慢一点说。" if direction == "slower" else "好，我快一点说。"
        print(f"[SPEECH] 语速 {current:.2f} → {target:.2f}（{text}）", flush=True)
        audio = self._synthesize(reply)
        self._update(
            request_id,
            state="completed",
            response_text=reply,
            session_action="continue",
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
            result={
                "intent": {"intent": "speech_rate", "source": "rules"},
                "speech_rate": target,
            },
        )
        return True

    def _try_local_facts(self, request_id: str, text: str) -> bool:
        """时间/日期/天气这类「中枢自己就知道」的事实，别去排队问模型。

        实测走 bot 要 6.5 秒，回来的还是「我这边看不到实时时间」——
        本地答一句 0.8 秒就够了。天气同理：模型手里没有数据源，问它只会
        得到「我这边看不到」，这里直接查结构化接口。
        """
        from datetime import datetime

        from .weather import answer_weather

        normalized = "".join(str(text or "").split())
        if not normalized:
            return False
        now = datetime.now()
        reply = ""
        if any(word in normalized for word in ("几点", "什么时间", "现在时间", "多少点")):
            reply = f"现在是 {now.hour} 点 {now.minute} 分。"
        elif any(
            word in normalized
            for word in ("几号", "星期几", "周几", "今天日期", "今天是几", "什么日子")
        ):
            weekday = "一二三四五六日"[now.weekday()]
            reply = f"今天是 {now.month} 月 {now.day} 日，星期{weekday}。"
        else:
            # 查不到（网络不通 / 城市查不出）就返回空串，退回模型那条路，
            # 不要在这里硬编一句「看不到」，免得用户以为这功能又坏了。
            reply = answer_weather(normalized) or ""
        if not reply:
            return False
        audio = self._synthesize(reply)
        self._update(
            request_id,
            state="completed",
            response_text=reply,
            session_action="continue",
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
            result={"intent": {"intent": "local_fact", "source": "rules"}},
        )
        return True

    def _try_local_device(self, request_id: str, device_id: str, text: str) -> bool:
        """设备状态与音量设置的本地快通道。

        这两类都不需要模型：答案和值都在 ``DeviceRegistry`` 里，问一句
        「音量多少」去排 3~7 秒的模型队列毫无意义。优先级排在「点歌/播放
        控制」之后 —— 播放中的「大点声」已经被 MusicPlugin 接走了。
        """
        slots = _device_volume_slots(text)
        if slots is not None:
            intent_name = "device_set"
        elif _is_volume_question(text):
            slots = {"capability": "volume"}
            intent_name = "device_query"
        else:
            return False
        plugin = DevicePlugin(self.devices)
        result = plugin.handle(
            IntentResult(intent=intent_name, slots=slots, source="rules"),
            PluginContext(text=text, request_id=request_id, device_id=device_id),
        )
        reply = result.reply
        audio = self._synthesize(reply) if reply else None
        self._update(
            request_id,
            state="completed",
            response_text=reply,
            session_action=result.session_action,
            device_action=result.device_action,
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
            result={"intent": result.public()},
        )
        return True

    def _finish_fountain(self, request_id: str, fountain_action: str) -> None:
        """喷泉开关：先回确认语音，设备播完再真正开关（说话时不跳）。"""
        response_text = (
            "好的，音乐喷泉启动，跟着音乐跳起来。"
            if fountain_action == "fountain_start"
            else "音乐喷泉已关闭。"
        )
        audio = self._synthesize(response_text)
        self._update(
            request_id,
            state="completed",
            response_text=response_text,
            session_action=fountain_action,
            response_audio=audio[0] if audio else None,
            response_audio_format=audio[1] if audio else None,
            response_audio_content_type=audio[2] if audio else None,
        )

    def _handle_music_action(
        self,
        request_id: str,
        device_id: str,
        action_and_query: Tuple[str, str],
    ) -> None:
        action, query = action_and_query
        service = self.music

        def finish(response_text: str, session_action: str, **kwargs: Any) -> None:
            audio = kwargs.pop("with_audio", False)
            audio_payload = self._synthesize(response_text) if audio else None
            self._update(
                request_id,
                state="completed",
                response_text=response_text,
                session_action=session_action,
                response_audio=audio_payload[0] if audio_payload else None,
                response_audio_format=audio_payload[1] if audio_payload else None,
                response_audio_content_type=audio_payload[2] if audio_payload else None,
                **kwargs,
            )

        def mark(state: str) -> None:
            """音乐状态同时写进设备影子，回答「在不在放歌」时才不会瞎猜。"""
            service.set_state(device_id, state)
            self.devices.update_from_state(
                device_id, {"music": "idle" if state == "stopped" else state}
            )

        if action == "music_stop":
            mark("stopped")
            finish("音乐已停止。", "music_stop")
            return
        if action in ("music_volume_up", "music_volume_down"):
            if not service.has_session(device_id):
                finish("现在没有在放音乐。", "music_ignore")
                return
            # 音乐音量是设备自己那 4 档，中枢只跟踪：设备改完不回话，
            # 这里按动作推一步，查「音乐音量多少」时至少有数。
            profile = self.devices.get(device_id)
            base = 1 if profile.music_step is None else profile.music_step
            delta = 1 if action == "music_volume_up" else -1
            self.devices.update_from_state(
                device_id,
                {"music_step": max(0, min(3, base + delta)), "music": "playing"},
            )
            finish(
                "好的，声音调大一点。" if action == "music_volume_up" else "好的，声音小一点。",
                action,
            )
            return
        if action == "music_pause":
            if not service.has_session(device_id):
                finish("现在没有在放音乐。", "music_ignore")
                return
            mark("paused")
            finish("已经暂停。", "music_pause")
            return
        if action == "music_resume":
            if not service.has_session(device_id):
                finish("现在没有在放音乐。", "music_ignore")
                return
            mark("playing")
            finish("继续播放。", "music_resume")
            return
        if action == "music_replay":
            session = service.session(device_id)
            track = session.current() if session else None
            if track is None:
                finish("现在没有在放音乐。", "music_ignore")
                return
            mark("playing")
            finish(
                "从头再放一遍。",
                "music_replay",
                device_action=self._music_device_action(track),
                with_audio=True,
            )
            return
        if action == "music_next":
            track = None
            for _ in range(3):
                candidate = service.next_track(device_id)
                if candidate is None:
                    break
                try:
                    service.prepare(candidate)
                    track = candidate
                    break
                except MusicError:
                    continue
            if track is None:
                finish("后面没有歌了。", "music_ignore")
                return
            self.devices.update_from_state(device_id, {"music": "playing"})
            finish(
                f"换一首，《{track.display_title()}》。",
                "music_next",
                device_action=self._music_device_action(track),
                with_audio=True,
            )
            return

        # ---- 点歌 ----
        self._update(request_id, state="searching_music")
        try:
            candidates = service.search(query)
        except MusicError as error:
            finish(f"搜歌失败了：{error}", "music_ignore")
            return
        if not candidates:
            finish(
                "我没找到这首歌，你可以说得更具体一点。"
                if query
                else "想听什么歌？告诉我就好。",
                "music_ignore",
            )
            return
        # 用户点名了歌手（「枫 周杰伦」）就先按歌手收敛：
        # 网易云的搜索排序会把翻唱顶到原唱前面，不收敛就放成别人的版本。
        _title_hint, artist_hint = _split_song_query(query)
        if artist_hint:
            matched = [
                item for item in candidates if _artist_matches(item.artist, artist_hint)
            ]
            if matched:
                candidates = matched + [item for item in candidates if item not in matched]
        # 试前几首：网易云上不少歌是 VIP 版权，取不到直链就顺延到下一首
        track = None
        last_error = ""
        for candidate in candidates[:3]:
            try:
                service.prepare(candidate)
                track = candidate
                break
            except MusicError as error:
                last_error = str(error)
                continue
        if track is None:
            finish(f"这首歌放不了：{last_error}", "music_ignore")
            return
        index = candidates.index(track)
        service.begin_session(device_id, candidates, index)
        self.devices.update_from_state(device_id, {"music": "playing"})
        source_hint = "本地" if track.source == "local" else "网上"
        if artist_hint and not _artist_matches(track.artist, artist_hint):
            # 原唱拿不到时别糊弄：直接说清是别的版本，用户自己决定听不听
            response_text = f"网上没找到{artist_hint}的原唱，先放《{track.display_title()}》。"
        else:
            response_text = f"好的，{source_hint}找到《{track.display_title()}》，马上放。"
        finish(
            response_text,
            "music_play",
            device_action=self._music_device_action(track),
            with_audio=True,
        )

    @staticmethod
    def _music_device_action(track: Any) -> Dict[str, Any]:
        return {
            "action": "music_play",
            "track_id": track.track_id,
            "title": track.title,
            "duration": round(float(track.duration or 0.0), 1),
            "stream_path": f"/api/v1/portable/v1/music/{track.track_id}/stream",
        }

    @staticmethod
    def _public_media(media: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in media.items()
            if key != "data"
        }


def describe_audio_level(audio: bytes) -> Optional[str]:
    """录音电平过低时返回一句可读诊断，正常返回 None。

    2026-11-06 排查「说了没反应」时发现：设备上传的 6 秒录音里，人声只占 1 秒、
    峰值仅 -29 dBFS（正常说话应在 -12 dBFS 以上），Qwen ASR 在低信噪比下会
    幻听出完全无关的句子（同一段音频三次识别给出三句不同的话），随后被语音门控
    当成环境噪声丢掉。现场日志只有 ``[GATE] 忽略环境语音``，看不出根因在上游录音。
    这里把电平打进日志，让「麦克风没收到声音」和「识别听错」一眼可分。
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError):
        return None
    if sample_width != 2 or not frames:
        return None

    total = len(frames) // 2
    samples = struct.unpack(f"<{total}h", frames[: total * 2])
    peak = max(abs(value) for value in samples)
    if peak == 0:
        return "录音电平：全静音（峰值 0），麦克风未采集到任何声音"
    rms = math.sqrt(sum(value * value for value in samples) / total)
    peak_db = 20 * math.log10(peak / 32768)
    rms_db = 20 * math.log10(rms / 32768) if rms > 0 else -99.0
    duration = total / float(sample_rate or 1)

    # 采样重复：ES8311 的 pre_div 与 MCLK 不匹配时，ADC 实际出样率低于标称，
    # I2S 按 16 kHz 读走时每个采样被重复若干次（实测 pre_div 误写成 3 时，
    # 相邻采样重复率恰好 67% ≈ 2/3）。这种音频听起来「发闷、机械」，
    # ASR 一定幻听，而且跟音量无关——先查这条再看电平。
    repeated = _repeat_rate(samples, sample_rate)
    if repeated >= 25.0:
        return (
            f"录音存在采样重复：最响 1 秒内 {repeated:.0f}% 相邻采样完全相同"
            f"（peak={peak_db:.1f}dBFS 时长={duration:.2f}s）。"
            "说明 I2S/ES8311 时钟不匹配、ADC 实际出样率低于标称，"
            "音频被零阶保持重复，ASR 必然幻听。"
            "请检查 ES8311 寄存器 0x02（pre_div）是否对应实际 MCLK。"
        )

    if peak_db >= -20:
        return None
    return (
        f"录音电平偏低：peak={peak_db:.1f}dBFS rms={rms_db:.1f}dBFS "
        f"时长={duration:.2f}s（正常说话 peak 应高于 -20dBFS）。"
        "ASR 在低信噪比下会幻听出无关文字，请检查麦克风增益/距离。"
    )


# 固件会把录音峰值归一化到 0.7（≈ -3 dBFS），所以「纯底噪」也能顶到 -3dBFS，
# peak 已经不能判断有没有人说话，只有整段 RMS 还能区分。
# 实测（2026-11-06，pre_div 修好后）：正常说话 RMS 在 -14 ~ -25 dBFS；
# 只有底噪的那几条（ASR 识别成「嗯。」「咳。」）在 -34 dBFS 左右。
SILENT_RMS_DBFS = -31.0


def audio_silence_note(audio: bytes) -> Optional[str]:
    """整段录音只有底噪（没人说话）时返回原因，用来跳过这次 ASR。

    设备没有唤醒词，VAD 门限偏低，咳嗽、翻身、风扇都会触发一次上传。
    这些录音送进 Qwen ASR 不会返回空，而是「嗯。」「咳。」甚至
    「朝鲜族女性。」这种幻听 —— 既花了 0.7 秒和一次调用，又给下游的
    语音门控制造了假信号。能确定没人说话就别送。
    """
    decoded = _decode_pcm(audio)
    if decoded is None:
        return None
    samples, sample_rate = decoded
    if not samples:
        return None
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if rms <= 0:
        return "整段静音"
    rms_db = 20 * math.log10(rms / 32768)
    if rms_db >= SILENT_RMS_DBFS:
        return None
    duration = len(samples) / float(sample_rate or 1)
    return (
        f"整段只有底噪：rms={rms_db:.1f}dBFS 时长={duration:.2f}s"
        f"（低于 {SILENT_RMS_DBFS:.0f}dBFS，没有可识别的人声）"
    )


def _decode_pcm(audio: bytes):
    """把 wav 字节解成 (samples, sample_rate)，解不了返回 None。"""
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError):
        return None
    if sample_width != 2 or not frames:
        return None
    total = len(frames) // 2
    return struct.unpack(f"<{total}h", frames[: total * 2]), sample_rate


def _strip_flat_edges(samples, flat_run: int = 64):
    """去掉首尾的数字静音（连续相同且够长的平台段）。

    设备上传的 wav 常常是「0.17 秒全 0 + 人声」—— 采集刚起时缓冲区是零填充。
    这段静音对 ASR 无害，但会让「相邻采样重复率」虚高到 25% 以上。
    2026-11-06 就是被这条假告警带到「ES8311 时钟不匹配」的沟里，
    实际上其余 5 条录音的重复率都只有 1~3%，pre_div 也早改对了。
    """
    total = len(samples)
    if total < flat_run * 2:
        return samples
    head = 1
    while head < total and samples[head] == samples[0]:
        head += 1
    start = head if head >= flat_run else 0
    tail = 1
    while tail < total and samples[total - 1 - tail] == samples[total - 1]:
        tail += 1
    end = total - tail if tail >= flat_run and total - tail > start else total
    return samples[start:end]


def _repeat_rate(samples, sample_rate: int) -> float:
    """最响 1 秒窗口内的相邻采样重复率（百分数）。

    只看最响的一段，避免大段静音（采样几乎全为 0）把重复率算成 70%+，
    那是静音本身的性质，不是时钟问题。首尾的零填充平台先剥掉再算。
    """

    samples = _strip_flat_edges(samples)
    window = max(1, int(sample_rate))
    if len(samples) <= window * 2:
        span = samples
    else:
        step = max(1, window // 4)
        best_start = 0
        best_energy = -1
        for start in range(0, len(samples) - window + 1, step):
            chunk = samples[start : start + window]
            energy = sum(value * value for value in chunk)
            if energy > best_energy:
                best_energy = energy
                best_start = start
        span = samples[best_start : best_start + window]
    if len(span) < 2:
        return 0.0
    # 剩下的音频不足 0.35 秒就别下结论：样本太少，一两个平台段就能把
    # 比例拉到 30%，这种数字没有诊断价值，只会制造假警报。
    if len(span) < sample_rate * 0.35:
        return 0.0
    repeats = sum(1 for index in range(1, len(span)) if span[index] == span[index - 1])
    return repeats * 100.0 / (len(span) - 1)


def _normalize_wav_for_wearable(audio: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError) as error:
        raise PortableAudioError(
            "TTS_AUDIO_UNSUPPORTED",
            "RK3588 TTS 必须返回 PCM WAV 音频。",
        ) from error
    if channels not in {1, 2} or sample_width != 2 or sample_rate <= 0:
        raise PortableAudioError(
            "TTS_AUDIO_UNSUPPORTED",
            "RK3588 TTS 只支持 16 bit 单声道或双声道 WAV。",
        )
    import array

    samples = array.array("h")
    samples.frombytes(frames)
    if channels == 2:
        samples = array.array(
            "h",
            [
                max(-32768, min(32767, (samples[index] + samples[index + 1]) // 2))
                for index in range(0, len(samples) - 1, 2)
            ],
        )
    if sample_rate != 16000 and samples:
        target_count = max(1, round(len(samples) * 16000 / sample_rate))
        source_count = len(samples)
        resampled = array.array("h")
        for index in range(target_count):
            position = index * (source_count - 1) / max(1, target_count - 1)
            left = int(position)
            right = min(left + 1, source_count - 1)
            fraction = position - left
            value = round(samples[left] + (samples[right] - samples[left]) * fraction)
            resampled.append(max(-32768, min(32767, value)))
        samples = resampled
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(samples.tobytes())
    return output.getvalue()
