"""桌宠的本机（这台 Mac）控制：说「打开WPS」就真的打开。

为什么要有这个模块
------------------
2026-11-10 用户：对桌宠说「帮我打开WPS」，它回「我这边没法打开电脑上的软件」——
因为中枢一直只指挥**外设**（录音豆、喷泉、音乐），从来没把这台跑着中枢的
Mac 本身当成被控对象。这个模块补上这一环：桌宠的意图插件直接在**本机**
执行打开/退出应用、打开网址，动作在这台 Mac 上立刻生效。

设计边界（安全）：
- 只做「打开/退出应用、打开网址」三个动作，白名单别名表 + `open` 系命令，
  **不接任意 shell**——这句话是语音进来的，ASR 抖一下不能变成执行任意命令。
- 每次动作写一行 jsonl 日志（.data/host_actions.jsonl），控制页要展示历史。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

# 别名表：语音里喊的叫法 → open -a 认的应用名（键已归一化：小写、去空格）
APP_ALIASES: Dict[str, str] = {
    "wps": "wpsoffice",
    "wpsoffice": "wpsoffice",
    "wps office": "wpsoffice",  # 归一化后是 "wpsoffice"，这条保险
    "文档": "wpsoffice",
    "微信": "wechat",
    "wechat": "wechat",
    "企业微信": "企业微信",
    "wecom": "企业微信",
    "qq": "qq",
    "谷歌浏览器": "google chrome",
    "googlechrome": "google chrome",
    "chrome": "google chrome",
    "浏览器": "safari",
    "safari": "safari",
    "苹果浏览器": "safari",
    "edge": "microsoft edge",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "code": "visual studio code",
    "微信开发者工具": "wechatwebdevtools",
    "计算器": "calculator",
    "备忘录": "notes",
    "日历": "calendar",
    "音乐": "music",
    "终端": "terminal",
    "系统设置": "system settings",
    "设置": "system settings",
    "飞书": "lark",
    "lark": "lark",
    "百度网盘": "baidunetdisk_mac",
    "网盘": "baidunetdisk_mac",
    "blender": "blender",
    "docker": "docker",
    "chatgpt": "chatgpt",
    "claude": "claude",
    "obsidian": "obsidian",
    "嘉立创eda": "嘉立创eda(专业版)",
    "eda": "嘉立创eda(专业版)",
    "workbuddy": "workbuddy",
}

# 看起来像网址的目标（有域名点、没有空格、不含中文）就走浏览器
_DOMAIN_RE = re.compile(
    r"^(https?://)?[a-z0-9][a-z0-9.-]*\.(com|cn|net|org|io|cc|xyz|top|vip|app|dev|ai|me)(/[^\s]*)?$",
    re.IGNORECASE,
)

_LOG_PATH = Path(".data/host_actions.jsonl")
_recent: deque = deque(maxlen=50)


def _normalize(target: str) -> str:
    return str(target or "").strip().lower().replace(" ", "")


def looks_like_url(target: str) -> bool:
    cleaned = str(target or "").strip()
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return True
    return bool(_DOMAIN_RE.match(cleaned)) and " " not in cleaned


def resolve_app(target: str) -> str:
    """喊法 → open -a 认的应用名；认不出就原样返回（用户可能念的是英文应用名）。"""
    key = _normalize(target)
    if not key:
        return ""
    return APP_ALIASES.get(key) or APP_ALIASES.get(str(target).strip().lower()) or str(target).strip()


def _log(action: str, target: str, ok: bool, message: str) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "target": target,
        "ok": ok,
        "message": message,
    }
    _recent.append(entry)
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志写失败不影响动作本身


def _run_open(args: List[str]) -> bool:
    try:
        completed = subprocess.run(
            args, capture_output=True, timeout=10, text=True
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def open_app(target: str) -> Dict[str, Any]:
    """`open -a` 打开应用。失败时兜底按网址处理（「打开bilibili.com」）。"""
    if looks_like_url(target):
        return open_site(target)
    name = resolve_app(target)
    if not name:
        ok, message = False, "你要打开哪个应用？说一下名字。"
        _log("open_app", target, ok, message)
        return {"ok": ok, "message": message}
    if _run_open(["open", "-a", name]):
        ok, message = True, f"好的，正在打开{name}。"
    else:
        ok, message = False, f"电脑上没找到「{target}」，没打开成。"
    _log("open_app", target, ok, message)
    return {"ok": ok, "message": message}


def _bundle_main_pids(name: str) -> List[int]:
    """找出该应用主可执行文件的进程号（*.app/Contents/MacOS/*）。"""
    pattern = f"{re.escape(name)}.app/Contents/MacOS"
    try:
        completed = subprocess.run(
            ["pgrep", "-if", pattern], capture_output=True, timeout=5, text=True
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(pid) for pid in completed.stdout.split() if pid.isdigit()]


def quit_app(target: str) -> Dict[str, Any]:
    name = resolve_app(target)
    if not name:
        ok, message = False, "你要退出哪个应用？说一下名字。"
        _log("quit_app", target, ok, message)
        return {"ok": ok, "message": message}

    # 第一优先：AppleScript 正常退出（会走应用的保存/收尾流程）。
    # 2026-11-10 实测：后台服务发 Apple Events 会被 TCC 拦（-10004 权限违例，
    # 且没法弹授权窗），所以失败不报错，静默落到第二优先。
    apple_script_ok = False
    try:
        completed = subprocess.run(
            ["osascript", "-e", f'quit app "{name}"'],
            capture_output=True,
            timeout=10,
            text=True,
        )
        apple_script_ok = completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        apple_script_ok = False

    # 第二优先：对本机进程直接发 SIGTERM（温和退出，不走任何权限弹窗）
    pids = _bundle_main_pids(name)
    for pid in pids:
        try:
            subprocess.run(["kill", "-TERM", str(pid)], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if pids:
        time.sleep(2.0)
        leftovers = _bundle_main_pids(name)
        ok = apple_script_ok or not leftovers
        if ok:
            message = f"好，{name}已经退出了。"
        else:
            message = f"{name}退得不干净，还剩 {len(leftovers)} 个进程没退。"
        _log("quit_app", target, ok, message)
        return {"ok": ok, "message": message}

    ok = apple_script_ok
    message = f"好，{name}已经退出了。" if ok else f"{name}好像没在运行。"
    _log("quit_app", target, ok, message)
    return {"ok": ok, "message": message}


def open_site(target: str) -> Dict[str, Any]:
    url = str(target or "").strip()
    if not url:
        ok, message = False, "要打开哪个网站？说一下网址。"
        _log("open_site", target, ok, message)
        return {"ok": ok, "message": message}
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    if _run_open(["open", url]):
        ok, message = True, f"好，已经在浏览器里打开{url.split('//', 1)[-1]}。"
    else:
        ok, message = False, "网址没打开成。"
    _log("open_site", target, ok, message)
    return {"ok": ok, "message": message}


def execute(action: str, target: str) -> Dict[str, Any]:
    """统一入口：intent 插件和 HTTP 控制页都走这里。"""
    action = str(action or "").strip()
    if action in ("open_app", "open"):
        return open_app(target)
    if action in ("quit_app", "close", "quit"):
        return quit_app(target)
    if action in ("open_site", "open_url"):
        return open_site(target)
    return {"ok": False, "message": f"不认识的动作：{action}"}


# ---- 语音快通道解析 -----------------------------------------------------
# 「打开WPS」这种指令不该过模型（模型今天 3~10 秒才回话），命中别名表/
# 网址就本地直执行。白名单外的目标一律返回 None 交给模型兜底，
# 保证「打开我的心」（ASR 抖动）不会误开程序。

_OPEN_WORDS = ("打开", "开启", "启动", "运行", "开一下", "开个")
_QUIT_WORDS = ("退出", "关闭", "关掉", "退掉", "停掉")
_PREFIX_WORDS = ("麻烦你", "麻烦", "请你", "帮我", "给我", "请")
# 这些目标不是这台 Mac 的事（喷泉/放歌归设备，灯/空调归外设），别抢
_NON_HOST_MARKS = ("喷泉", "音乐", "歌", "灯", "空调", "电视", "录音", "豆")


def parse_voice_command(text: str) -> Optional[tuple]:
    """从一句语音文本里解析出本机控制指令。

    返回 (action, target) 或 None（不是本机指令 / 目标不在白名单）。
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    for prefix in _PREFIX_WORDS:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    action = None
    for word in _OPEN_WORDS:
        if cleaned.startswith(word):
            action, cleaned = "open_app", cleaned[len(word):].strip()
            break
    if action is None:
        for word in _QUIT_WORDS:
            if cleaned.startswith(word):
                action, cleaned = "quit_app", cleaned[len(word):].strip()
                break
    if action is None:
        return None
    # 去动词后的填充词（「打开一下微信」）和尾部语气词（「打开WPS了」）
    for filler in ("一下", "一个", "个", "吧"):
        while cleaned.startswith(filler):
            cleaned = cleaned[len(filler):].strip()
    for tail in ("一下", "了", "吧", "呗", "它"):
        while cleaned.endswith(tail):
            cleaned = cleaned[: -len(tail)].strip()
    if not cleaned:
        return None
    if any(mark in cleaned for mark in _NON_HOST_MARKS):
        return None  # 喷泉/音乐/灯……不是本机的事，交给对应通道
    known = _normalize(cleaned) in APP_ALIASES or looks_like_url(cleaned)
    if not known:
        return None  # 白名单外：宁可慢一点也不乱开
    return (action, cleaned)


def recent(count: int = 20) -> List[Dict[str, Any]]:
    return list(_recent)[-count:]
