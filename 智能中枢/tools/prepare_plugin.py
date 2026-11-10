#!/usr/bin/env python3
"""解压并检查同事给的 Windows 发布包，在 macOS/Linux 上直接跑 Python 入口。

背景：插件正式交付物是 Windows 的 ``hardware-agent-mcp.exe``，但它只是个
启动器，真正逻辑在 ``payload/mcp_server.py``（纯标准库）。所以在 Mac 上
不需要 Windows、也不需要 Wine，直接 ``python3 payload/mcp_server.py`` 就能
当作 stdio MCP 服务被中枢拉起。

用法：

    # 解压 + 定位入口 + 打印后续命令
    python3 tools/prepare_plugin.py --zip ~/Downloads/hardware-agent-*.zip

    # 只校验不解压
    python3 tools/prepare_plugin.py --zip ... --verify-only

    # 解压到指定目录
    python3 tools/prepare_plugin.py --zip ... --dest /opt/hardware-agent
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path
from typing import Optional

EXPECTED_SHA256 = "00ADA89D6E9083D63FABB70516701CF454B68E27A8B79D91DCAC2C120EE53181"

#: 入口候选，按优先级排列。
ENTRY_CANDIDATES = (
    "payload/mcp_server.py",
    "mcp_server.py",
    "payload/hardware_agent_mcp.py",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def find_entry(root: Path) -> Optional[Path]:
    for relative in ENTRY_CANDIDATES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    matches = sorted(root.rglob("mcp_server.py"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description="解压并检查硬件插件发布包")
    parser.add_argument("--zip", required=True, help="发布包路径")
    parser.add_argument("--dest", help="解压目录，默认 ~/.smart-hub/hardware-plugin/<前8位哈希>")
    parser.add_argument("--verify-only", action="store_true", help="只算哈希，不解压")
    parser.add_argument(
        "--force", action="store_true", help="目标目录已存在时清空重解"
    )
    args = parser.parse_args()

    archive = Path(args.zip).expanduser().resolve()
    if not archive.is_file():
        print(f"找不到文件：{archive}")
        return 2

    digest = sha256_of(archive)
    print(f"文件：{archive}")
    print(f"大小：{archive.stat().st_size:,} 字节")
    print(f"SHA-256：{digest}")
    if digest == EXPECTED_SHA256:
        print("校验：与交接回复 5 节给出的值一致")
    else:
        print("校验：与交接回复给出的值不一致，请确认是不是换了发布包")
        print(f"      期望 {EXPECTED_SHA256}")
    if args.verify_only:
        return 0

    dest = (
        Path(args.dest).expanduser()
        if args.dest
        else Path.home() / ".smart-hub" / "hardware-plugin" / digest[:8]
    )
    if dest.exists() and any(dest.iterdir()):
        if not args.force:
            print(f"目标目录已存在：{dest}")
            print("      要覆盖请加 --force")
            return 1
        import shutil

        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        bundle.extractall(dest)

    payload_dirs = sorted({name.split("/")[0] for name in names if "/" in name})
    print(f"解压到：{dest}")
    print(f"顶层目录：{', '.join(payload_dirs[:6]) or '（无子目录）'}")

    entry = find_entry(dest)
    if entry is None:
        print("\n没有找到 mcp_server.py。发布包结构可能和预期不同，请把目录树发给我们。")
        print("  已解压内容：")
        for item in sorted(dest.iterdir())[:20]:
            print(f"    {item.name}")
        return 1

    payload_dir = entry.parent
    print(f"入口文件：{entry}")
    print(f"工作目录：{payload_dir}")

    # 入口若是 UTF-8 之外编码，Windows 上会踩坑，Mac 上顺带确认一下
    try:
        entry.read_text(encoding="utf-8")
        print("编码：UTF-8 可读")
    except UnicodeDecodeError:
        print("编码：不是 UTF-8，插件可能要求 PYTHONUTF8=1")

    print("\n接下来（把 <...> 换成真实值）：")
    print(f"""
export VHA_DATA_DIR="$HOME/.smart-hub/hardware-agent"
export HARDWARE_AGENT_MCP_CWD="{payload_dir}"
export RK3588_BASE_URL="http://<rk3588-host>:8099"
export HARDWARE_AGENT_TENANT_ID="<tenant>"
export HARDWARE_AGENT_ACTOR_REF="<actor>"
export AGENTOS_COMMAND_API_KEY="<key>"

# 1) 自检：握手 / ping / 工具清单 / 设备列表
python3 tools/check_plugin.py --command "{entry}" --cwd "{payload_dir}"

# 2) 接到中枢（核心是这条：不用 exe，直接用 python 起同一份入口）
HARDWARE_AGENT_MCP_COMMAND="{entry}" \\
HARDWARE_AGENT_MCP_CWD="{payload_dir}" \\
./tools/start_pet_hub.sh
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
