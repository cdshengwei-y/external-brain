from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path
from typing import Optional

from hub_core.http_mqtt_bridge import BridgeHttpServer
from hub_core.http_api import serve
from hub_core.mqtt import PahoMqttTransport
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport

from agent_gateway.pet_agent import build_agent_from_env
from agent_gateway.portable import PortableRequestService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="智能中枢第一版本地服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parent / ".data" / "hub.db"),
    )
    parser.add_argument("--mqtt-host", help="配置后启用 MQTT 设备总线")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-prefix", default="agentos")
    parser.add_argument("--mqtt-client-id", default="smart-hub")
    parser.add_argument(
        "--bridge-host",
        default=None,
        help="启用 RK3588 HTTP-MQTT 兼容桥，例如 0.0.0.0",
    )
    parser.add_argument("--bridge-port", type=int, default=19700)
    parser.add_argument("--bridge-timeout", type=float, default=30.0)
    parser.add_argument(
        "--agent",
        action="store_true",
        help="启用桌宠大脑（模型工具循环 + MCP 硬件插件），配置来自环境变量",
    )
    parser.add_argument(
        "--demo-agent",
        action="store_true",
        help="用脚本模型 + 本地 mock 插件启动桌宠大脑，不需要模型服务",
    )
    parser.add_argument(
        "--local-tts",
        action="store_true",
        help="用本机 say 合成回复语音（RK3588 TTS 不在线时用）",
    )
    parser.add_argument(
        "--amd-tts",
        action="store_true",
        help=(
            "用 AMD Radeon Cloud 的 OmniVoice 合成回复语音"
            "（配置来自 AMD_RADEON_API_KEY / OMNIVOICE_VOICE / OMNIVOICE_SPEED）"
        ),
    )
    parser.add_argument(
        "--neural-tts",
        action="store_true",
        help=(
            "用本地神经 TTS（sherpa-onnx + Kokoro/Piper）合成，CPU 推理、不联网"
            "（模型放 ~/.cache/sherpa-tts/，可用 LOCAL_TTS_MODEL_DIR 指定）"
        ),
    )
    parser.add_argument(
        "--swbot",
        action="store_true",
        help="把桌宠对话转给自建 sw-bot（配置来自 SWBOT_BASE_URL / SWBOT_EMAIL / SWBOT_PASSWORD / SWBOT_BOT）",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help=(
            "把桌宠对话直接接到 OpenAI 兼容接口，绕过 sw-bot"
            "（配置来自 HUB_CHAT_BASE_URL / HUB_CHAT_MODEL / HUB_CHAT_API_KEY，"
            "可选 HUB_CHAT_FALLBACK_* 做备用端点）"
        ),
    )
    parser.add_argument(
        "--voice",
        default=os.environ.get("PET_AGENT_TTS_VOICE"),
        help="本地 TTS 语音名，例如 Tingting；默认自动挑一个中文语音",
    )
    return parser


# 设备交互里最高频的几句固定回复。云端首次合成要 1.5~2 秒，提前合成进缓存后
# 喊醒就能立刻出声；顺序按使用频率排，第一条是纯唤醒的「我在」。
WARMUP_TEXTS = (
    "我在",
    "好的，我先待命。",
    "好的，音乐喷泉启动，跟着音乐跳起来。",
    "音乐喷泉已关闭。",
)


def _warmup_speech_provider(provider) -> None:
    """后台串行预热固定话术，不阻塞启动。

    云端平台是串行排队的（并发会被放大到二十几秒），所以预热必须一条条来；
    放进后台线程，用户看到「已启用」就能开始用，不用等预热跑完。
    """

    def warm() -> None:
        started = time.perf_counter()
        ok = provider.warmup(WARMUP_TEXTS)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"语音预热：{ok}/{len(WARMUP_TEXTS)} 条，{elapsed:.0f}ms", flush=True)

    threading.Thread(target=warm, name="omnivoice-warmup", daemon=True).start()


def _with_local_fallback(provider, voice: Optional[str]):
    """给云端 provider 套一层本机 say 兜底，云端抖到超时也不至于没声。"""
    from agent_gateway.local_tts import MacSaySpeechProvider
    from agent_gateway.omni_voice_tts import FallbackSpeechProvider

    try:
        local = MacSaySpeechProvider(voice=voice)
    except Exception:  # noqa: BLE001 - 兜底本身不该影响启动
        return provider
    if not local.available:
        return provider
    print(f"语音兜底：云端失败时退回本机 {local.voice}")
    return FallbackSpeechProvider(provider, local)


def _resolve_demo_agent(voice: Optional[str]):
    """脚本模型 + 硬件插件：没有模型服务时也能验证整条语音链路。

    插件优先用真实配置（``HARDWARE_AGENT_MCP_COMMAND`` / ``HARDWARE_AGENT_MCP_URL``），
    两个都没配才退回本地 mock。这样 ``--demo-agent`` 的含义是"不接对话模型"，
    而不是"一定用假设备"。
    """
    from agent_gateway.hardware_mcp import HardwareAgentConfig, McpStdioClient
    from agent_gateway.pet_agent import PetAgent, ScriptedChatModel

    mock_plugin = str(Path(__file__).resolve().parent / "tools" / "mock_hardware_mcp.py")
    using_real = bool(
        str(os.environ.get("HARDWARE_AGENT_MCP_COMMAND") or "").strip()
        or str(os.environ.get("HARDWARE_AGENT_MCP_URL") or "").strip()
    )
    try:
        if using_real:
            config = HardwareAgentConfig.from_env(os.environ)
        else:
            config = HardwareAgentConfig.from_env(
                os.environ,
                command=[mock_plugin],
                require_identity=False,
            )
    except Exception as error:  # noqa: BLE001 - 演示模式给出原因即可
        print(f"演示模式启动失败：{error}")
        return None
    if using_real:
        print(f"硬件插件：真实配置（{config.transport}，{config.command or config.url}）")
    else:
        print("硬件插件：本地 mock（未配置 HARDWARE_AGENT_MCP_COMMAND / _URL）")
    return PetAgent(
        ScriptedChatModel(),
        McpStdioClient(config),
        narrate_with_model=False,
    )


def main() -> None:
    args = build_parser().parse_args()
    store = HubStore(args.db)
    service = HubService(store=store, transport=MemoryTransport())
    service.mark_persisted_devices_offline()

    portable_service: Optional[PortableRequestService] = None
    speech_provider = None
    # 优先级：本地神经 > 云端 > 本机 say。本地神经又快又稳，装好模型就该它上场。
    if args.neural_tts:
        from agent_gateway.local_neural_tts import LocalNeuralSpeechProvider

        neural = LocalNeuralSpeechProvider()
        if neural.available:
            speech_provider = neural
            print(f"本地神经语音合成已启用：{neural.model_dir.name}")
        else:
            print(f"本地神经语音未启用：{neural.error or '没找到模型'}，继续试下面几种")

    # 云端音色比系统 say 自然得多，配了 key 就走云端；失败/没配再退回本地 say。
    if speech_provider is None and args.amd_tts:
        from agent_gateway.omni_voice_tts import OmniVoiceSpeechProvider

        provider = OmniVoiceSpeechProvider()
        if provider.available:
            speech_provider = _with_local_fallback(provider, args.voice)
            print(
                f"云端语音合成已启用：{provider.model}"
                f"（音色 {provider.voice}，语速 {provider.speed or '默认'}）"
            )
            _warmup_speech_provider(provider)
        else:
            print(
                "云端语音合成未启用：缺少 AMD_RADEON_API_KEY，"
                "改用 --local-tts 或检查 config/plugin.env"
            )

    if speech_provider is None and args.local_tts:
        from agent_gateway.local_tts import MacSaySpeechProvider

        provider = MacSaySpeechProvider(voice=args.voice)
        if provider.available:
            speech_provider = provider
            print(f"本地语音合成已启用：{provider.voice}")
        else:
            print("本地语音合成不可用（缺少 say 或中文语音），回复将只有文字")

    agent = None
    intent_classifier = None
    if args.demo_agent:
        agent = _resolve_demo_agent(args.voice)
        if agent is not None:
            print("桌宠大脑已启用：脚本模型（不接对话模型）")
    elif args.agent:
        agent = build_agent_from_env()
        if agent is None:
            print(
                "桌宠大脑未启用：请检查 PET_AGENT_CHAT_BASE_URL / PET_AGENT_CHAT_MODEL "
                "与 HARDWARE_AGENT_* 环境变量"
            )
        else:
            print("桌宠大脑已启用：模型工具循环 + MCP 硬件插件")
    elif args.llm:
        from agent_gateway.chat_llm import build_chat_client_from_env
        from agent_gateway.swbot import SwBotAgent, SwBotIntentClassifier

        chat_client = build_chat_client_from_env()
        if chat_client is None:
            print(
                "直连模型未启用：请配置 HUB_CHAT_BASE_URL / HUB_CHAT_MODEL"
                "（HUB_CHAT_API_KEY 视服务端要求填）"
            )
        else:
            agent = SwBotAgent(chat_client)
            # 同一个端点在一次调用里既判意图又给回复，设备不用多等一轮。
            intent_classifier = SwBotIntentClassifier(chat_client)
            print(f"桌宠对话已直连模型：{chat_client.bot_hint}（端点 {chat_client.base_url}）")
            print("          端点按顺序为优先级，前一个失败自动切下一个")
            print("意图判定由该模型负责（失败自动退回本地关键词）")
    elif args.swbot:
        from agent_gateway.swbot import (
            SwBotAgent,
            SwBotIntentClassifier,
            build_client_from_env,
        )

        swbot_client = build_client_from_env()
        if swbot_client is None:
            print(
                "sw-bot 未启用：请配置 SWBOT_BASE_URL / SWBOT_EMAIL / SWBOT_PASSWORD"
            )
        else:
            agent = SwBotAgent(swbot_client)
            # 同一个 bot 同时承担第③步（判意图）和闲聊回答：
            # 分类调用就带着 reply，设备不会因为「先问意图再问答案」多等一轮。
            intent_classifier = SwBotIntentClassifier(swbot_client)
            print(
                f"桌宠对话已接到 sw-bot：{swbot_client.base_url}"
                f" → Bot「{swbot_client.bot_hint or '默认第一个'}」"
            )
            print("意图判定由 sw-bot 负责（失败自动退回本地关键词）")

    if agent is not None or speech_provider is not None:
        from agent_gateway.qwen_asr import QwenAsrClient
        from agent_gateway.weather import default_city, start_warmer

        portable_service = PortableRequestService(
            service,
            agent=agent,
            speech_provider=speech_provider,
            transcription_provider=QwenAsrClient(),
            intent_classifier=intent_classifier,
        )
        # 天气接口首次请求约 1.1 秒，后台先刷好，用户问「现在几度」就是 0ms。
        start_warmer()
        print(f"天气快答已启用：默认城市「{default_city()}」（PET_CITY 可改）")

    mqtt_transport = None
    if args.mqtt_host:
        mqtt_transport = PahoMqttTransport(
            host=args.mqtt_host,
            port=args.mqtt_port,
            topic_prefix=args.mqtt_prefix,
            on_message=service.handle_mqtt_message,
            client_id=args.mqtt_client_id,
        )
        service.transport = mqtt_transport
        print(f"MQTT 已连接: {args.mqtt_host}:{args.mqtt_port}")
    else:
        print("当前使用内存传输，适合 API 和调度器开发；接入设备时请配置 --mqtt-host。")

    bridge_server: Optional[BridgeHttpServer] = None
    bridge_thread: Optional[threading.Thread] = None
    if args.bridge_host:
        bridge_server = BridgeHttpServer(
            (args.bridge_host, args.bridge_port),
            service,
            wait_timeout_seconds=args.bridge_timeout,
        )
        bridge_thread = threading.Thread(
            target=bridge_server.serve_forever,
            name="http-mqtt-bridge",
            daemon=True,
        )
        bridge_thread.start()
        print(
            f"HTTP-MQTT 兼容桥已启动: "
            f"http://{args.bridge_host}:{args.bridge_port}"
        )
    try:
        serve(service, args.host, args.port, portable_service=portable_service)
    finally:
        if portable_service is not None:
            portable_service.close()
        if bridge_server is not None:
            bridge_server.shutdown()
            bridge_server.server_close()
        if bridge_thread is not None:
            bridge_thread.join(timeout=2)
        if mqtt_transport is not None:
            mqtt_transport.close()
        store.close()


if __name__ == "__main__":
    main()
