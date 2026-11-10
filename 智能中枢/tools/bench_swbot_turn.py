"""用中枢自己的 SwBotClient 量一遍「现在这条链路」到底多慢。

    python3 tools/bench_swbot_turn.py            # 默认一条无害问题
    python3 tools/bench_swbot_turn.py --text "现在几点了"

只做一次 round-trip，读环境变量里的 sw-bot 账号（source config/plugin.env 后运行）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_gateway.swbot import SwBotError, build_client_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="你好，简单介绍一下你自己")
    parser.add_argument("--bot", default=None)
    args = parser.parse_args()

    client = build_client_from_env()
    if client is None:
        print("环境变量里没有 sw-bot 账号（需要 SWBOT_BASE_URL / SWBOT_EMAIL）")
        return 1

    print(f"base={client.base_url} bot={args.bot or client.bot_hint or '(默认)'}")
    print(f"prompt={args.text}")
    started = time.perf_counter()
    try:
        reply = client.send(args.text, bot_id=args.bot)
    except (SwBotError, Exception) as error:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        print(f"失败  耗时 {elapsed:.2f}s  {type(error).__name__}: {error}")
        return 1
    elapsed = time.perf_counter() - started
    print(f"成功  耗时 {elapsed:.2f}s  {len(reply)}字")
    print(f"回复：{reply[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
