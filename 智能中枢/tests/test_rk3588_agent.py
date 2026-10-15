import unittest
from unittest.mock import patch

from agent_gateway.rk3588 import RK3588AgentClient, RK3588AgentError


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

    def test_route_message_sends_v1_context_to_auto_route(self):
        client = RK3588AgentClient("http://rk3588.local", timeout_seconds=3)
        with patch.object(
            client,
            "_request",
            return_value={"route": "chat", "reply": "你好"},
        ) as request:
            response = client.route_message(
                request_id="request-001",
                conversation_id="conversation-001",
                input_text="你好",
                target_device_id="bean_ye_1",
            )

        self.assertEqual(response["route"], "chat")
        request.assert_called_once_with(
            "POST",
            "/api/route",
            {
                "protocol_version": "1.0",
                "request_id": "request-001",
                "conversation_id": "conversation-001",
                "source": "desktop",
                "task": "auto",
                "input": "你好",
                "target_device_id": "bean_ye_1",
            },
            timeout_seconds=30.0,
        )

    def test_route_message_uses_at_least_30_second_timeout(self):
        client = RK3588AgentClient("http://rk3588.local", timeout_seconds=15)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return '{"reply":"收到"}'.encode("utf-8")

        with patch("agent_gateway.rk3588.urllib.request.urlopen", return_value=Response()) as urlopen:
            client.route_message("request-001", "conversation-001", "你好")

        self.assertGreaterEqual(urlopen.call_args.kwargs["timeout"], 30)

    def test_route_timeout_retries_with_the_same_request_id(self):
        client = RK3588AgentClient("http://rk3588.local", timeout_seconds=15)
        with patch.object(
            client,
            "_request",
            side_effect=[
                RK3588AgentError("gateway request timed out", is_timeout=True),
                {"reply": "重试成功"},
            ],
        ) as request:
            response = client.route_message(
                request_id="request-retry",
                conversation_id="conversation-001",
                input_text="查询状态",
            )

        self.assertEqual(response["reply"], "重试成功")
        self.assertEqual(request.call_count, 2)
        first_payload = request.call_args_list[0].args[2]
        second_payload = request.call_args_list[1].args[2]
        self.assertEqual(first_payload["request_id"], "request-retry")
        self.assertEqual(second_payload["request_id"], "request-retry")
        self.assertEqual(first_payload, second_payload)

    def test_reply_text_hides_routing_diagnostics(self):
        response = {
            "reply": "今日新闻摘要",
            "sources": [{"url": "https://example.com"}],
            "route_detail": "web_search",
            "search_query": "今日新闻",
            "error_code": "WEB_SUMMARY_UNAVAILABLE",
        }
        self.assertEqual(RK3588AgentClient.reply_text(response), "今日新闻摘要")

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
                conversation_id="conversation-001",
                input_text="开始录音",
                target_device_id="bean_ye_1",
            )

        self.assertEqual(response["status"], "planned")
        request.assert_called_once_with(
            "POST",
            "/api/hub/v1/commands",
            {
                "protocol_version": "1.0",
                "request_id": "request-001",
                "conversation_id": "conversation-001",
                "source": "desktop",
                "dispatch": False,
                "input": "开始录音",
                "target_device_id": "bean_ye_1",
            },
        )

    def test_dispatch_command_requires_plan_id_and_uses_standard_fields(self):
        client = RK3588AgentClient("http://rk3588.local", timeout_seconds=3)
        with patch.object(
            client,
            "_request",
            return_value={"status": "accepted", "request_id": "execute-001"},
        ) as request:
            response = client.dispatch_hub_command(
                request_id="execute-001",
                conversation_id="conversation-001",
                plan_id="plan-001",
                target_device_id="bean_ye_1",
            )

        self.assertEqual(response["status"], "accepted")
        request.assert_called_once_with(
            "POST",
            "/api/hub/v1/commands",
            {
                "protocol_version": "1.0",
                "request_id": "execute-001",
                "conversation_id": "conversation-001",
                "source": "desktop",
                "dispatch": True,
                "target_device_id": "bean_ye_1",
                "plan_id": "plan-001",
            },
        )

    def test_confirm_command_sends_plan_id(self):
        client = RK3588AgentClient("http://rk3588.local", timeout_seconds=3)
        with patch.object(client, "_request", return_value={"status": "confirmed"}) as request:
            response = client.confirm_hub_command(
                request_id="request-001",
                plan_id="plan-001",
                conversation_id="conversation-001",
            )

        self.assertEqual(response["status"], "confirmed")
        request.assert_called_once_with(
            "POST",
            "/api/hub/v1/commands/request-001/confirm",
            {
                "protocol_version": "1.0",
                "request_id": "request-001",
                "conversation_id": "conversation-001",
                "plan_id": "plan-001",
                "source": "desktop",
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
