"""Persistent five-minute telemetry, missing readings and scheduling tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tapo_details import TapoDetails, TapoSamples, daily_energy, voltage_periods, voltage_interruptions, parse_electricity_rate, estimated_costs, dollars_to_rate, rate_to_dollars
from tapo_plugs import Plug
from test_tapo_details import PLUG, CREDENTIALS


class SampleTests(unittest.TestCase):
    def test_dollar_entry_converts_exactly_without_reinterpreting_saved_cents(self):
        self.assertEqual(dollars_to_rate("0.26268"), "26.268")
        self.assertEqual(rate_to_dollars("26.268"), "0.26268")
        self.assertEqual(rate_to_dollars("26268"), "262.68")
        self.assertEqual(dollars_to_rate("0.15148"), "15.148")
        self.assertEqual(dollars_to_rate(" .000001 "), "0.0001")
        self.assertEqual(dollars_to_rate("0"), "0")
        self.assertIsNone(dollars_to_rate(""))
        self.assertEqual(rate_to_dollars(None), "")
        for text in ("-1", "NaN", "1e2", "0.1234567", "$0.3", "1" * 15):
            with self.assertRaises(ValueError):
                dollars_to_rate(text)
        self.assertEqual(estimated_costs({"today_kwh": 5}, dollars_to_rate("0.26268"))["today_kwh"], "1.31")
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(self.directory) / "settings.json"
        self.store = TapoSamples(self.config)
        self.bucket = int(datetime(2026, 11, 14, 12, tzinfo=timezone.utc).timestamp())
        self.row = {
            "state": True, "power_w": 123.4, "today_kwh": 1.2, "month_kwh": 3.4,
            "voltage_v": 240.0, "current_a": 0.5, "updated": "2026-11-14T12:00:03+00:00", "error": "",
        }

    def test_rate_validation_blank_zero_decimals_and_invalid_inputs(self):
        for value, expected in (("", None), ("  ", None), ("0", "0"), ("30.0000", "30"),
                                (" .125 ", "0.125"), ("000.5000", "0.5")):
            self.assertEqual(parse_electricity_rate(value), expected)
        for value in (None, True, 30, -1, "-1", "NaN", "inf", "1e2", "30c", "1.12345",
                      "1,5", "1" * 17):
            with self.assertRaises(ValueError):
                parse_electricity_rate(value)

    def test_rate_costs_persist_by_identity_and_clear_without_changing_samples(self):
        self.store.save(PLUG.mac, self.bucket, {**self.row, "today_kwh": 5, "month_kwh": 120})
        self.assertEqual(self.store.set_rate(PLUG.mac, "30", None), "30")
        end = datetime.fromtimestamp(self.bucket, timezone.utc)
        result = TapoSamples(self.config).history(PLUG.mac, end)
        self.assertEqual(result["rate_cents_per_kwh"], "30")
        self.assertEqual(result["estimated_costs"], {"today_kwh": "1.50", "month_kwh": "36.00"})
        self.assertIsNone(self.store.history("different-mac", end)["rate_cents_per_kwh"])
        with self.assertRaises(ValueError):
            self.store.set_rate(PLUG.mac, "40", None)
        with self.assertRaises(ValueError):
            self.store.set_rate(PLUG.mac, "40", "30.0")
        self.store.set_rate(PLUG.mac, "0", "30")
        self.assertEqual(self.store.history(PLUG.mac, end)["estimated_costs"]["today_kwh"], "0.00")
        self.store.set_rate(PLUG.mac, "", "0")
        result = self.store.history(PLUG.mac, end)
        self.assertIsNone(result["rate_cents_per_kwh"])
        self.assertEqual(result["samples"][-1]["today_kwh"], 5)
        with patch.object(self.store, "_connect", side_effect=OSError("Disk full")):
            with self.assertLogs(level="ERROR"):
                with self.assertRaises(OSError):
                    self.store.set_rate(PLUG.mac, "30", None)

    def test_estimated_costs_round_half_up_and_never_make_failed_readings_zero(self):
        self.assertEqual(estimated_costs({"today_kwh": 0.05, "month_kwh": 0}, "30"),
                         {"today_kwh": "0.02", "month_kwh": "0.00"})
        for value in (None, float("nan"), float("inf"), -1, True, "5"):
            self.assertIsNone(estimated_costs({"today_kwh": value}, "30")["today_kwh"])
        self.assertIsNone(estimated_costs({"today_kwh": 5, "error": "Offline"}, "0")["today_kwh"])
        self.assertIsNone(estimated_costs({"today_kwh": 5}, None)["today_kwh"])

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
            self.assertEqual(thread.call_args.kwargs["args"], (self.bucket, (0,)))
            cache.reading = False
            with patch("tapo_details.time.time", return_value=self.bucket + 299):
                cache.sample_due()
            self.assertEqual(thread.call_count, 1)
            with patch("tapo_details.time.time", return_value=self.bucket + 300):
                cache.sample_due()
            self.assertEqual(thread.call_count, 2)
            self.assertEqual(thread.call_args.kwargs["args"], (self.bucket + 300, (0,)))

    def test_hot_water_only_is_polled_each_minute_and_busy_reads_retry(self):
        hot = Plug("192.168.0.51", "12:34:56:78:9a:bd", off_peak=True)
        cache = TapoDetails((PLUG, hot), CREDENTIALS, self.config)
        with patch("tapo_details.time.time", return_value=self.bucket), patch("tapo_details.threading.Thread") as thread:
            cache.sample_due()
            self.assertEqual(thread.call_args.kwargs["args"], (self.bucket, (0, 1)))
            for minute in range(1, 5):
                with patch("tapo_details.time.time", return_value=self.bucket + 60 * minute):
                    cache.reading = True
                    cache.sample_due()
                    self.assertEqual(thread.call_count, minute)
                    cache.reading = False
                    cache.sample_due()
                    self.assertEqual(thread.call_args.kwargs["args"], (self.bucket + 60 * minute, (1,)))
            cache.reading = False
            with patch("tapo_details.time.time", return_value=self.bucket + 300):
                cache.sample_due()
            self.assertEqual(thread.call_args.kwargs["args"], (self.bucket + 300, (0, 1)))
        with patch("tapo_details.read_details", AsyncMock(return_value=self.row)) as read:
            asyncio.run(cache._read(self.bucket + 60, (1,)))
            read.assert_awaited_once_with(hot, CREDENTIALS)
        result = self.store.history(hot.mac, datetime.fromtimestamp(self.bucket + 120, timezone.utc), interval=60)
        saved = next(row for row in result["samples"] if row["updated"])
        self.assertEqual(saved["sample_interval_seconds"], 60)
        self.assertEqual(result["sample_interval_seconds"], 60)

    def test_one_minute_history_preserves_legacy_cadence_and_leaves_real_gaps(self):
        self.store.save(PLUG.mac, self.bucket - 600, self.row)
        self.store.save(PLUG.mac, self.bucket, {**self.row, "sample_interval_seconds": 60})
        end = datetime.fromtimestamp(self.bucket + 120, timezone.utc)
        result = self.store.history(PLUG.mac, end, interval=60)
        stamps = {int(datetime.fromisoformat(row["timestamp"]).timestamp()): row for row in result["samples"]}
        self.assertNotIn(self.bucket - 540, stamps)
        self.assertEqual(stamps[self.bucket - 600]["sample_interval_seconds"], 300)
        self.assertTrue(stamps[self.bucket - 300]["error"])
        self.assertTrue(stamps[self.bucket + 60]["error"])
        periods = result["voltage_periods"]
        voltage = [row for row in periods if row["state"] == "Voltage"]
        self.assertEqual([row["seconds"] for row in voltage], [300, 60])

    def test_voltage_periods_keep_every_transition_and_bound_missing_history(self):
        start = datetime.fromtimestamp(self.bucket, timezone.utc)
        values = [240, 240, None, 240, 0, -1, float("nan"), True, "240"]
        samples = [dict(self.row, timestamp=(start + timedelta(minutes=index)).isoformat(),
                        voltage_v=value, sample_interval_seconds=60)
                   for index, value in enumerate(values)]
        periods = voltage_periods(samples, start, start + timedelta(minutes=12))
        self.assertEqual([(p["state"], p["seconds"]) for p in periods],
                         [("Voltage", 120), ("Unknown", 60), ("Voltage", 60),
                          ("Zero", 60), ("Unknown", 420)])
        self.assertEqual(sum(p["seconds"] for p in periods), 720)
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(periods, periods[1:])))
        self.assertEqual(voltage_periods([], start, start + timedelta(hours=1))[0]["state"], "Unknown")
        # A new one-minute reading takes precedence over an overlapping legacy observation.
        samples = [dict(self.row, timestamp=start.isoformat()),
                   dict(self.row, timestamp=(start + timedelta(minutes=1)).isoformat(),
                        error="Offline", sample_interval_seconds=60)]
        self.assertEqual([(p["state"], p["seconds"]) for p in
                          voltage_periods(samples, start, start + timedelta(minutes=3))],
                         [("Voltage", 60), ("Unknown", 120)])

    def test_voltage_interruptions_group_zero_and_unknown_without_inventing_boundaries(self):
        start = datetime.fromtimestamp(self.bucket, timezone.utc)
        values = [None, 240, 0, None, 240, None, 240, None]
        samples = [dict(self.row, timestamp=(start + timedelta(minutes=index)).isoformat(),
                        voltage_v=value, sample_interval_seconds=60)
                   for index, value in enumerate(values)]
        periods = voltage_periods(samples, start, start + timedelta(minutes=len(values)))
        interruptions = voltage_interruptions(periods)
        stamp = lambda minute: (start + timedelta(minutes=minute)).isoformat()
        self.assertEqual(interruptions, [
            {"start": stamp(0), "return": stamp(1), "at_range_start": True, "zero": False, "unknown": True},
            {"start": stamp(2), "return": stamp(4), "at_range_start": False, "zero": True, "unknown": True},
            {"start": stamp(5), "return": stamp(6), "at_range_start": False, "zero": False, "unknown": True},
            {"start": stamp(7), "return": None, "at_range_start": False, "zero": False, "unknown": True},
        ])
        self.assertEqual(voltage_interruptions([{"state": "Voltage"}]), [])
        self.assertEqual(voltage_interruptions([{"state": "Zero", "start": stamp(0)}]), [
            {"start": stamp(0), "return": None, "at_range_start": True, "zero": True, "unknown": False}
        ])

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
