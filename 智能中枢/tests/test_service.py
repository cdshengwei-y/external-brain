import tempfile
import threading
import time
import unittest

from hub_core.service import HubService
from hub_core.store import HubStore
from hub_core.transport import MemoryTransport


class HubServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = HubStore(self.database.name)
        self.transport = MemoryTransport()
        self.service = HubService(self.store, self.transport)
        self.service.register_device(
            {
                "device_id": "recording-beam-001",
                "device_type": "recording_beam",
                "protocol_version": "1.0",
                "firmware_version": "test",
                "capabilities": ["button.press", "indicator.set"],
            }
        )

    def tearDown(self):
        self.store.close()
        self.database.close()

    def test_register_and_submit_command(self):
        command = self.service.submit_command(
            "recording-beam-001",
            {
                "conversation_id": "conversation-1",
                "capability": "indicator.set",
                "params": {"color": "blue", "mode": "solid"},
            },
        )

        self.assertEqual(command["state"], "accepted")
        self.assertEqual(len(self.transport.commands), 1)
        self.assertEqual(
            self.transport.commands[0]["envelope"]["command_id"],
            command["command_id"],
        )

    def test_replace_active_command(self):
        first = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "blue"},
            },
        )
        second = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "red"},
            },
        )

        self.assertEqual(self.service.get_command(first["command_id"])["state"], "superseded")
        self.assertEqual(self.service.get_command(second["command_id"])["state"], "accepted")
        self.assertEqual(len(self.transport.commands), 2)

    def test_queue_dispatches_after_previous_command_succeeds(self):
        first = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "blue"},
                "policy": "reject_if_busy",
            },
        )
        second = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "red"},
                "policy": "queue",
            },
        )

        self.assertEqual(second["state"], "queued")
        self.service.acknowledge_command(
            "recording-beam-001",
            first["command_id"],
            {"state": "succeeded", "result": {"applied": True}},
        )

        self.assertEqual(self.service.get_command(second["command_id"])["state"], "accepted")
        self.assertEqual(len(self.transport.commands), 2)

    def test_cancel_queued_command(self):
        first = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "blue"},
                "policy": "reject_if_busy",
            },
        )
        second = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "red"},
                "policy": "queue",
            },
        )

        cancelled = self.service.cancel_command(second["command_id"])

        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(len(self.transport.commands), 1)
        self.assertEqual(self.service.get_command(first["command_id"])["state"], "accepted")

    def test_wait_for_command_returns_after_device_ack(self):
        command = self.service.submit_command(
            "recording-beam-001",
            {
                "capability": "indicator.set",
                "params": {"color": "blue"},
            },
        )
        result = {}

        def wait_for_result():
            result["command"] = self.service.wait_for_command(
                command["command_id"],
                timeout_seconds=1,
            )

        waiter = threading.Thread(target=wait_for_result)
        waiter.start()
        time.sleep(0.02)
        self.service.acknowledge_command(
            "recording-beam-001",
            command["command_id"],
            {"state": "succeeded", "result": {"applied": True}},
        )
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result["command"]["state"], "succeeded")

    def test_mqtt_manifest_can_arrive_before_availability(self):
        self.service.handle_mqtt_message(
            "hub/v1/devices/mqtt-beam-001/availability",
            {"status": "online"},
        )
        self.service.handle_mqtt_message(
            "hub/v1/devices/mqtt-beam-001/manifest",
            {
                "device_id": "mqtt-beam-001",
                "device_type": "recording_beam",
                "protocol_version": "1.0",
                "capabilities": ["indicator.set"],
            },
        )
        self.assertEqual(
            self.service.get_device("mqtt-beam-001")["status"],
            "online",
        )

    def test_latest_device_state_is_persisted_and_returned(self):
        self.assertEqual(
            self.service.get_device_state("recording-beam-001"),
            {
                "device_id": "recording-beam-001",
                "available": False,
                "updated_at": None,
            },
        )

        self.service.ingest_state(
            "recording-beam-001",
            {
                "recording": True,
                "sd_total_mb": 7571,
                "sd_free_mb": 7570,
                "wifi_connected": True,
                "wifi_ip": "192.168.0.125",
                "mqtt_connected": True,
                "current_file": "20261009_120000.wav",
            },
        )
        state = self.service.get_device_state("recording-beam-001")

        self.assertTrue(state["available"])
        self.assertTrue(state["recording"])
        self.assertEqual(state["sd_free_mb"], 7570)
        self.assertEqual(state["wifi_ip"], "192.168.0.125")
        self.assertIsNotNone(state["updated_at"])


if __name__ == "__main__":
    unittest.main()
