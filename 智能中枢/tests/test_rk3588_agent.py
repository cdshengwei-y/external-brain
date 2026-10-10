import unittest
from unittest.mock import patch

from agent_gateway.rk3588 import RK3588AgentClient


class RK3588AgentClientTest(unittest.TestCase):
    def test_run_agent_sends_text_and_audio_to_auto_route(self):
        client = RK3588AgentClient("http://rk3588.local", timeout_seconds=3)
        with patch.object(
            client,
            "_request",
            return_value={"content": "收到"},
        ) as request:
            response = client.run_agent(
                input_text="开始录音",
                audio_data_url="data:audio/wav;base64,AAAA",
            )

        self.assertEqual(response["content"], "收到")
        request.assert_called_once_with(
            "POST",
            "/api/agent",
            {
                "task": "auto",
                "input": "开始录音",
                "audio": "data:audio/wav;base64,AAAA",
            },
        )

    def test_extracts_explicit_actions_only(self):
        response = {
            "content": "```json\n{\"actions\": [{\"device_id\": \"bean_ye_1\", \"capability\": \"record.start\", \"params\": {}}]}\n```"
        }

        self.assertEqual(
            RK3588AgentClient.extract_actions(response),
            [
                {
                    "device_id": "bean_ye_1",
                    "capability": "record.start",
                    "params": {},
                }
            ],
        )

    def test_ignores_natural_language_without_structured_action(self):
        self.assertEqual(
            RK3588AgentClient.extract_actions(
                {"content": "好的，我可以帮你开始录音。"}
            ),
            [],
        )

    def test_accepts_actions_from_top_level_response(self):
        self.assertEqual(
            RK3588AgentClient.extract_actions(
                {
                    "content": "已经规划完成",
                    "actions": [
                        {
                            "device_id": "bean_ye_1",
                            "capability": "record.stop",
                            "params": {},
                        }
                    ],
                }
            ),
            [
                {
                    "device_id": "bean_ye_1",
                    "capability": "record.stop",
                    "params": {},
                }
            ],
        )

    def test_plan_command_uses_documented_hardware_gateway_endpoint(self):
        client = RK3588AgentClient(
            "http://rk3588.local",
            timeout_seconds=3,
            api_key="test-key",
        )
        with patch.object(
            client,
            "_request",
            return_value={"status": "planned", "request_id": "request-001"},
        ) as request:
            response = client.plan_hub_command(
                request_id="request-001",
                input_text="开始录音",
                target_device="bean_ye_1",
            )

        self.assertEqual(response["status"], "planned")
        request.assert_called_once_with(
            "POST",
            "/api/hub/v1/commands",
            {
                "request_id": "request-001",
                "dispatch": False,
                "input": "开始录音",
                "target_device": "bean_ye_1",
            },
        )

    def test_hub_health_uses_hardware_gateway_path(self):
        client = RK3588AgentClient("http://rk3588.local")
        with patch.object(
            client,
            "_request",
            return_value={"status": "ok", "devices": []},
        ) as request:
            response = client.hub_health()

        self.assertEqual(response["status"], "ok")
        request.assert_called_once_with("GET", "/api/hub/v1/health")


if __name__ == "__main__":
    unittest.main()
