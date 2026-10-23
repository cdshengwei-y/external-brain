from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Optional

from hub_core.http_mqtt_bridge import BridgeHttpServer
from hub_core.http_api import serve
from hub_core.mqtt import PahoMqttTransport
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport


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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = HubStore(args.db)
    service = HubService(store=store, transport=MemoryTransport())
    service.mark_persisted_devices_offline()
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
        serve(service, args.host, args.port)
    finally:
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
