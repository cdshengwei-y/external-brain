import threading
import time
import unittest
from unittest.mock import Mock

from agent_gateway.registration import (
    RK3588DeviceRegistration,
    RK3588DeviceRegistrar,
)
from agent_gateway.rk3588 import RK3588AgentError


class RK3588DeviceRegistrarTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.registration = RK3588DeviceRegistration(
            device_id="bean_ye_1",
            name="录音豆",
            base_url="http://192.168.0.107:19700/",
            capabilities=("record.start", "record.stop", "status.read"),
        )

    def test_registers_missing_device(self):
        self.client.list_hub_devices.return_value = {"devices": []}
        registrar = RK3588DeviceRegistrar(self.client, self.registration)

        self.assertEqual(registrar.sync_once(), "registered")
        self.client.register_hub_device.assert_called_once_with(
            device_id="bean_ye_1",
            name="录音豆",
            base_url="http://192.168.0.107:19700",
            capabilities=["record.start", "record.stop", "status.read"],
        )

    def test_does_not_reregister_matching_device(self):
        self.client.list_hub_devices.return_value = {
            "devices": [
                {
                    "device_id": "bean_ye_1",
                    "name": "录音豆",
                    "base_url": "http://192.168.0.107:19700",
                    "capabilities": [
                        "record.start",
                        "record.stop",
                        "status.read",
                    ],
                }
            ]
        }
        registrar = RK3588DeviceRegistrar(self.client, self.registration)

        self.assertEqual(registrar.sync_once(), "already_registered")
        self.client.register_hub_device.assert_not_called()

    def test_calibrates_missing_registration_fields_once(self):
        self.client.list_hub_devices.return_value = {
            "devices": [
                {
                    "device_id": "bean_ye_1",
                    "name": "录音豆",
                    "capabilities": [
                        "record.start",
                        "record.stop",
                        "status.read",
                    ],
                }
            ]
        }
        registrar = RK3588DeviceRegistrar(self.client, self.registration)

        self.assertEqual(registrar.sync_once(), "registered")
        self.assertEqual(registrar.sync_once(), "already_registered")
        self.client.register_hub_device.assert_called_once()

    def test_updates_existing_device_when_configuration_differs(self):
        self.client.list_hub_devices.return_value = {
            "devices": [
                {
                    "device_id": "bean_ye_1",
                    "name": "旧录音豆",
                    "base_url": "http://192.168.0.99:19700",
                    "capabilities": ["record.start"],
                }
            ]
        }
        registrar = RK3588DeviceRegistrar(self.client, self.registration)

        self.assertEqual(registrar.sync_once(), "registered")
        self.client.register_hub_device.assert_called_once()

    def test_retries_after_rk3588_connection_failure(self):
        ready = threading.Event()
        calls = {"count": 0}

        def list_devices():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RK3588AgentError("RK3588 暂不可用")
            ready.set()
            return {"devices": []}

        self.client.list_hub_devices.side_effect = list_devices
        registrar = RK3588DeviceRegistrar(
            self.client,
            self.registration,
            check_interval_seconds=1,
            initial_retry_seconds=0.01,
            max_retry_seconds=0.02,
        )

        registrar.start()
        self.assertTrue(ready.wait(1))
        registrar.stop()

        self.assertGreaterEqual(calls["count"], 2)
        self.assertEqual(registrar.last_status, "stopped")
        self.client.register_hub_device.assert_called_once()


if __name__ == "__main__":
    unittest.main()
