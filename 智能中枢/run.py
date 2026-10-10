from __future__ import annotations

import argparse
from pathlib import Path

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
    parser.add_argument("--mqtt-prefix", default="hub/v1")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = HubStore(args.db)
    service = HubService(store=store, transport=MemoryTransport())
    mqtt_transport = None
    if args.mqtt_host:
        mqtt_transport = PahoMqttTransport(
            host=args.mqtt_host,
            port=args.mqtt_port,
            topic_prefix=args.mqtt_prefix,
            on_message=service.handle_mqtt_message,
        )
        service.transport = mqtt_transport
        print(f"MQTT 已连接: {args.mqtt_host}:{args.mqtt_port}")
    else:
        print("当前使用内存传输，适合 API 和调度器开发；接入设备时请配置 --mqtt-host。")
    try:
        serve(service, args.host, args.port)
    finally:
        if mqtt_transport is not None:
            mqtt_transport.close()
        store.close()


if __name__ == "__main__":
    main()
