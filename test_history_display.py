"""Formatting of activity history without changing saved timestamps."""

import unittest
from datetime import datetime
from unittest.mock import patch

from app import format_history_event


class HistoryDisplayTests(unittest.TestCase):
    def test_older_event_displays_year(self) -> None:
        event = {"timestamp": "2025-11-10T12:49:16+10:00", "channel": None,
                 "name": "Controller", "event": "STARTED", "reason": "Startup"}
        with patch("app.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 10)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertTrue(format_history_event(event).startswith("10/11/2025 12:49"))

    def test_channel_and_system_use_recorded_local_time(self) -> None:
        event = {
            "timestamp": "2026-11-10T12:49:16+10:00",
            "channel": 6, "name": "Wherly Fan", "event": "ON", "reason": "Manual",
        }
        with patch("app.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 10)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(
                format_history_event(event),
                "10/11 12:49 - Channel 7, Wherly Fan - ON - Manual",
            )
            event.update(channel=None, name="Controller", event="STARTED", reason="Startup")
            self.assertEqual(format_history_event(event), "10/11 12:49 - System - STARTED - Startup")

    def test_invalid_timestamp_is_explicit(self) -> None:
        event = {"timestamp": "invalid", "channel": None, "name": "Controller",
                 "event": "FAULT", "reason": "Offline"}
        self.assertIn("Invalid timestamp: invalid", format_history_event(event))
