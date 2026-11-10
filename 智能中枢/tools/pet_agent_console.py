#!/usr/bin/env python3
"""桌宠大脑文字控制台：不接设备也能验证工具循环与确认流程。

三种用法：

    # 1) 离线演示：内置脚本模型，不需要任何模型服务
    python3 tools/pet_agent_console.py --demo

    # 2) 接真实模型（OpenAI 兼容 /chat/completions）
    PET_AGENT_CHAT_BASE_URL=http://192.168.0.140:8000/v1 \
    PET_AGENT_CHAT_MODEL=qwen2.5-7b-instruct \
    python3 tools/pet_agent_console.py

    # 3) 接真实硬件插件（需要注入完整身份环境变量）
    HARDWARE_AGENT_TENANT_ID=... HARDWARE_AGENT_ACTOR_REF=... \
    RK3588_BASE_URL=... AGENTOS_COMMAND_API_KEY=... \
    python3 tools/pet_agent_console.py --real-plugin \
        --mcp "C:/Program Files/smart-hardware/hardware-agent-mcp.exe"

默认使用 tools/mock_hardware_mcp.py，控制台里说“有哪些设备”“打开客厅灯”即可。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_gateway.hardware_mcp import (  # noqa: E402
    HardwareAgentConfig,
    HardwareAgentConfigError,
    McpStdioClient,
)
from agent_gateway.pet_agent import (  # noqa: E402
    OpenAICompatibleChatModel,
    PetAgent,
    ToolCall,
)

MOCK_PLUGIN = str(PROJECT_ROOT / "tools" / "mock_hardware_mcp.py")


class DemoModel:
    """离线演示用的脚本模型，按关键词给出很笨但可复现的工具决策。"""

    def __init__(self) -> None:
        self.turn = 0

    def complete(self, messages, tools=None):
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            # 工具结果已经回来了，演示模型只需要把它说成人话。
            return {"content": _summarize(str(last.get("content") or "")), "tool_calls": []}

        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = str(message.get("content") or "")
                break
        if "设备" in user_text and "打开" not in user_text:
            return _call("hardware_agent_list_devices")
        if "打开" in user_text or "关闭" in user_text:
            power = "off" if "关闭" in user_text else "on"
            return _call(
                "hardware_agent_execute_command",
                device_id="living_room_light_1",
                capability="light.power.set",
                params={"power": power},
            )
        return {"content": "我是演示模式的桌宠，说“有哪些设备”或“打开客厅灯”试试。", "tool_calls": []}


def _summarize(raw: str) -> str:
    import json

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "收到执行结果。"
    devices = payload.get("devices")
    if isinstance(devices, list):
        names = [
            str(item.get("name") or item.get("device_id"))
            for item in devices
            if isinstance(item, dict)
        ]
        return "现在的设备有：" + "、".join(names) + "。" if names else "现在没有可用设备。"
    if payload.get("status") == "succeeded":
        return "好的，已经处理完了。"
    return str(payload.get("message") or "这次没有成功。")


def _call(name: str, **arguments: Any) -> Dict[str, Any]:    return {
        "content": "",
        "tool_calls": [
            ToolCall(id=f"demo-{time.time_ns()}", name=name, arguments=arguments)
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="桌宠大脑文字控制台")
    parser.add_argument(
        "--model-url",
        default=os.environ.get("PET_AGENT_CHAT_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL"),
        help="OpenAI 兼容接口地址，例如 http://127.0.0.1:8000/v1",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("PET_AGENT_CHAT_MODEL") or os.environ.get("OPENAI_MODEL"),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("PET_AGENT_CHAT_API_KEY")
        or os.environ.get("OPENAI_API_KEY"),
    )
    parser.add_argument(
        "--mcp",
        default=os.environ.get("HARDWARE_AGENT_MCP_COMMAND") or MOCK_PLUGIN,
        help="硬件插件可执行文件；默认用本地 mock",
    )
    parser.add_argument(
        "--real-plugin",
        action="store_true",
        help="按真实插件要求校验身份环境变量（HARDWARE_AGENT_* 等）",
    )
    parser.add_argument(
        "--narrate",
        choices=["local", "model"],
        default=os.environ.get("PET_AGENT_NARRATE", "local"),
        help="执行结果用固定话术还是再让模型润色",
    )
    parser.add_argument("--demo", action="store_true", help="离线演示，不需要模型服务")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        config = HardwareAgentConfig.from_env(
            os.environ,
            command=[args.mcp],
            require_identity=args.real_plugin,
        )
    except HardwareAgentConfigError as error:
        print(f"硬件插件配置不完整：{error}")
        return 2

    if args.demo:
        model: Any = DemoModel()
        print("模式：离线演示（脚本模型 + 本地 mock 插件）")
    else:
        if not args.model_url or not args.model:
            print(
                "缺少模型配置：请提供 --model-url / --model，"
                "或直接用 --demo 走离线演示。"
            )
            return 2
        model = OpenAICompatibleChatModel(args.model_url, args.model, args.api_key)
        print(f"模式：{args.model} @ {args.model_url}")

    client = McpStdioClient(config)
    agent = PetAgent(
        model,
        client,
        narrate_with_model=args.narrate == "model",
    )
    print(f"硬件插件：{args.mcp}")
    print("输入内容后回车；/pending 看待确认计划，/quit 退出。\n")

    conversation_id = f"console-{int(time.time())}"
    try:
        while True:
            try:
                text = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text in {"/quit", "/exit", "q"}:
                break
            if text == "/pending":
                plan = agent.pending_plan(conversation_id)
                print(f"待确认：{plan if plan else '无'}\n")
                continue
            turn = agent.handle_turn(conversation_id, text)
            print(f"桌宠> {turn.text}")
            if turn.needs_confirmation:
                print("      （需要你回答“执行”或“取消”）")
            if turn.executed or turn.outcomes:
                for outcome in turn.outcomes:
                    print(
                        f"      [工具] {outcome.name} status={outcome.status} "
                        f"code={outcome.error_code or '-'}"
                    )
            print()
    finally:
        agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
