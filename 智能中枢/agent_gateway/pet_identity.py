"""桌宠的身份：它叫什么名字，由谁起的。

为什么单独抽一个模块
--------------------
2026-11-06 真机实测：用户问「你叫什么？」，它答「我是你的桌面小宠物，
你叫我小桌就行。」—— 唤醒词明明是「贾维斯」，自称却是另一个名字；
换成备用模型 gpt-6-luna 更离谱，直接答「我叫 ChatGPT」。

根因是系统提示词里**根本没有身份**，模型只能拿自己的出厂人格凑。
而用户给的那句「你应该叫幻形之了」只活在聊天历史里，中枢一重启就丢。

所以名字要有三件事：
1. 默认值（贾维斯）—— 与唤醒词对齐，不问也知道自己是谁；
2. 可改写 —— 用户说「以后叫你 X」就真的改名；
3. 落盘 —— 重启后还是 X，不然「你叫什么」每轮都翻车。

存放位置跟着 ``HUB_DATA_DIR`` 走，默认 ``.data/pet_identity.json``。
读写都是原子替换，中途断电不会留下半个文件。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

# 默认名就是唤醒词。用户原话：「我都是用贾维斯唤醒他的，他就这么叫」——
# 所以名字不是随便起的，它是唤醒词在对话里的人格化。两处必须同源：
# 改唤醒词（PET_WAKE_WORDS）时，没改过名的桌宠自称也跟着变，不会出现
# 「喊贾维斯叫醒了一个自称小桌的东西」这种错位。
DEFAULT_ASSISTANT_NAME = "贾维斯"

# 名字的合法形状：2~4 个汉字，或 2~12 个字母数字（允许「小 J」这种）。
_NAME_CJK = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")
_NAME_LATIN = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{1,11}$")

# ASR 常把「叫什么」这类问句拼出这些词，别把它们当成新名字。
_BAD_NAMES = {
    "什么", "啥", "啥呀", "谁", "哪个", "什么名", "什么名字", "名字",
    "我吗", "你吗", "自己", "这个", "那个", "一下", "一声",
}

# 四种「改名」句式。收紧到必须带「你」或「给你」，否则「我叫小明」
# 会被当成给自己改名 —— 那是用户名，不是桌宠名。
_RENAME_PATTERNS = (
    # 以后就叫你小明 / 从现在开始喊你小灰
    re.compile(r"(?:以后|今后|从现在开始|从今天起)(?:就)?(?:叫你|喊你|称你为?|管你叫)(?P<name>.{2,6})$"),
    # 你就叫小明吧 / 你的名字叫小明
    re.compile(r"你(?:以后|今后)?(?:就)?(?:叫做?|的名字是|名字叫)(?P<name>.{2,6}?)(?:了|啦|吧|啊|呀|哈|哦)?$"),
    # 给你起个名字叫小明
    re.compile(r"(?:给你|帮你)(?:起|取|想)(?:个|一个)?名(?:字)?(?:叫|是)(?P<name>.{2,6})$"),
    # 你应该叫小明了 / 你应该叫小明吧
    re.compile(r"你应该(?:叫做?|是)(?P<name>.{2,6}?)(?:了|啦|吧|啊|呀|哈|哦)?$"),
)

# 「改回默认」的句式。必须排在改名之前判：否则「还是叫贾维斯吧」会被
# 「你就叫 X」那族规则吃掉，虽然结果也是贾维斯，但语义上少了「回滚」这件事。
_RESET_PATTERNS = (
    re.compile(r"(?:恢复|还原|改回|叫回|用回|换回)(?:到)?(?:默认|原来|原本|原名|原样|以前|最初)(?:的)?(?:名字|称呼)?"),
    re.compile(r"你?还是(?:叫|用|叫回)(?:默认|原名|原来)"),
    re.compile(r"(?:名字|称呼)(?:改|换|恢复)(?:回|成|到)(?:默认|原来|原名)"),
    re.compile(r"别再叫什么.{0,6}了.{0,6}(?:叫|用)(?:默认|原名)"),
)
# 结尾的语气词，剥掉再判名字。
_TAIL_PARTICLES = ("了", "啦", "吧", "啊", "呀", "哈", "哦", "呢", "嘛")

_lock = threading.RLock()
_cache: Optional[dict] = None
_cache_stamp: Optional[float] = None


def _stat_stamp() -> Optional[float]:
    try:
        return store_path().stat().st_mtime
    except OSError:
        return None


def wake_word() -> str:
    """用户喊哪句话能把它叫醒。

    ``PET_WAKE_WORDS`` 配了就以第一个为准（那是用户自己加的叫法），
    没配就是内置的「贾维斯」。名字默认值跟着它走。
    """
    raw = os.environ.get("PET_WAKE_WORDS") or ""
    for piece in raw.replace("，", ",").split(","):
        candidate = piece.strip()
        if candidate:
            return _clean_name(candidate) or candidate
    return DEFAULT_ASSISTANT_NAME


def is_default_name(name: str) -> bool:
    return _clean_name(name) == wake_word()


def store_path() -> Path:
    """身份文件的落盘位置。测试用 ``PET_IDENTITY_PATH`` 指到临时目录。"""
    override = os.environ.get("PET_IDENTITY_PATH")
    if override:
        return Path(override)
    data_dir = os.environ.get("HUB_DATA_DIR") or ".data"
    return Path(data_dir) / "pet_identity.json"


def _load() -> dict:
    global _cache, _cache_stamp
    with _lock:
        # 缓存带 mtime 校验：同机别的进程（工具脚本、另一份中枢）改了名字，
        # 这边下一次读就能看见，不必重启。实测踩过：命令行改名回「贾维斯」，
        # 跑着的中枢还拿着旧的「小灰」，问"你叫什么"答错。
        stamp = _stat_stamp()
        if _cache is not None and stamp == _cache_stamp:
            return _cache
        path = store_path()
        payload: dict = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError):
            # 文件不存在 / 被写坏：都当没配过，用默认名字，不要因此起不来。
            payload = {}
        _cache = payload
        _cache_stamp = stamp
        return _cache


def _write(payload: dict) -> None:
    global _cache, _cache_stamp
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
    with _lock:
        _cache = payload
        _cache_stamp = _stat_stamp()


def assistant_name() -> str:
    """当前名字；没改过就是唤醒词本身（默认「贾维斯」）。"""
    value = str(_load().get("assistant_name") or "").strip()
    return value or wake_word()


def set_assistant_name(name: str) -> bool:
    """改名并落盘。名字不合法就什么都不做，返回 False。

    改回默认名时把配置里的键**删掉**而不是写入同名字符串 ——
    这样 ``pet_identity.json`` 回到 ``{}``，语义上就是「没改过名」，
    以后改唤醒词它还会跟着变；留一个写死的「贾维斯」反而把默认值钉死了。
    """
    candidate = _clean_name(name)
    if not candidate:
        return False
    payload = dict(_load())
    current = payload.get("assistant_name")
    if candidate == wake_word():
        if current is None:
            return False
        payload.pop("assistant_name", None)
        try:
            _write(payload)
        except OSError:
            return False
        return True
    if current == candidate:
        return False
    payload["assistant_name"] = candidate
    try:
        _write(payload)
    except OSError:
        return False
    return True


def reset_assistant_name() -> bool:
    """恢复默认名（= 唤醒词）。本来就没改过则返回 False。"""
    payload = dict(_load())
    if payload.get("assistant_name") is None:
        return False
    payload.pop("assistant_name", None)
    try:
        _write(payload)
    except OSError:
        return False
    return True


def _clean_name(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    text = text.strip("。.，,！!？?、~～…—-《》\"'“”‘’()（） 　")
    for particle in _TAIL_PARTICLES:
        if text.endswith(particle) and len(text) > len(particle) + 1:
            text = text[: -len(particle)]
    text = text.strip("。.，,！!？?、~～…—- 　")
    if not text or text in _BAD_NAMES:
        return None
    if _NAME_CJK.match(text) or _NAME_LATIN.match(text):
        return text
    return None


def detect_reset(text: str) -> bool:
    """这句话是不是在要求「改回默认名字」。

    2026-11-06：一轮真机对话里 ASR 把一句话听成「你应该叫幻形之了」，
    名字就此被改成「幻形之」，而用户根本不知道该怎么改回来。
    既然名字能改，就必须有确定性的回滚路径，不能只靠再念一遍新名字。
    """
    normalized = "".join(str(text or "").split())
    normalized = normalized.strip("。.，,！!？?、~～…—- ")
    if not normalized or len(normalized) > 30:
        return False
    if any(word in normalized for word in ("什么", "啥", "谁", "哪", "吗", "？", "?")):
        return False
    if any(pattern.search(normalized) for pattern in _RESET_PATTERNS):
        return True
    # 点名回滚：「你还是叫贾维斯吧」「叫回贾维斯」。
    # 默认名可能被 PET_WAKE_WORDS 改过，所以这一句不能写死进正则。
    word = wake_word()
    if word and word in normalized:
        if any(token in normalized for token in ("还是", "改回", "叫回", "换回", "恢复", "改叫")):
            return True
    return False


def detect_rename(text: str) -> Optional[str]:
    """这句话是不是在给桌宠改名？是就返回新名字，否则 None。

    只认「叫你 / 你就叫 / 给你起名 / 你应该叫」这几种明确句式。
    ASR 会把「你叫什么」识别成各种形状，但那些都带疑问词且不以上面的
    句式收尾，因此不会误判 —— 误改名的代价比漏改一次大得多。
    """
    normalized = "".join(str(text or "").split())
    normalized = normalized.strip("。.，,！!？?、~～…—- ")
    if not normalized:
        return None
    if any(word in normalized for word in ("什么", "啥", "谁", "哪", "吗", "？", "?")):
        # 「你叫什么」「你应该叫什么」都是在问，不是在下指令。
        return None
    for pattern in _RENAME_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        name = _clean_name(match.group("name"))
        if name:
            return name
    return None


def identity_line() -> str:
    """塞进系统提示词的一句身份说明。空名字时返回空串。"""
    name = assistant_name()
    if not name:
        return ""
    line = (
        f"你叫「{name}」，用户就是这么喊你的。被问到名字、自我介绍时，"
        f"只说这个名字；不要说自己叫 ChatGPT、DeepSeek、通义千问、"
        f"任何大模型或任何别的名字。"
    )
    call_word = wake_word()
    if call_word and call_word != name:
        # 改过名的情况：唤醒词由固件/ASR 决定，改不了，得把它讲清楚，
        # 否则模型会在「你叫什么」和「叫醒你的是谁」之间自相矛盾。
        line += f"（用户喊「{call_word}」叫醒你，那是唤醒词；你的名字是「{name}」。）"
    return line


def reset_cache() -> None:
    """测试用：丢掉内存缓存，下次重新读盘。"""
    global _cache, _cache_stamp
    with _lock:
        _cache = None
        _cache_stamp = None
