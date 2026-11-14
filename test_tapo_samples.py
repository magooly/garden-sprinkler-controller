"""Persistent quarter-hour telemetry, missing readings and scheduling tests."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tapo_details import TapoDetails, TapoSamples
from test_tapo_details import PLUG, CREDENTIALS


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(self.directory) / "settings.json"
        self.store = TapoSamples(self.config)
        self.bucket = int(datetime(2026, 11, 14, 12, tzinfo=timezone.utc).timestamp())
        self.row = {
            "state": True, "power_w": 123.4, "today_kwh": 1.2, "month_kwh": 3.4,
            "voltage_v": 240.0, "current_a": 0.5, "updated": "2026-11-14T12:00:03+00:00", "error": "",
        }

    def test_96_slots_restart_persistence_identity_and_missing_values(self):
        self.store.save(PLUG.mac, self.bucket, self.row)
        end = datetime.fromtimestamp(self.bucket + 60, timezone.utc)
        result = TapoSamples(self.config).history(PLUG.mac, end)
        self.assertEqual(len(result["samples"]), 96)
        self.assertEqual(result["samples"][-1]["power_w"], 123.4)
        self.assertIsNone(result["samples"][0]["power_w"])
        self.assertTrue(result["samples"][0]["error"])
        self.assertTrue(all(r["power_w"] is None for r in self.store.history("other", end)["samples"]))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_retains_25_hours_and_no_duplicate_bucket(self):
        self.store.save(PLUG.mac, self.bucket - 26 * 3600, self.row)
        self.store.save(PLUG.mac, self.bucket - 25 * 3600, self.row)
        self.store.save(PLUG.mac, self.bucket, self.row)
        self.store.save(PLUG.mac, self.bucket, {**self.row, "power_w": 0.0})
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM samples").fetchone()[0], 2)
        result = self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))
        self.assertEqual(result["samples"][-1]["power_w"], 0.0)

    def test_failed_sample_does_not_persist_previous_reading_as_current(self):
        cache = TapoDetails((PLUG,), CREDENTIALS, self.config)
        cache.rows[0] = self.row
        with patch("tapo_details.read_details", AsyncMock(side_effect=OSError("offline"))):
            asyncio.run(cache._read(self.bucket))
        row = self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))["samples"][-1]
        self.assertIsNone(row["power_w"])
        self.assertEqual(row["error"], "offline")
        self.assertEqual(cache.rows[0]["power_w"], 123.4)

    def test_sample_due_nonblocking_once_per_bucket_and_skips_busy_reader(self):
        cache = TapoDetails((PLUG,), CREDENTIALS, self.config)
        with patch("tapo_details.time.time", return_value=self.bucket), patch("tapo_details.threading.Thread") as thread:
            cache.reading = True
            cache.sample_due()
            thread.assert_not_called()
            cache.reading = False
            cache.sample_due()
            cache.reading = False
            cache.sample_due()
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(thread.call_args.kwargs["args"], (self.bucket,))
            cache.reading = False
            with patch("tapo_details.time.time", return_value=self.bucket + 900):
                cache.sample_due()
            self.assertEqual(thread.call_count, 2)

    def test_storage_errors_are_explicit_without_raising_switching_fault(self):
        with patch.object(self.store, "_connect", side_effect=OSError("Disk full")):
            self.store.save(PLUG.mac, self.bucket, self.row)
            self.assertTrue(self.store.error)
            with self.assertRaises(OSError):
                self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))
