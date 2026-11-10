"""History retention, persistence, and schedule prediction checks."""

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from activity_history import ActivityHistory, HISTORY_LIMIT
from controller import Zone, next_scheduled_start
from test_controller import schedule_for


class ActivityHistoryTests(unittest.TestCase):
    def test_filtered_pages_are_ordered_and_count_only_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = ActivityHistory(Path(directory) / "settings.json")
            for number in range(30):
                history.record(0, "Fan", "ON", str(number))
                history.record(1, "Light", "OFF", str(number))
            history.record(None, "Controller", "FAULT", "Offline")
            history.record(None, "Controller", "STARTED", "Startup")
            self.assertEqual(history.page(0, "0")["total"], 30)
            self.assertEqual(history.page(25, "0")["events"][0]["reason"], "4")
            self.assertEqual(history.page(0, "system")["total"], 2)
            self.assertEqual(history.page(0, "faults")["events"][0]["event"], "FAULT")
            for invalid in ("16", "-1", "", "00x", [], None):
                with self.assertRaises(ValueError):
                    history.page(0, invalid)

    def test_retains_1000_events_across_restart_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            history = ActivityHistory(config)
            for number in range(HISTORY_LIMIT + 5):
                history.record(0, "Fan", "ON", str(number))
            self.assertEqual(history.path.stat().st_mode & 0o777, 0o600)
            restored = ActivityHistory(config)
            first = restored.page(0)
            last = restored.page(975)
            self.assertEqual(first["total"], 1000)
            self.assertEqual(first["events"][0]["reason"], "1004")
            self.assertEqual(last["events"][-1]["reason"], "5")
            with self.assertRaises(ValueError):
                restored.page(1)

    def test_write_failure_is_visible_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = ActivityHistory(Path(directory) / "settings.json")
            with patch.object(history, "_connect", side_effect=OSError("disk full")):
                with self.assertLogs(level="ERROR"):
                    history.record(0, "Fan", "ON", "Manual")
                self.assertIn("disk full", history.error)
            history.record(0, "Fan", "OFF", "Stop channel")
            self.assertIn("missing", history.page(0)["warning"])

    def test_next_start_uses_saved_enabled_unpaused_schedule(self) -> None:
        schedule = list(schedule_for((0,), ("06:00", "06:20"), ("08:00", "08:30")))
        zone = Zone("Fan", True, tuple(schedule), paused_windows=(False, True, False, False))
        monday = datetime(2026, 11, 9, 5, 0)
        self.assertEqual(next_scheduled_start(zone, monday), datetime(2026, 11, 9, 6, 0))
        self.assertEqual(next_scheduled_start(zone, monday.replace(hour=6)), datetime(2026, 11, 16, 6, 0))
        self.assertIsNone(next_scheduled_start(Zone("Off"), monday))
