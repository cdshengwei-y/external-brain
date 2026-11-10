"""硬件动作插件连通性探针。

用中枢同一个客户端（agent_gateway/hardware_mcp.py）走一遍：
initialize 握手 -> tools/list -> 设备列表 -> 可选的真实命令。

用法（需先 source config/plugin.env，且插件服务已由
tools/start_hardware_agent_mcp.sh 启动）：

    python3 tools/verify_hardware_agent.py            # 只查设备
    python3 tools/verify_hardware_agent.py bean_ye_1  # 追加一条 status.read
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_gateway.hardware_mcp import HardwareAgentConfig, McpStdioClient


def main() -> int:
    try:
        config = HardwareAgentConfig.from_env()
    except Exception as error:
        print(f"配置错误：{error}")
        return 2
    print(f"插件地址：{config.url}")
    client = McpStdioClient(config)
    try:
        client.start()
        tools = client.list_tools()
        print(f"握手成功，工具数：{len(tools)}")
        print("  " + ", ".join(sorted(t.get("name", "?") for t in tools)))

        outcome = client.call_tool("hardware_agent_list_devices", {})
        print(f"\n设备列表（status={outcome.status}）：")
        try:
            devices = (outcome.payload.get("devices") or []) if outcome.payload else []
            for device in devices:
                print(
                    f"  - {device.get('device_id')}: "
                    f"{device.get('name')} "
                    f"[{device.get('registration_status')}/"
                    f"{device.get('connectivity_status')}] "
                    f"能力：{','.join(c.get('name', '?') for c in device.get('capabilities', []))}"
                )
            if not devices:
                print("  （空）" + (outcome.raw_text or ""))
        except (TypeError, AttributeError):
            print(f"  原始返回：{outcome.raw_text[:300]}")

        if len(sys.argv) > 1:
            device_id = sys.argv[1]
            print(f"\n对 {device_id} 发一条 status.read（真实命令）：")
            outcome = client.call_tool(
                "hardware_agent_execute_command",
                {
                    "request_id": f"probe-{__import__('time').time_ns()}",
                    "conversation_id": "verify-script",
                    "user_request": f"探针查询 {device_id} 状态",
                    "device_id": device_id,
                    "capability": "status.read",
                    "params": {},
                },
            )
            print(f"  status={outcome.status} error_code={outcome.error_code}")
            print(f"  结果：{json.dumps(outcome.payload, ensure_ascii=False)[:400]}")
            return 0 if outcome.ok else 1
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
