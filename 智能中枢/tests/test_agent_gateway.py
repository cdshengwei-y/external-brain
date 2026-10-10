import unittest
from unittest.mock import patch

from agent_gateway.service import HubApiClient, TextAgent


class FakeHub:
    def __init__(self):
        self.commands = []

    def list_devices(self):
        return [
            {
                "device_id": "bean_ye_1",
                "status": "online",
                "capabilities": ["record.start", "record.stop", "status.read"],
            }
        ]

    def get_device_state(self, device_id):
        return {
            "device_id": device_id,
            "available": True,
            "recording": False,
            "sd_total_mb": 7571,
            "sd_free_mb": 7570,
            "wifi_connected": True,
            "wifi_ip": "192.168.0.125",
            "wifi_rssi_dbm": -49,
            "mqtt_connected": True,
            "current_file": "",
        }

    def submit_command(self, device_id, capability, params, conversation_id, policy):
        self.commands.append(
            {
                "device_id": device_id,
                "capability": capability,
                "params": params,
                "conversation_id": conversation_id,
                "policy": policy,
            }
        )
        return {"command_id": "command-001"}

    def wait_for_command(self, command_id, timeout_ms=30000):
        return {
            "command": {"command_id": command_id, "state": "succeeded"},
            "completed": True,
            "timed_out": False,
        }


class TextAgentTest(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.agent = TextAgent(self.hub, device_id="bean_ye_1")

    def test_start_recording_is_translated_to_hub_command(self):
        self.assertEqual(self.agent.respond("开始录音"), "已经开始录音。")
        self.assertEqual(self.hub.commands[0]["capability"], "record.start")
        self.assertEqual(self.hub.commands[0]["device_id"], "bean_ye_1")

    def test_status_query_returns_real_state_summary(self):
        response = self.agent.respond("查询状态")

        self.assertIn("设备状态查询完成", response)
        self.assertIn("录音：未录音", response)
        self.assertIn("TF 卡：7570 / 7571 MB 可用", response)
        self.assertIn("Wi-Fi：已连接 192.168.0.125（-49 dBm）", response)
        self.assertIn("MQTT：已连接", response)

    def test_unknown_request_is_not_sent_to_hub(self):
        response = self.agent.respond("打开厨房的灯")
        self.assertIn("我现在支持", response)
        self.assertEqual(self.hub.commands, [])

    def test_conversation_id_can_be_reused_for_follow_up_messages(self):
        conversation_id = "desktop-session-001"
        first_agent = TextAgent(
            self.hub,
            device_id="bean_ye_1",
            conversation_id=conversation_id,
        )
        second_agent = TextAgent(
            self.hub,
            device_id="bean_ye_1",
            conversation_id=conversation_id,
        )

        first_agent.respond("查询状态")
        second_agent.respond("停止录音")

        self.assertEqual(
            [command["conversation_id"] for command in self.hub.commands],
            [conversation_id, conversation_id],
        )

    def test_queued_command_is_not_reported_as_success(self):
        self.hub.wait_for_command = lambda command_id: {
            "command": {"command_id": command_id, "state": "queued"},
            "completed": False,
            "timed_out": True,
        }

        response = self.agent.respond("开始录音")

        self.assertEqual(self.agent.response_status, "queued")
        self.assertIn("已进入队列", response)

    def test_running_command_timeout_has_timeout_status(self):
        self.hub.wait_for_command = lambda command_id: {
            "command": {"command_id": command_id, "state": "running"},
            "completed": False,
            "timed_out": True,
        }

        response = self.agent.respond("开始录音")

        self.assertEqual(self.agent.response_status, "timeout")
        self.assertIn("等待超时", response)

    def test_wait_request_allows_the_hub_long_poll_window(self):
        client = HubApiClient("http://127.0.0.1:18787", timeout_seconds=10)

        with patch.object(client, "_request", return_value={}) as request:
            client.wait_for_command("command-001", timeout_ms=30000)

        request.assert_called_once_with(
            "GET",
            "/api/v1/commands/command-001/wait?timeout_ms=30000",
            timeout_seconds=31.0,
        )


if __name__ == "__main__":
    unittest.main()
