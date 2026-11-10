#!/usr/bin/env python3
"""设备注册 / 探活 / 注销的命令行入口（管理员流程，不在聊天里触发）。

背景：按插件团队 2026-10-26 回填，
``hardware_agent_register_device`` 的唯一入参是 ``device_package``（对象，必填，
device-package-v1 格式），不是文件路径。``tools/list`` 仍会返回这个工具，插件
也不会代宿主拒绝调用，所以宿主主动把它从模型的工具清单里摘掉了，改由本脚本
在部署阶段显式执行。

用法：

    # 1. 注册：把设备包 JSON 整个塞进 device_package
    python3 tools/register_device.py --package device-package.json

    # 2. 探活：确认设备真的能通（会更新插件侧的 connectivity 状态）
    python3 tools/register_device.py --verify living_room_light_1

    # 3. 看当前有哪些设备
    python3 tools/register_device.py --list

    # 4. 注销
    python3 tools/register_device.py --unregister living_room_light_1

    # 联调环境指定插件入口
    python3 tools/register_device.py --package dp.json \
        --command ~/.smart-hub/hardware-plugin/<hash>/payload/mcp_server.py

    # 身份变量还没配全时，只验协议能不能走通
    python3 tools/register_device.py --list --allow-missing-identity

退出码：0 全部成功 / 1 有调用失败 / 2 用法或输入文件错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_gateway.hardware_mcp import (  # noqa: E402
    HardwareAgentConfig,
    HardwareAgentConfigError,
    McpStdioClient,
    ToolOutcome,
)

TOOL_REGISTER = "hardware_agent_register_device"
TOOL_UNREGISTER = "hardware_agent_unregister_device"
TOOL_VERIFY = "hardware_agent_verify_device"
TOOL_LIST = "hardware_agent_list_devices"


def say(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 58 - len(title)))


def load_package(path: str) -> Optional[Dict[str, Any]]:
    """读设备包 JSON。他们给的是对象本身，不是路径，所以必须整包塞进去。"""
    file = Path(path).expanduser().resolve()
    if not file.is_file():
        print(f"找不到设备包文件：{file}")
        print("提示：真实设备包由设备/插件团队提供，插件包里只有示例" "（device-package.example.json）。")
        return None
    try:
        raw = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"读取失败：{error}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"设备包不是合法 JSON：{error}")
        return None
    if not isinstance(data, dict):
        print(f"设备包顶层必须是对象，实际是 {type(data).__name__}")
        return None

    version = data.get("protocol_version")
    package_type = data.get("package_type")
    print(f"设备包：{file.name}（{file.stat().st_size:,} 字节）")
    if version or package_type:
        print(f"  声明：protocol_version={version!r} package_type={package_type!r}")
    else:
        print("  注意：没有 protocol_version / package_type 字段，可能不是 device-package-v1")

    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    device_id = device.get("device_id") or data.get("device_id")
    if device_id:
        print(f"  device_id：{device_id}")
    capabilities = device.get("capabilities") or data.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        names = []
        for item in capabilities:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, Mapping) and item.get("name"):
                names.append(str(item["name"]))
        print(f"  capabilities：{', '.join(names) or '(未识别格式)'}")
    return data


def print_outcome(outcome: ToolOutcome, show_raw: bool) -> bool:
    print(f"  工具：{outcome.name}")
    print(f"  status：{outcome.status or '(缺失)'}  ok={outcome.ok}")
    if outcome.error_code:
        print(f"  error_code：{outcome.error_code}")
    if outcome.message and not outcome.ok:
        print(f"  message：{outcome.message}")
    payload = outcome.payload or {}
    if outcome.ok:
        # 注册类工具返回结构未定义，把关键字段挑出来，其余原样打印。
        # status 上面已经打过，这里不重复。
        for key in (
            "device_id",
            "message",
            "connectivity_status",
            "connectivity",
            "verified",
            "already_registered",
        ):
            if key in payload:
                print(f"  {key}：{payload[key]}")
    if show_raw or not outcome.ok:
        print("  原始返回：")
        print("    " + (outcome.raw_text or json.dumps(payload, ensure_ascii=False)))
    return outcome.ok


def call_and_report(
    client: McpStdioClient,
    name: str,
    arguments: Dict[str, Any],
    label: str,
    show_raw: bool,
) -> bool:
    say(label)
    try:
        outcome = client.call_tool(name, arguments)
    except Exception as error:  # noqa: BLE001 - 命令行工具，要打出真实原因
        print(f"  调用失败：{type(error).__name__}: {error}")
        for line in client.stderr_tail()[-10:]:
            print(f"  [插件 stderr] {line}")
        return False
    return print_outcome(outcome, show_raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="设备注册 / 探活 / 注销（管理员入口，不经聊天）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--package", help="设备包 JSON 文件路径（注册）")
    group.add_argument("--verify", metavar="DEVICE_ID", help="探活指定设备")
    group.add_argument("--unregister", metavar="DEVICE_ID", help="注销指定设备")
    group.add_argument("--list", action="store_true", help="列出当前设备")

    parser.add_argument(
        "--command", help="插件入口（默认读 HARDWARE_AGENT_MCP_COMMAND）"
    )
    parser.add_argument("--url", help="远端 MCP 地址（默认读 HARDWARE_AGENT_MCP_URL）")
    parser.add_argument(
        "--allow-missing-identity",
        action="store_true",
        help="身份变量不全也继续，只验协议",
    )
    parser.add_argument(
        "--raw", action="store_true", help="成功时也打印插件原始返回"
    )
    parser.add_argument(
        "--also-verify",
        action="store_true",
        help="注册成功后顺带 verify 一次（需要能从设备包里认出 device_id）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    package: Optional[Dict[str, Any]] = None
    if args.package:
        package = load_package(args.package)
        if package is None:
            return 2

    try:
        config = HardwareAgentConfig.from_env(
            None,
            command=[args.command] if args.command else None,
            url=args.url,
            require_identity=not args.allow_missing_identity,
        )
    except HardwareAgentConfigError as error:
        print(f"配置不完整：{error}")
        print("提示：先跑 python3 tools/check_plugin.py 确认插件本身能起来。")
        return 1

    print(f"插件入口：{config.transport} — {config.command or config.url}")

    client = McpStdioClient(config)
    try:
        client.start()
    except Exception as error:  # noqa: BLE001
        print(f"插件启动或握手失败：{type(error).__name__}: {error}")
        for line in client.stderr_tail()[-15:]:
            print(f"  [插件 stderr] {line}")
        client.close()
        return 1

    ok = True
    try:
        print(f"已握手：{client.server_info.get('name')} {client.negotiated_version}")
        names = {str(t.get("name")) for t in client.tools if isinstance(t, Mapping)}
        for required in (TOOL_REGISTER, TOOL_VERIFY, TOOL_LIST):
            if required not in names:
                print(f"  警告：插件没有暴露 {required}")

        if package is not None:
            # 唯一入参就是 device_package 对象本身。
            ok = call_and_report(
                client,
                TOOL_REGISTER,
                {"device_package": package},
                "注册设备",
                args.raw,
            )
            device = package.get("device") if isinstance(package.get("device"), dict) else {}
            device_id = device.get("device_id") or package.get("device_id")
            if ok and args.also_verify and device_id:
                ok = call_and_report(
                    client,
                    TOOL_VERIFY,
                    {"device_id": str(device_id)},
                    f"探活 {device_id}",
                    args.raw,
                )
            elif ok and args.also_verify:
                print("  跳过探活：设备包里没认出 device_id，请手动 --verify")
        elif args.verify:
            ok = call_and_report(
                client, TOOL_VERIFY, {"device_id": args.verify}, f"探活 {args.verify}", args.raw
            )
        elif args.unregister:
            ok = call_and_report(
                client,
                TOOL_UNREGISTER,
                {"device_id": args.unregister},
                f"注销 {args.unregister}",
                args.raw,
            )
        elif args.list:
            call_and_report(client, TOOL_LIST, {}, "当前设备", True)
    finally:
        client.close()

    if package is not None and not ok:
        print("\n注册失败。常见原因：设备包格式不是 device-package-v1、tenant/actor 与设备归属不匹配。")
        print("原始返回已在上面打印，可直接贴给插件团队。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
