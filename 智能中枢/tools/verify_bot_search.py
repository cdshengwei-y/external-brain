#!/usr/bin/env python3
"""验证 Bot 平台的联网搜索是否真的可用。

背景：中枢的 search 意图是「透传给 Bot」的空壳（用户决策：用 Bot 平台
自带的搜索插件）。但 Bot 平台有没有装搜索插件没人验证过 —— 没装的话
Bot 只会顺着用户的话编答案（实测发生过：先说「能查天气」再问就说不知道）。

判定标准：拿两个有确定答案的实时性问题问它 ——
- 回复含具体数值/事实 → 搜索在线；
- 空承诺（「我这就去查」「稍等」）、说看不到、或编出离谱数据 → 不在线。

用法：
    set -a; source config/plugin.env; set +a
    python3 tools/verify_bot_search.py
退出码：0=搜索可用，1=不可用（去 Bot 平台给「桌宠」装搜索插件）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_gateway.swbot import SwBotIntentClassifier, build_client_from_env

# 问题挑「答案随时间变、但当天网上到处都是」的，编不出来也蒙不准
PROBES = [
    ("成都今天的气温大概是多少度？一句话回答", "温度数值（如 16 度 / 28°C）"),
    ("美元兑人民币汇率现在是多少？只要数字", "6-8 之间的数字"),
]

PROMISE_WORDS = (
    "我这就", "稍等", "让我查", "无法", "看不到", "没办",
    "不能联网", "没有联网", "查询不了", "无法访问",
)


def looks_real(text: str, hint: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if any(word in normalized for word in PROMISE_WORDS):
        return False
    if "度" in hint or "温度" in hint:
        # 温度：匹配 6 ~ 45 之间的数字
        return bool(re.search(r"(?<!\d)([1-9]|[1-3]\d|4[0-5])(?!\d)", normalized))
    if "汇率" in hint:
        return bool(re.search(r"[6-8]\.\d{1,4}", normalized))
    return len(normalized) >= 6


def main() -> int:
    client = build_client_from_env()
    if client is None:
        print("未配置 SWBOT_EMAIL / SWBOT_BASE_URL（plugin.env），无法验证。")
        return 2
    classifier = SwBotIntentClassifier(client)
    ok_count = 0
    try:
        for question, hint in PROBES:
            print(f"问：{question}")
            try:
                result = classifier.classify(question, "- search：查实时信息并给出答案")
            except Exception as error:  # noqa: BLE001 - 探测脚本要兜住一切
                print(f"  调用失败：{error}\n")
                continue
            reply = getattr(result, "reply", "") or ""
            print(f"答：{reply}")
            if looks_real(reply, hint):
                print("  ✓ 像真实数据\n")
                ok_count += 1
            else:
                print("  ✗ 不像真实数据（空承诺 / 拒答 / 编造）\n")
    finally:
        client.close() if hasattr(client, "close") else None

    print("=" * 40)
    if ok_count == len(PROBES):
        print("Bot 平台搜索在线：search 意图可以放心透传。")
        return 0
    if ok_count == 0:
        print("Bot 平台搜索不可用：请到 bot.vx123.xin 给「桌宠」装搜索插件，")
        print("装好后重跑本脚本。在那之前 search 意图会老实承认查不了。")
    else:
        print("结果不稳定（部分通过）：搜索可能时好时坏，建议再跑一次确认。")
    return 0 if ok_count else 1


if __name__ == "__main__":
    sys.exit(main())
