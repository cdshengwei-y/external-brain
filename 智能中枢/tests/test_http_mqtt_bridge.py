import tempfile
import threading
import time
import unittest

from hub_core.http_mqtt_bridge import HttpMqttBridge
from hub_core.errors import ConflictError
from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport


class HttpMqttBridgeTest(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = HubStore(self.database.name)
        self.transport = MemoryTransport()
        self.service = HubService(self.store, self.transport)
        self.service.register_device(
            {
                "device_id": "bean_ye_1",
                "device_type": "recording_beam",
                "protocol_version": "1.0",
                "firmware_version": "test",
                "capabilities": ["record.start", "record.stop", "status.read"],
            }
        )

    def tearDown(self):
        self.store.close()
        self.database.close()

    def test_maps_http_command_to_hub_and_returns_real_ack(self):
        bridge = HttpMqttBridge(self.service, wait_timeout_seconds=1)
        response = {}

        def execute():
            response["value"] = bridge.execute(
                {
                    "request_id": "hub-001",
                    "source": "rk3588",
                    "command": {
                        "device_id": "bean_ye_1",
                        "action": "record.start",
                        "params": {},
                    },
                }
            )

        worker = threading.Thread(target=execute)
        worker.start()
        deadline = time.time() + 1
        while not self.transport.commands and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.transport.commands), 1)

        self.service.acknowledge_command(
            "bean_ye_1",
            "hub-001",
            {"state": "succeeded", "result": {"message": "已经开始录音"}},
        )
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        status_code, body = response["value"]
        self.assertEqual(status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(body["message"], "已经开始录音")

    def test_returns_pending_without_claiming_success(self):
        bridge = HttpMqttBridge(self.service, wait_timeout_seconds=0)
        status_code, body = bridge.execute(
            {
                "request_id": "hub-002",
                "command": {
                    "device_id": "bean_ye_1",
                    "action": "status.read",
                    "params": {},
                },
            }
        )

        self.assertEqual(status_code, 202)
        self.assertFalse(body["ok"])
        self.assertTrue(body["pending"])
        self.assertEqual(body["state"], "accepted")

    def test_retries_same_request_id_without_duplicate_mqtt_command(self):
        bridge = HttpMqttBridge(self.service, wait_timeout_seconds=0)
        payload = {
            "request_id": "hub-003",
            "command": {
                "device_id": "bean_ye_1",
                "action": "record.stop",
                "params": {},
            },
        }
        bridge.execute(payload)
        self.service.acknowledge_command(
            "bean_ye_1",
            "hub-003",
            {"state": "succeeded", "result": {"message": "已经停止录音"}},
        )

        status_code, body = bridge.execute(payload)

        self.assertEqual(status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.transport.commands), 1)

    def test_same_request_id_cannot_change_command(self):
        bridge = HttpMqttBridge(self.service, wait_timeout_seconds=0)
        bridge.execute(
            {
                "request_id": "hub-004",
                "command": {
                    "device_id": "bean_ye_1",
                    "action": "record.start",
                    "params": {},
                },
            }
        )

        with self.assertRaises(ConflictError) as context:
            bridge.execute(
                {
                    "request_id": "hub-004",
                    "command": {
                        "device_id": "bean_ye_1",
                        "action": "record.stop",
                        "params": {},
                    },
                }
            )

        self.assertIn("request_id 已被其他命令使用", str(context.exception))


if __name__ == "__main__":
    unittest.main()
