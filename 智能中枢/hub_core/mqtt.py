from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, Optional


class MqttUnavailableError(RuntimeError):
    pass


class PahoMqttTransport:
    """Optional MQTT transport. Install paho-mqtt to enable it."""

    def __init__(
        self,
        host: str,
        port: int,
        topic_prefix: str,
        on_message: Callable[[str, Dict[str, Any]], None],
        client_id: str = "smart-hub",
    ) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as error:
            raise MqttUnavailableError(
                "未安装 paho-mqtt，请先运行: python3 -m pip install -r requirements.txt"
            ) from error

        self._mqtt = mqtt
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix.rstrip("/")
        self.on_message = on_message
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_message = self._handle_message
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.subscribe(f"{self.topic_prefix}/devices/+/manifest", qos=1)
        self.client.subscribe(f"{self.topic_prefix}/devices/+/availability", qos=1)
        self.client.subscribe(f"{self.topic_prefix}/devices/+/state", qos=1)
        self.client.subscribe(f"{self.topic_prefix}/devices/+/events", qos=1)
        self.client.subscribe(f"{self.topic_prefix}/devices/+/command-acks", qos=1)
        self.client.loop_start()

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def publish_command(self, device_id: str, envelope: Dict[str, Any]) -> None:
        topic = f"{self.topic_prefix}/devices/{device_id}/commands"
        self.client.publish(topic, json.dumps(envelope, ensure_ascii=False), qos=1)

    def publish_cancel(self, device_id: str, envelope: Dict[str, Any]) -> None:
        topic = f"{self.topic_prefix}/devices/{device_id}/commands"
        self.client.publish(topic, json.dumps(envelope, ensure_ascii=False), qos=1)

    def _handle_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        try:
            self.on_message(message.topic, payload)
        except Exception as error:  # MQTT 回调不能因单条坏消息退出接收线程。
            print(f"忽略 MQTT 消息 {message.topic}: {error}", file=sys.stderr)
