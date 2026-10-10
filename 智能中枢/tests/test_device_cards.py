import unittest

from desktop_app.device_cards import (
    device_display_name,
    format_device_state,
    is_real_device,
)


class DeviceCardFormattingTest(unittest.TestCase):
    def test_simulator_is_not_displayed_as_real_device(self):
        self.assertFalse(
            is_real_device(
                {
                    "device_id": "recording-beam-sim-001",
                    "device_type": "recording_beam",
                    "firmware_version": "simulator-agent-test",
                }
            )
        )
        self.assertFalse(
            is_real_device(
                {
                    "device_id": "speaker-001",
                    "device_type": "speaker",
                    "metadata": {"source": "simulator"},
                }
            )
        )

    def test_recording_bean_card_uses_real_state_fields(self):
        device = {
            "device_id": "bean_ye_1",
            "device_type": "recording_bean",
            "status": "online",
        }
        state = {
            "available": True,
            "recording": True,
            "sd_total_mb": 7571,
            "sd_free_mb": 7570,
            "wifi_connected": True,
            "wifi_ip": "192.168.0.125",
            "wifi_rssi_dbm": -48,
            "mqtt_connected": True,
            "current_file": "20261009_120000.wav",
            "updated_at": "2026-10-09T03:37:50Z",
        }

        display = format_device_state(device, state)

        self.assertIn("录音：录音中", display)
        self.assertIn("TF 卡：7570 / 7571 MB 可用", display)
        self.assertIn("Wi-Fi：已连接 192.168.0.125（-48 dBm）", display)
        self.assertIn("MQTT：已连接", display)

    def test_offline_card_marks_historical_state(self):
        device = {
            "device_id": "bean_ye_1",
            "device_type": "recording_bean",
            "status": "offline",
        }
        state = {
            "available": True,
            "recording": False,
            "updated_at": "2026-10-09T03:37:50Z",
        }

        display = format_device_state(device, state)

        self.assertIn("设备当前离线，以下为最近一次有效状态", display)
        self.assertIn("最近状态（设备离线）", display)

    def test_offline_card_without_state_shows_last_connection_time(self):
        display = format_device_state(
            {
                "device_id": "bean_ye_1",
                "device_type": "recording_bean",
                "status": "offline",
                "last_seen": "2026-10-09T03:37:50Z",
            },
            {"available": False},
        )

        self.assertIn("设备当前离线，暂无可用的历史状态", display)
        self.assertIn("最近连接时间：2026-10-09T03:37:50Z", display)

    def test_device_name_has_recording_bean_label(self):
        self.assertEqual(
            device_display_name(
                {"device_id": "bean_ye_1", "device_type": "recording_bean"}
            ),
            "录音豆",
        )


if __name__ == "__main__":
    unittest.main()
