#!/usr/bin/env python3
"""本地假 sw-bot：把中枢到 bot 的那一段链路先跑通。

用途：真凭据到位之前，验证「中枢 → bot → 意图 → 插件 → 回设备」整条链。

它照 sw-bot 的接口形状实现，只做三件事：
  POST /api/auth/login            发 cookie
  GET  /api/bots                  返回一个叫「硬件」的 Bot
  POST /api/bots/{id}/messages    收一条消息，用 SSE 回一段文字

收到中枢的意图提示词时返回严格 JSON（{"intent","slots","reply"}），
收到普通聊天时返回一句人话 —— 这样能分清是分类调用还是闲聊调用。

    python tools/mock_swbot.py --port 8899

然后把中枢指过来：

    SWBOT_BASE_URL=http://127.0.0.1:8899 SWBOT_EMAIL=mock@local \
    SWBOT_PASSWORD=mock SWBOT_BOT=硬件 \
    python run.py --host 0.0.0.0 --port 18787 --swbot --local-tts
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

BOTS = [
    {"id": "bot-hardware", "name": "硬件", "description": "本地假 bot，用于验证链路"},
    {"id": "bot-pet", "name": "桌宠", "description": "本地假 bot 备选"},
]

TRIGGERS = ("放一首", "放一个", "放个", "放点", "来一首", "来一个", "来点", "来首",
            "唱一首", "唱首", "点歌", "点一首", "播放", "放一下", "给我放",
            "我想听", "想听", "听一首", "听首")

STOP_WORDS = ("关", "停", "结束", "退出", "别放", "收起")

CHAT_REPLIES = (
    "我在，链路是通的。你刚说的是「{text}」。",
    "听到了：「{text}」。这是本地假 bot 在答。",
    "「{text}」——收到，我是假 bot，真 bot 接上就换成它的回答。",
)


def classify(text: str) -> Dict[str, Any]:
    """假 bot 的意图判定：故意用关键词，重点是验证 JSON 通道而不是智商。"""
    normalized = re.sub(r"[\s，。！？、,.!?~～]", "", text)
    if "喷泉" in normalized:
        if any(word in normalized for word in STOP_WORDS):
            return {"intent": "fountain_stop", "reply": "好，喷泉关了。"}
        return {"intent": "fountain_start", "reply": "好，喷泉开起来。"}
    if any(word in normalized for word in ("退下", "休息", "待机", "别说话")):
        return {"intent": "sleep", "reply": "好，我先待命。"}
    if any(word in normalized for word in ("暂停", "停一下")):
        return {"intent": "music_pause", "reply": "已经暂停。"}
    if any(word in normalized for word in ("继续", "接着放")):
        return {"intent": "music_resume", "reply": "继续放。"}
    if any(word in normalized for word in ("下一首", "换一首", "切歌")):
        return {"intent": "music_next", "reply": "换下一首。"}
    if any(word in normalized for word in ("重播", "再放一遍", "从头放")):
        return {"intent": "music_replay", "reply": "从头再放一遍。"}
    if any(word in normalized for word in ("停止播放", "关闭音乐", "别放了", "不听了")):
        return {"intent": "music_stop", "reply": "好，音乐停了。"}
    if any(word in normalized for word in ("大声", "调大", "太小")):
        return {"intent": "music_volume_up", "reply": "声音调大一点。"}
    if any(word in normalized for word in ("小声", "调小", "太吵")):
        return {"intent": "music_volume_down", "reply": "声音调小一点。"}

    trigger = next((word for word in TRIGGERS if word in normalized), None)
    if trigger is not None:
        query = normalized.split(trigger, 1)[1]
        for filler in ("的音乐", "的歌", "一首", "一个", "吧", "啊", "呗"):
            query = query.replace(filler, "")
        query = query.strip()
        return {
            "intent": "music_play",
            "slots": {"query": query},
            "reply": f"好，放{query}。" if query else "好，随便放一首。",
        }

    if any(word in normalized for word in ("天气", "几度", "新闻", "价格", "比分", "汇率")):
        return {"intent": "search", "reply": f"关于「{text}」，假 bot 查不到实时数据。"}

    reply = CHAT_REPLIES[len(normalized) % len(CHAT_REPLIES)].format(text=text)
    return {"intent": "chat", "reply": reply}


def answer(message: str) -> str:
    """返回 SSE 的最终文本。

    中枢的意图提示词里带「意图路由器」和「用户说：」两个标记，
    据此判断这次是分类调用还是闲聊调用。
    """
    if "意图路由器" in message:
        match = re.search(r"用户说：(.*)$", message, re.S)
        user_text = (match.group(1).strip() if match else message).strip()
        payload = classify(user_text)
        return json.dumps(payload, ensure_ascii=False)
    return f"我在，链路是通的。你说的是「{message[:20]}」。"


class Handler(BaseHTTPRequestHandler):
    server_version = "MockSwBot/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础 ----------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        print(f"[mock-swbot] {fmt % args}", flush=True)

    def _json(self, status: int, payload: Any, cookie: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", "swbot_session=mock; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        return "swbot_session=mock" in (self.headers.get("Cookie") or "")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------- 路由 ----------------

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/auth/login":
            payload = self._read_json()
            email = str(payload.get("email") or "")
            print(f"[mock-swbot] 登录：{email}", flush=True)
            self._json(200, {"ok": True, "user": {"email": email}}, cookie=True)
            return
        if self.path.startswith("/api/bots/") and self.path.endswith("/messages"):
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            self._stream_answer(self._read_json())
            return
        self._json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/auth/me":
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"user": {"email": "mock@local", "name": "假bot"}})
            return
        if self.path == "/api/bots":
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, BOTS)
            return
        if self.path.startswith("/api/bots/") and self.path.endswith("/messages"):
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"messages": []})
            return
        self._json(404, {"error": "not found"})

    # ---------------- SSE ----------------

    def _stream_answer(self, payload: Dict[str, Any]) -> None:
        message = str(payload.get("text") or "")
        print(f"[mock-swbot] 收到（{len(message)} 字）：{message[:60]!r}", flush=True)
        # 假装思考一下，让链路的延迟和真 bot 接近
        time.sleep(0.35)
        text = answer(message)
        attempt = uuid.uuid4().hex[:8]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        for chunk in (text[: max(1, len(text) // 2)], text[max(1, len(text) // 2) :]):
            if not chunk:
                continue
            event = {"type": "delta", "attempt": attempt, "text": chunk}
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        done = {"type": "done", "attempt": attempt, "text": text}
        self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()
        print(f"[mock-swbot] 回复：{text[:80]}", flush=True)
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假 sw-bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[mock-swbot] 监听 http://{args.host}:{args.port}", flush=True)
    print("[mock-swbot] Bot 列表：硬件 / 桌宠", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
