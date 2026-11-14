"""24-hour controller history boundaries and uncertainty tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from contextlib import closing

from activity_history import ActivityHistory


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.history = ActivityHistory(Path(self.directory) / "settings.json")
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

    def event(self, hours, event, channel=8, reason="test"):
        with closing(self.history._connect()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO events (timestamp, channel, name, event, reason) VALUES (?,?,?,?,?)",
                    ((self.now + timedelta(hours=hours)).isoformat(), channel, "Plug", event, reason),
                )

    def test_no_history_is_unknown_not_off(self):
        result = self.history.timeline(8, self.now)
        self.assertEqual(result["segments"][0]["state"], "Unknown")
        self.assertEqual(result["segments"][0]["seconds"], 86400)
        self.assertEqual(result["on_seconds"], 0)

    def test_prior_state_boundaries_duration_and_other_channels(self):
        self.event(-25, "ON")
        self.event(-20, "OFF")
        self.event(-18, "ON", channel=9)
        self.event(-2, "ON", reason="Manual")
        self.event(1, "OFF")
        result = self.history.timeline(8, self.now)
        self.assertEqual([r["state"] for r in result["segments"]], ["ON", "OFF", "ON"])
        self.assertEqual([r["seconds"] for r in result["segments"]], [14400, 64800, 7200])
        self.assertEqual(result["on_seconds"], 21600)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][-1]["reason"], "Manual")

    def test_restart_and_fault_create_unknown_until_next_recorded_switch(self):
        self.event(-25, "ON")
        self.event(-10, "STARTED", channel=None)
        self.event(-8, "OFF")
        self.event(-6, "ON")
        self.event(-4, "FAULT", channel=None)
        result = self.history.timeline(8, self.now)
        self.assertEqual([r["state"] for r in result["segments"]], ["ON", "Unknown", "OFF", "ON", "Unknown"])
        self.assertEqual(sum(r["seconds"] for r in result["segments"]), 86400)

    def test_known_history_loss_marks_whole_range_unknown(self):
        self.event(-25, "ON")
        self.history.error = "Some events may be missing"
        result = self.history.timeline(8, self.now)
        self.assertEqual(result["segments"][0]["state"], "Unknown")
        self.assertEqual(result["on_seconds"], 0)
        self.assertTrue(result["warning"])

    def test_bad_channel_rejected(self):
        for channel in (None, True, 7, 16, "8"):
            with self.assertRaises(ValueError):
                self.history.timeline(channel, self.now)

    def test_requested_and_confirmed_are_separate_and_outage_is_unknown(self):
        self.event(-25, "REQUESTED OFF", channel=15)
        self.event(-23, "REQUESTED ON", channel=15)
        self.event(-22, "ON", channel=15)
        self.event(-12, "UNAVAILABLE", channel=15)
        self.event(-3, "REQUESTED OFF", channel=15)
        self.event(-1, "OFF", channel=15)
        result = self.history.timeline(15, self.now)
        self.assertEqual([r["state"] for r in result["segments"]], ["Unknown", "ON", "Unknown", "OFF"])
        self.assertEqual([r["state"] for r in result["requested_segments"]], ["OFF", "ON", "OFF"])
        self.assertEqual(result["on_seconds"], 10 * 3600)
        self.assertEqual(sum(r["seconds"] for r in result["requested_segments"]), 86400)
