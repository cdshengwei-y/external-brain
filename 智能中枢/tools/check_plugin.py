#!/usr/bin/env python3
"""硬件插件接入自检：拿到同事的插件后，先跑这一条命令。

它按顺序检查六个环节，哪个环节挂了就停在哪一步，并给出可读的原因：

    1. 平台与可执行文件   —— .exe 在 macOS/Linux 上根本起不来
    2. 身份环境变量       —— 四个必填项齐不齐
    3. 进程能否拉起       —— spawn 失败通常是权限或解释器问题
    4. MCP 握手           —— initialize / initialized / tools/list
    5. 工具清单           —— 插件到底暴露了哪些能力
    6. 设备列表           —— 能不能看到真机

用法：

    # 用环境变量里的 HARDWARE_AGENT_MCP_COMMAND
    python3 tools/check_plugin.py

    # 直接指定插件路径
    python3 tools/check_plugin.py --command "/Applications/hardware-agent-mcp"

    # 顺带调一个无副作用的工具
    python3 tools/check_plugin.py --call hardware_agent_list_devices

    # 调带参数的工具
    python3 tools/check_plugin.py --call hardware_agent_get_device_status \
        --args '{"device_id": "living_room_light_1"}'

    # 本机没有完整身份变量时，先只看协议通不通
    python3 tools/check_plugin.py --allow-missing-identity
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_gateway.hardware_mcp import (  # noqa: E402
    HardwareAgentConfig,
    HardwareAgentConfigError,
    McpStdioClient,
)

REQUIRED_KEYS = (
    "HARDWARE_AGENT_TENANT_ID",
    "HARDWARE_AGENT_ACTOR_REF",
    "RK3588_BASE_URL",
    "AGENTOS_COMMAND_API_KEY",
)

QUERY_TOOLS = ("hardware_agent_list_devices",)


def say(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 60 - len(title)))


def check_platform(command: str) -> Optional[str]:
    """返回阻断原因，None 表示可以继续。"""
    say("1/6 平台与可执行文件")
    system = platform.system()
    print(f"当前系统：{system} ({platform.machine()})")
    print(f"插件命令：{command}")

    if command.endswith(".exe") and system != "Windows":
        print("结论：这是 Windows 可执行文件，当前系统无法直接运行。")
        print("      可选做法：要一份 macOS/Linux 版；或该插件的源码/跨平台打包；")
        print("      或者把插件放在 Windows 机器上，但那台机器就得同时承担中枢角色。")
        return "平台不匹配"

    path = Path(command)
    if path.exists():
        if command.endswith(".py"):
            print(f"文件：存在，将由 {sys.executable} 启动")
        elif not os.access(path, os.X_OK):
            print("结论：文件存在但没有可执行权限。")
            print(f"      修复：chmod +x {command}")
            return "缺少可执行权限"
        else:
            print("文件：存在且可执行")
    else:
        import shutil

        if shutil.which(command) is None:
            print("结论：找不到这个命令（既不是路径也不是 PATH 里的程序）。")
            return "找不到可执行文件"
        print(f"文件：通过 PATH 找到 {shutil.which(command)}")
    return None


def check_identity(allow_missing: bool) -> Optional[str]:
    say("2/6 身份环境变量")
    missing: List[str] = []
    for key in REQUIRED_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            masked = value if len(value) <= 6 else f"{value[:3]}***{value[-2:]}"
            print(f"  {key} = {masked}")
        else:
            print(f"  {key} = （空）")
            missing.append(key)
    if missing and not allow_missing:
        print(f"结论：缺 {len(missing)} 项：{', '.join(missing)}")
        return "身份变量不全"
    if missing:
        print("结论：有缺失，但按 --allow-missing-identity 继续（插件可能自己会报错）")
    else:
        print("结论：齐全")
    return None


def check_endpoint(url: str, token: Optional[str]) -> None:
    say("1/6 远端接口")
    print(f"接口地址：{url}")
    print(f"访问令牌：{'已配置' if token else '未配置'}")
    print("结论：走远端模式，跳过平台与本地身份检查")


def check_spawn(config: HardwareAgentConfig) -> Optional[McpStdioClient]:
    label = "连接接口并握手" if config.transport == "http" else "拉起进程并握手"
    say(f"3/6 {label}")
    client = McpStdioClient(config)
    try:
        client.start()
    except Exception as error:  # noqa: BLE001 - 自检就是要把各种失败都打出来
        print(f"结论：失败 —— {type(error).__name__}: {error}")
        for line in client.stderr_tail[-15:]:
            print(f"  [插件日志] {line}")
        client.close()
        return None
    info = client.server_info or {}
    print(f"结论：成功")
    print(f"  传输：{config.transport}")
    print(f"  服务名：{info.get('name')} {info.get('version')}")
    print(f"  协商协议：{client.negotiated_version}")
    try:
        client.ping()
        print("  ping：正常")
    except Exception as error:  # noqa: BLE001 - ping 失败不影响后续检查
        print(f"  ping：失败（{type(error).__name__}: {error}）")
    return client


def report_tools(client: McpStdioClient) -> List[str]:
    say("4/6 工具清单")
    tools = client.tools
    if not tools:
        print("结论：插件没有暴露任何工具")
        return []
    names = []
    for tool in tools:
        name = str(tool.get("name"))
        names.append(name)
        desc = str(tool.get("description") or "").splitlines()[0][:60]
        print(f"  - {name}")
        if desc:
            print(f"      {desc}")
    return names


def check_devices(client: McpStdioClient, names: List[str]) -> None:
    say("5/6 设备列表")
    if "hardware_agent_list_devices" not in names:
        print("结论：插件没有 hardware_agent_list_devices，跳过")
        return
    try:
        outcome = client.call_tool("hardware_agent_list_devices", {})
    except Exception as error:  # noqa: BLE001
        print(f"结论：调用失败 —— {type(error).__name__}: {error}")
        return
    print(f"状态：{outcome.status}")
    if not outcome.ok:
        print(f"错误码：{outcome.error_code}")
        print(f"说明：{outcome.message}")
        return
    devices = outcome.payload.get("devices") or []
    if not devices:
        print("结论：能通，但一台设备都没有（RK3588 侧可能没注册设备）")
        return
    print(f"结论：{len(devices)} 台设备")
    for device in devices:
        if not isinstance(device, dict):
            continue
        print(
            f"  - {device.get('name') or device.get('device_id')}"
            f"  [{device.get('device_id')}]"
            f"  {device.get('connectivity_status') or ''}"
        )


def call_once(client: McpStdioClient, name: str, arguments: Dict[str, Any]) -> None:
    say(f"6/6 调用 {name}")
    try:
        outcome = client.call_tool(name, arguments)
    except Exception as error:  # noqa: BLE001
        print(f"结论：失败 —— {type(error).__name__}: {error}")
        return
    print(f"status   : {outcome.status}")
    print(f"ok       : {outcome.ok}")
    if outcome.error_code:
        print(f"error    : {outcome.error_code} / {outcome.message}")
    print("payload  :")
    print(json.dumps(outcome.payload, ensure_ascii=False, indent=2)[:2000])


def main() -> int:
    parser = argparse.ArgumentParser(description="硬件插件接入自检")
    parser.add_argument(
        "--url",
        default=os.environ.get("HARDWARE_AGENT_MCP_URL"),
        help="插件远端接口地址，例如 http://192.168.0.20:18800/mcp",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HARDWARE_AGENT_MCP_TOKEN"),
        help="远端接口的访问令牌（按 Bearer 发送）",
    )
    parser.add_argument(
        "--command",
        default=os.environ.get("HARDWARE_AGENT_MCP_COMMAND"),
        help="插件可执行文件路径或命令（本地子进程模式）",
    )
    parser.add_argument(
        "--allow-missing-identity",
        action="store_true",
        help="身份变量不全也继续，只验协议",
    )
    parser.add_argument(
        "--cwd",
        default=os.environ.get("HARDWARE_AGENT_MCP_CWD"),
        help="插件工作目录（发布包解压后通常是 payload/），相对路径文件依赖它",
    )
    parser.add_argument("--call", help="握手后额外调用一个工具")
    parser.add_argument("--args", default="{}", help="配合 --call 的参数 JSON")
    args = parser.parse_args()

    url = str(args.url or "").strip()
    env = dict(os.environ)
    if args.cwd:
        env["HARDWARE_AGENT_MCP_CWD"] = args.cwd
    if url:
        env["HARDWARE_AGENT_MCP_URL"] = url
        if args.token:
            env["HARDWARE_AGENT_MCP_TOKEN"] = args.token
        check_endpoint(url, args.token)
        say("2/6 鉴权")
        print("结论：由服务端校验令牌，本地不再检查四个身份变量")
        config_source: Dict[str, Any] = {"url": url}
    else:
        if not args.command:
            print(
                "既没有接口地址也没有插件路径：请设置 HARDWARE_AGENT_MCP_URL"
                "（远端接口）或 HARDWARE_AGENT_MCP_COMMAND（本地子进程）"
            )
            return 2
        blocked = check_platform(args.command)
        if blocked:
            return 1
        blocked = check_identity(args.allow_missing_identity)
        if blocked:
            return 1
        config_source = {"command": args.command}

    try:
        if url:
            config = HardwareAgentConfig.from_env(
                env, url=url, require_identity=False
            )
        else:
            config = HardwareAgentConfig.from_env(
                env,
                command=[str(config_source["command"])],
                require_identity=not args.allow_missing_identity,
            )
    except HardwareAgentConfigError as error:
        print(f"\n配置不完整：{error}")
        return 1

    client = check_spawn(config)
    if client is None:
        return 1

    try:
        names = report_tools(client)
        check_devices(client, names)
        if args.call:
            try:
                parsed = json.loads(args.args)
            except json.JSONDecodeError as error:
                print(f"--args 不是合法 JSON：{error}")
                return 2
            call_once(client, args.call, parsed if isinstance(parsed, dict) else {})
    finally:
        client.close()

    print("\n自检完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
