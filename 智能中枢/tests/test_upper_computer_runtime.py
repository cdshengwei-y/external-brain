import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop_app.upper_computer import HubRuntime


class HubRuntimeTest(unittest.TestCase):
    def test_starts_and_stops_device_registrar_with_runtime(self):
        registrar = Mock()
        runtime = HubRuntime(registrar=registrar)

        with patch.object(HubRuntime, "_port_open", return_value=True), patch.object(
            HubRuntime, "_health_ok", return_value=True
        ), patch.object(HubRuntime, "_bridge_health_ok", return_value=True):
            runtime.ensure_started()
            registrar.start.assert_called_once_with()

        runtime.stop_started_processes()
        registrar.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
