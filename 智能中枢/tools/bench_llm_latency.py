"""对比「本地 DeepSeek」与「中转平台」在桌宠语音场景下的首字延迟/总耗时。

    python3 tools/bench_llm_latency.py                 # 只测本地
    python3 tools/bench_llm_latency.py --repeat 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request

LOCAL_BASE = "http://192.168.0.245:8888/v1"
LOCAL_MODEL = "deepseek-v4.1-flash"
LOCAL_KEY = "sk-local"

# sub2api / sw-bot 这类站点前置了 WAF，非浏览器 UA 会被 403 挡掉。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

PREFIX = "（语音输入，请用口语、一两句话回答，不要用 markdown）"

PROMPTS = [
    "今天天气怎么样",
    "能不能查到关于录音豆的信息？",
    "这就是放歌的，然后这周围就是。",
    "帮我把灯关掉",
    "现在几点了",
]


def stream_once(base: str, model: str, key: str, prompt: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PREFIX + prompt}],
        "stream": True,
        "max_tokens": 200,
        "temperature": 0.3,
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": BROWSER_UA,
            "Accept": "text/event-stream",
        },
    )
    started = time.perf_counter()
    ttft = None
    pieces: list[str] = []
    usage = {}
    with urllib.request.urlopen(request, timeout=120) as response:
        for raw in response:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    if ttft is None:
                        ttft = time.perf_counter() - started
                    pieces.append(text)
    total = time.perf_counter() - started
    out = "".join(pieces)
    completion_tokens = usage.get("completion_tokens") or 0
    return {
        "ttft": ttft or total,
        "total": total,
        "chars": len(out),
        "tokens": completion_tokens,
        "text": out,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=LOCAL_BASE)
    parser.add_argument("--model", default=LOCAL_MODEL)
    parser.add_argument("--key", default=LOCAL_KEY)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    all_ttft: list[float] = []
    all_total: list[float] = []
    print(f"endpoint={args.base} model={args.model} repeat={args.repeat}\n")
    for prompt in PROMPTS:
        for index in range(args.repeat):
            try:
                result = stream_once(args.base, args.model, args.key, prompt)
            except Exception as error:  # noqa: BLE001
                print(f"  [{prompt}] 失败：{error}")
                continue
            all_ttft.append(result["ttft"])
            all_total.append(result["total"])
            preview = result["text"].replace("\n", " ")[:46]
            print(
                f"  ttft={result['ttft']*1000:6.0f}ms  总={result['total']:5.2f}s  "
                f"{result['chars']:3d}字  {preview}"
            )
    if all_ttft:
        print()
        print(
            f"首字延迟  中位 {statistics.median(all_ttft)*1000:.0f}ms  "
            f"最慢 {max(all_ttft)*1000:.0f}ms"
        )
        print(
            f"整段耗时  中位 {statistics.median(all_total):.2f}s  "
            f"最慢 {max(all_total):.2f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
