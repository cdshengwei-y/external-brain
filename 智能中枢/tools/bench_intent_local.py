"""用真实的意图分类提示词打本地模型，看它能不能当第③步用。

    python3 tools/bench_intent_local.py

要验证两件事：
1. 延迟（这一步的耗时就是设备端「等我说话」的全部体验）；
2. 输出是不是严格 JSON —— 分类器一旦吐出多余的话，parse_intent_reply 就退关键词。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_gateway.intent import build_intent_prompt, parse_intent_reply  # noqa: E402

BASE = "http://192.168.0.245:8888/v1"
MODEL = "deepseek-v4.1-flash"
KEY = "sk-local"

# 与 portable.py 注册的插件保持一致，只写意图名和一句话说明
CATALOG = """- chat：闲聊、问答、解释、聊天
- fountain：音乐喷泉开关
- music：放歌、暂停、下一首
- device：音量、屏幕、待机
- record_status：查录音豆状态、录音进度
- sleep：退下、别说话、去睡觉"""

CASES = [
    "现在几点了",
    "把喷泉打开",
    "录音豆还剩多少电",
    "你先退下吧",
    "这就是放歌的，然后这周围就是。",
]


def ask(text: str) -> tuple[float, str]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": build_intent_prompt(text, CATALOG)}
        ],
        "stream": False,
        "max_tokens": 300,
        "temperature": 0,
    }
    request = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    elapsed = time.perf_counter() - started
    return elapsed, body["choices"][0]["message"]["content"]


def main() -> int:
    ok = 0
    for text in CASES:
        try:
            elapsed, raw = ask(text)
        except Exception as error:  # noqa: BLE001
            print(f"  [{text}] 调用失败：{error}")
            continue
        parsed = parse_intent_reply(raw, default_intent="chat", source="bench")
        hit = parsed is not None
        ok += 1 if hit else 0
        shown = raw.replace("\n", " ")[:70]
        print(f"  {elapsed:5.2f}s  {'JSON✓' if hit else 'JSON✗'}  [{text}]  {shown}")
    print(f"\n可用率 {ok}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
