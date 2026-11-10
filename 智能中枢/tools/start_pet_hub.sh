#!/bin/bash
# 一键启动桌宠中枢：让桌宠说一句话就能得到语音回复。
#
#   ./tools/start_pet_hub.sh
#
# 做了三件事，都是为了「开箱能说话」：
#   1. 绑 0.0.0.0，桌宠才连得上（固件指向 http://192.168.0.107:18787）；
#   2. 有模型配置就用真模型，没有就用脚本模型兜底，链路照样通；
#   3. 开本地语音合成，不再依赖 RK3588 的 TTS。
#
# 临时改端口：PORT=9000 ./tools/start_pet_hub.sh
# 传额外参数：./tools/start_pet_hub.sh --db /tmp/other.db

set -euo pipefail

cd "$(dirname "$0")/.."

# 载入硬件插件运行配置（含 API Key，权限 600，不进版本库）。
# 没有这个文件就退化成 mock / 脚本模型，链路照样能跑。
ENV_FILE="${PLUGIN_ENV_FILE:-config/plugin.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  echo "插件配置：已载入 $ENV_FILE"
fi

PORT="${PORT:-18787}"
HOST="${HOST:-0.0.0.0}"

# 桌宠只连两个地方：内网模型（192.168.0.245）和 sub2api。
# 2026-11-06 16:50 踩过的坑：从带代理的终端启动，会把 HTTP_PROXY/HTTPS_PROXY
# 一起继承进来。TUN/透明代理会在网络层把**内网**流量也劫走，
# 表现是「ping 245 100% 丢包、chat 502 upstream refused、一句 32 秒」——
# 看起来像那台机器挂了，其实关掉代理立刻恢复（chat 200 / 1.1s）。
# plugin.env 里那行 NO_PROXY 救不了这种，因为 urllib 在 macOS 上优先读系统代理。
# 所以启动前一律清干净，只留 no_proxy 兜住本地和内网。
unset http_proxy https_proxy all_proxy ftp_proxy
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY FTP_PROXY
export no_proxy="127.0.0.1,localhost,192.168.0.245,sub2api.vx123.xin"
export NO_PROXY="$no_proxy"

if [ -n "${PET_AGENT_CHAT_BASE_URL:-}" ] && [ -n "${PET_AGENT_CHAT_MODEL:-}" ]; then
  MODE=(--agent)
  echo "对话后端：模型 ${PET_AGENT_CHAT_MODEL} @ ${PET_AGENT_CHAT_BASE_URL}"
elif [ -n "${HUB_CHAT_BASE_URL:-}" ] && [ -n "${HUB_CHAT_MODEL:-}" ]; then
  MODE=(--llm)
  echo "对话后端：直连 ${HUB_CHAT_MODEL} @ ${HUB_CHAT_BASE_URL}"
  if [ -n "${HUB_CHAT_FALLBACK_BASE_URL:-}" ]; then
    echo "          备用 ${HUB_CHAT_FALLBACK_MODEL:-?} @ ${HUB_CHAT_FALLBACK_BASE_URL}"
  fi
elif [ -n "${SWBOT_EMAIL:-}" ] && [ -n "${SWBOT_PASSWORD:-}" ]; then
  MODE=(--swbot)
  echo "对话后端：sw-bot ${SWBOT_BASE_URL:-https://bot.vx123.xin} → Bot「${SWBOT_BOT:-默认第一个}」"
else
  MODE=(--demo-agent)
  echo "对话后端：脚本模型（没配置 HUB_CHAT_* / PET_AGENT_CHAT_* / SWBOT_*）"
fi

echo "监听地址：http://${HOST}:${PORT}  （桌宠应指向本机局域网 IP）"

# 语音优先级：本地神经 TTS（快、稳、不联网）> 云端 OmniVoice（自然但慢）> 本机 say。
# 本地模型的判断和 run.py 保持一致：LOCAL_TTS_MODEL_DIR，或缓存目录里确实有模型。
NEURAL_MODEL_DIR="${LOCAL_TTS_MODEL_DIR:-$HOME/.cache/sherpa-tts}"
if [ -d "$NEURAL_MODEL_DIR" ] && ls "$NEURAL_MODEL_DIR"/*/*.onnx >/dev/null 2>&1; then
  TTS=(--neural-tts)
  echo "语音合成：本地神经 TTS（${NEURAL_MODEL_DIR}）"
elif [ -n "${AMD_RADEON_API_KEY:-}" ]; then
  TTS=(--amd-tts)
  echo "语音合成：云端 OmniVoice（音色 ${OMNIVOICE_VOICE:-female}，失败回退本机 say）"
else
  TTS=(--local-tts)
  echo "语音合成：本机 say"
fi
echo

exec python3 run.py \
  --host "$HOST" \
  --port "$PORT" \
  "${MODE[@]}" \
  "${TTS[@]}" \
  "$@"
