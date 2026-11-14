"""Service details requests must not alter watering or latch telemetry faults."""

import threading
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from service import Service
from test_service import FakeDevice


class DetailsServiceTests(unittest.TestCase):
    def test_timeline_rejects_unassigned_or_invalid_channel_without_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = FakeDevice()
            service = Service(Path(directory) / "settings.json", primary)
            before = list(primary.calls)
            for channel in (8, 7, 16, True, "8"):
                with self.assertRaises(ValueError):
                    service.dispatch({"command": "tapo_timeline", "channel": channel})
            self.assertEqual(primary.calls, before)
            self.assertFalse(service.fault)

    def test_read_command_does_not_drive_outputs_or_change_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = FakeDevice()
            service = Service(Path(directory) / "settings.json", primary)
            before = list(primary.calls)
            response = service.dispatch({"command": "tapo_details", "refresh": False})
            self.assertEqual(response, {"reading": False, "rows": []})
            self.assertEqual(primary.calls, before)
            self.assertFalse(service.fault)
            with self.assertRaises(ValueError):
                service.dispatch({"command": "tapo_details", "refresh": "true"})
            self.assertFalse(service.fault)

    def test_slow_details_worker_does_not_block_status_or_stop_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory) / "settings.json", FakeDevice())
            release = threading.Event()
            entered = threading.Event()
            done = threading.Event()
            def slow():
                entered.set()
                try:
                    release.wait(2)
                finally:
                    with service.tapo_details.lock:
                        service.tapo_details.reading = False
                    done.set()
            with patch.object(service.tapo_details, "_refresh", slow):
                try:
                    self.assertTrue(service.dispatch({"command": "tapo_details", "refresh": True})["reading"])
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(service.dispatch({"command": "status"})["fault"])
                    self.assertFalse(service.dispatch({"command": "stop_all"})["fault"])
                    self.assertFalse(done.is_set())
                finally:
                    release.set()
                    self.assertTrue(done.wait(1))
