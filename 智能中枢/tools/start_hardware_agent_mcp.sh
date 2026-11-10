#!/bin/bash
# 启动同事的硬件动作插件（Streamable HTTP MCP，仅监听 127.0.0.1:8787）。
#
# 链路：中枢 → 本插件(注册表/权限/幂等) → RK3588 /api/agentos/v1 → 设备。
# 身份说明：插件按 owner_ref 校验设备归属，部署快照里两台设备
# （bean_ye_1 / car_001）的 owner 都是 desktop-user，所以这里固定用它。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="$HOME/.smart-hub/hardware-plugin-current"

set -a
# shellcheck disable=SC1091
source "$ROOT/config/plugin.env"
set +a

# 客户端（中枢）与服务端共用同一个令牌，写在 plugin.env 的
# HARDWARE_AGENT_MCP_TOKEN 里，两边天然一致。
export HARDWARE_AGENT_MCP_HTTP_TOKEN="${HARDWARE_AGENT_MCP_TOKEN:?plugin.env 缺 HARDWARE_AGENT_MCP_TOKEN}"
export HARDWARE_AGENT_TENANT_ID="${HARDWARE_AGENT_TENANT_ID:-tenant-a}"
# 注意：覆盖 plugin.env 里的 user-a（那是旧 stdio 实例的身份）
export HARDWARE_AGENT_ACTOR_REF="desktop-user"
export VHA_DATA_DIR="$PLUGIN_DIR/runtime-data"

cd "$PLUGIN_DIR/payload"
exec python3 mcp_http_server.py --port "${HARDWARE_AGENT_MCP_PORT:-8787}"
