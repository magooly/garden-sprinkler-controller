"""Persistent five-minute telemetry, missing readings and scheduling tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tapo_details import TapoDetails, TapoSamples, daily_energy
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

    def test_288_slots_restart_persistence_identity_and_missing_values(self):
        self.store.save(PLUG.mac, self.bucket, self.row)
        end = datetime.fromtimestamp(self.bucket + 60, timezone.utc)
        result = TapoSamples(self.config).history(PLUG.mac, end)
        self.assertEqual(len(result["samples"]), 288)
        self.assertEqual(result["sample_interval_seconds"], 300)
        timestamps = [datetime.fromisoformat(row["timestamp"]).timestamp() for row in result["samples"]]
        self.assertTrue(all(last - first == 300 for first, last in zip(timestamps, timestamps[1:])))
        self.assertEqual(result["samples"][-1]["power_w"], 123.4)
        self.assertIsNone(result["samples"][0]["power_w"])
        self.assertTrue(result["samples"][0]["error"])
        self.assertTrue(all(r["power_w"] is None for r in self.store.history("other", end)["samples"]))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_retains_eight_days_and_no_duplicate_bucket(self):
        self.store.save(PLUG.mac, self.bucket - 9 * 86400, self.row)
        self.store.save(PLUG.mac, self.bucket - 8 * 86400, self.row)
        self.store.save(PLUG.mac, self.bucket, self.row)
        self.store.save(PLUG.mac, self.bucket, {**self.row, "power_w": 0.0})
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM samples").fetchone()[0], 2)
        result = self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))
        self.assertEqual(result["samples"][-1]["power_w"], 0.0)

    def test_all_history_ranges_and_invalid_values(self):
        end = datetime.fromtimestamp(self.bucket, timezone.utc)
        for hours, count in ((6, 72), (24, 288), (168, 2016)):
            self.assertEqual(len(self.store.history(PLUG.mac, end, hours)["samples"]), count)
        for hours in (True, 0, 7, "24", 169):
            with self.assertRaises(ValueError):
                self.store.history(PLUG.mac, end, hours)

    def test_duplicate_logs_do_not_change_reads_and_recovery_resets_warning(self):
        cache = TapoDetails((PLUG,), CREDENTIALS, self.config)
        with patch("tapo_details.read_details", AsyncMock(side_effect=OSError("offline"))) as read:
            with self.assertLogs(level="WARNING") as logs:
                for _ in range(13):
                    asyncio.run(cache._read())
            self.assertEqual(read.call_count, 13)
            self.assertEqual(len(logs.records), 2)
        with patch("tapo_details.read_details", AsyncMock(return_value=self.row)):
            with self.assertLogs(level="INFO"):
                asyncio.run(cache._read())
        self.assertFalse(cache.failures)

    def test_energy_requires_complete_boundaries_and_no_reset_or_gaps(self):
        start = datetime(2026, 11, 12).astimezone()
        rows = [
            dict(self.row, timestamp=(start + timedelta(minutes=5 * index)).isoformat(),
                 updated=(start + timedelta(minutes=5 * index)).isoformat(), month_kwh=index * .01)
            for index in range(289)
        ]
        end = start + timedelta(days=1)
        total = daily_energy(rows, end)[0]
        self.assertAlmostEqual(total["kwh"], 2.88)
        for broken in (rows[1:], rows[:-1], rows[:50] + rows[53:],
                       [dict(row, month_kwh=0 if index == 100 else row["month_kwh"])
                        for index, row in enumerate(rows)],
                       [dict(row, error="Offline" if index == 100 else "")
                        for index, row in enumerate(rows)]):
            self.assertIsNone(daily_energy(broken, end)[0]["kwh"])

    def test_failed_sample_does_not_persist_previous_reading_as_current(self):
        cache = TapoDetails((PLUG,), CREDENTIALS, self.config)
        cache.rows[0] = self.row
        with patch("tapo_details.read_details", AsyncMock(side_effect=OSError("offline"))):
            asyncio.run(cache._read(self.bucket))
        row = self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))["samples"][-1]
        self.assertIsNone(row["power_w"])
        self.assertEqual(row["error"], "offline")
        self.assertIsNone(cache.rows[0]["power_w"])

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
            with patch("tapo_details.time.time", return_value=self.bucket + 299):
                cache.sample_due()
            self.assertEqual(thread.call_count, 1)
            with patch("tapo_details.time.time", return_value=self.bucket + 300):
                cache.sample_due()
            self.assertEqual(thread.call_count, 2)
            self.assertEqual(thread.call_args.kwargs["args"], (self.bucket + 300,))

    def test_legacy_quarter_hour_history_preserves_readings_without_filling_new_slots(self):
        for offset in (-900, 0):
            self.store.save(PLUG.mac, self.bucket + offset, self.row)
        result = self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))
        last = result["samples"][-4:]
        self.assertEqual([row["power_w"] for row in last], [123.4, None, None, 123.4])
        self.assertTrue(last[1]["error"])
        self.assertTrue(last[2]["error"])

    def test_storage_errors_are_explicit_without_raising_switching_fault(self):
        with patch.object(self.store, "_connect", side_effect=OSError("Disk full")):
            self.store.save(PLUG.mac, self.bucket, self.row)
            self.assertTrue(self.store.error)
            with self.assertRaises(OSError):
                self.store.history(PLUG.mac, datetime.fromtimestamp(self.bucket, timezone.utc))
