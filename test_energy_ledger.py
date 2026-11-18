"""Permanent yearly counter accounting, tariff history and recovery tests."""

from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

import energy_ledger
from tapo_details import TapoSamples
from test_tapo_details import PLUG


class YearlyEnergyTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(self.directory) / "settings.json"
        self.store = TapoSamples(self.config)
        self.start = datetime(2026, 11, 17, 12).astimezone()
        self.store.set_rate(PLUG.mac, "26.268", None)

    def save(self, stamp, today, month, **extra):
        self.store.save(PLUG.mac, int(stamp.timestamp()), {
            "today_kwh": today, "month_kwh": month, "error": "", **extra,
        })
        self.assertEqual(self.store.error, "")

    def years(self, stamp=None):
        return TapoSamples(self.config).history(PLUG.mac, stamp or self.start)["yearly_energy"]

    def test_persistent_deltas_no_double_count_and_tariffs_are_not_retroactive(self):
        self.save(self.start, 5, 120)
        self.save(self.start + timedelta(minutes=5), 6, 121)
        self.save(self.start + timedelta(minutes=5), 7, 122)
        self.save(self.start - timedelta(minutes=5), 999, 999)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), 1)
        self.assertEqual(self.years()[0]["cost"], "0.26")
        # A tariff change between readings leaves that one interval unpriced.
        self.store.set_rate(PLUG.mac, "30", "26.268")
        self.save(self.start + timedelta(minutes=10), 7, 122)
        self.save(self.start + timedelta(minutes=15), 8, 123)
        total = self.years()[0]
        self.assertEqual(Decimal(total["kwh"]), 3)
        self.assertEqual(Decimal(total["unpriced_kwh"]), 1)
        self.assertEqual(total["cost"], "0.56")
        self.store.set_rate(PLUG.mac, "0", "30")
        self.save(self.start + timedelta(minutes=20), 8, 123)
        self.save(self.start + timedelta(minutes=25), 9, 124)
        self.assertEqual(self.years()[0]["cost"], "0.56")
        self.assertEqual(Decimal(self.years()[0]["unpriced_kwh"]), 1)

    def test_rate_changed_away_and_back_is_still_ambiguous(self):
        self.save(self.start, 0, 0)
        self.store.set_rate(PLUG.mac, "30", "26.268")
        self.store.set_rate(PLUG.mac, "26.268", "30")
        self.save(self.start + timedelta(minutes=5), 1, 1)
        self.assertEqual(Decimal(self.years()[0]["unpriced_kwh"]), 1)

    def test_missing_rate_is_unpriced_not_free_and_invalid_readings_do_not_count(self):
        self.store.set_rate(PLUG.mac, "", "26.268")
        self.save(self.start, 0, 0)
        self.save(self.start + timedelta(minutes=5), 1, 1)
        self.save(self.start + timedelta(minutes=10), 90, 90, error="Offline")
        self.save(self.start + timedelta(minutes=15), None, None)
        self.save(self.start + timedelta(minutes=20), 2, 2)
        total = self.years()[0]
        self.assertEqual(Decimal(total["kwh"]), 2)
        self.assertEqual(Decimal(total["unpriced_kwh"]), 2)
        self.assertTrue(total["incomplete"])

    def test_day_month_year_rollovers_and_resets_never_make_negative_or_double_usage(self):
        start = datetime(2026, 11, 29, 23, 55).astimezone()
        self.save(start, 5, 100)
        self.save(start + timedelta(minutes=10), .2, 100.3)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), Decimal(".3"))
        december = datetime(2026, 12, 1, 0, 5).astimezone()
        self.save(december, .4, .4)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), Decimal(".7"))
        self.assertTrue(self.years()[0]["incomplete"])
        self.save(december + timedelta(minutes=5), .1, .1)
        self.save(december + timedelta(minutes=10), .2, .2)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), Decimal(".8"))
        new_year = datetime(2027, 1, 1, 0, 5).astimezone()
        self.save(new_year, .25, .25)
        rows = self.years(new_year)
        self.assertEqual([row["year"] for row in rows], [2027, 2026])
        self.assertEqual(Decimal(rows[0]["kwh"]), Decimal(".25"))
        self.assertTrue(rows[0]["incomplete"])
        self.assertEqual(Decimal(rows[1]["kwh"]), Decimal(".8"))

    def test_daily_counter_fallback_and_decimal_rounding_only_at_display(self):
        self.save(self.start, 1, None)
        for i in range(1, 11):
            self.save(self.start + timedelta(minutes=5 * i), 1 + i / 1000, None)
        total = self.years()[0]
        self.assertEqual(Decimal(total["kwh"]), Decimal(".01"))
        self.assertEqual(total["cost"], "0.00")
        snapshot = self.store.yearly_backup()
        energy_ledger.validate(snapshot)
        self.assertEqual(Decimal(snapshot["years"][0][2]["cost"]), Decimal(".0026268"))

    def test_retention_and_duplicate_after_restart_do_not_erase_or_repeat_tally(self):
        self.save(self.start, 0, 0)
        self.save(self.start + timedelta(minutes=5), 1, 1)
        later = self.start + timedelta(days=10)
        self.save(later, 2, 4)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)
        self.save(later, 2, 4)
        self.assertEqual(Decimal(self.years(later)[0]["kwh"]), 4)
        self.assertEqual(self.years(later)[0]["since"], self.start.isoformat())
        self.assertEqual(self.store.history("other-mac", later)["yearly_energy"], [])

    def test_bootstrap_retained_samples_is_once_and_never_guesses_old_tariffs(self):
        with closing(self.store._connect()) as connection:
            with connection:
                for i in range(3):
                    connection.execute("INSERT INTO samples VALUES (?,?,?)", (
                        PLUG.mac, int(self.start.timestamp()) + 300 * i,
                        json.dumps({"today_kwh": i, "month_kwh": i, "error": ""}),
                    ))
        total = self.years()[0]
        self.assertEqual(Decimal(total["kwh"]), 2)
        self.assertEqual(Decimal(total["unpriced_kwh"]), 2)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), 2)
        self.save(self.start + timedelta(minutes=15), 3, 3)
        self.save(self.start + timedelta(minutes=20), 4, 4)
        total = self.years()[0]
        self.assertEqual(Decimal(total["kwh"]), 4)
        self.assertEqual(Decimal(total["unpriced_kwh"]), 3)
        self.assertEqual(total["cost"], "0.26")

    def test_backup_restore_preserves_cursor_totals_and_clears_excluded_samples(self):
        self.save(self.start, 0, 0)
        self.save(self.start + timedelta(minutes=5), 1, 1)
        snapshot = self.store.yearly_backup()
        self.save(self.start + timedelta(minutes=10), 2, 2)
        self.store.restore_electricity({PLUG.mac: "26.268"}, snapshot)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), 1)
        self.save(self.start + timedelta(minutes=5), 1, 1)
        self.save(self.start + timedelta(minutes=10), 2, 2)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), 2)
        self.store.restore_electricity({}, {"years": [], "cursors": []})
        self.assertEqual(self.years(), [])

    def test_invalid_backup_tallies_are_rejected(self):
        self.save(self.start, 0, 0)
        self.save(self.start + timedelta(minutes=5), 1, 1)
        valid = self.store.yearly_backup()
        for key, bad in (("cost", "-1"), ("kwh", "NaN"), ("unpriced_kwh", "2"),
                         ("incomplete", 1), ("last", 0)):
            value = json.loads(json.dumps(valid))
            value["years"][0][2][key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                energy_ledger.validate(value)
        for value in (None, {}, {"years": [], "cursors": valid["cursors"]}):
            with self.assertRaises(ValueError):
                energy_ledger.validate(value)
        value = json.loads(json.dumps(valid))
        value["cursors"][0][1]["seen"] += 300
        with self.assertRaises(ValueError):
            energy_ledger.validate(value)

    def test_rounding_summary_storage_failure_and_cursor_are_transactional(self):
        from unittest.mock import patch
        self.store.set_rate(PLUG.mac, "30", "26.268")
        self.save(self.start, 0, 0)
        self.save(self.start + timedelta(minutes=5), .05, .05)
        self.assertEqual(self.years()[0]["cost"], "0.02")
        self.assertIn("$0.02 estimated", energy_ledger.summary(self.years()))
        with patch("energy_ledger.record", side_effect=ValueError("Invalid counter")):
            with self.assertLogs(level="ERROR"):
                self.store.save(PLUG.mac, int((self.start + timedelta(minutes=10)).timestamp()),
                                {"today_kwh": .10, "month_kwh": .10})
        self.assertTrue(self.store.error)
        self.assertEqual(Decimal(self.years()[0]["kwh"]), Decimal(".05"))
        self.save(self.start + timedelta(minutes=10), .10, .10)
        self.assertEqual(self.years()[0]["cost"], "0.03")
        self.assertIn("waiting", energy_ledger.summary([]))


if __name__ == "__main__":
    unittest.main()
