"""Read-only overview ordering, filtering, and warning regression tests."""

import unittest

from controller import Zone, default_zones
from schedule_overview import overview_rows
from test_controller import schedule_for


class OverviewTests(unittest.TestCase):
    def test_sorted_mixed_formats_four_periods_and_master_exclusion(self) -> None:
        zones = default_zones()
        zones[0] = Zone("Lawn", True, schedule_for(
            (0,), ("0600", "0620"), ("1000", "1020"), ("1400", "1420"), ("1620", "1640"),
        ))
        zones[1] = Zone("Veggies", True, schedule_for((0,), ("05:00", "05:20")))
        zones[7] = Zone("Old schedule", True, schedule_for((0,)))
        rows = overview_rows(zones, 0, 7, False)
        self.assertEqual([row.channel for row in rows], [1, 0, 0, 0, 0])
        self.assertEqual([row.period for row in rows], [1, 1, 2, 3, 4])
        self.assertEqual(overview_rows(zones, 1, 7, False), [])
        self.assertEqual(len(overview_rows(zones, 0, None, False)), 6)

    def test_duplicate_starts_and_overlaps_mark_both_sides(self) -> None:
        zones = default_zones()
        for channel, pair in enumerate((
            ("0600", "0620"), ("06:00", "06:10"), ("0615", "0630"), ("0630", "0640"),
        )):
            zones[channel] = Zone(str(channel), True, schedule_for((0,), pair))
        rows = overview_rows(zones, 0, 7, False)
        self.assertEqual([row.warning for row in rows], [
            "Same start / Overlap", "Same start / Overlap", "Overlap", "",
        ])

    def test_disabled_rows_visible_but_not_counted_as_conflicts(self) -> None:
        zones = default_zones()
        zones[0] = Zone("Active", True, schedule_for((0,)))
        zones[1] = Zone("Disabled", False, schedule_for((0,)))
        rows = overview_rows(zones, 0, 7, False)
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(row.warning for row in rows))
        self.assertEqual(len(overview_rows(zones, 0, 7, True)), 1)

    def test_invalid_edits_remain_visible_and_do_not_hide_other_rows(self) -> None:
        zones = default_zones()
        zones[0] = Zone("Incomplete", True, schedule_for((0,), ("0600", "")))
        zones[1] = Zone("Bad", True, schedule_for((0,), ("bad", "0700")))
        zones[2] = Zone("Reversed", True, schedule_for((0,), ("0800", "0700")))
        zones[3] = Zone("Valid", True, schedule_for((0,), ("0610", "0620")))
        rows = overview_rows(zones, 0, 7, False)
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row.warning == "Invalid/incomplete time pair" for row in rows), 3)
        self.assertEqual(rows[-1].channel, 1)
        self.assertEqual(next(row.warning for row in rows if row.channel == 3), "")

    def test_invalid_weekday_rejected(self) -> None:
        for day in (-1, 7, True):
            with self.subTest(day=day), self.assertRaises(ValueError):
                overview_rows(default_zones(), day, 7, False)

    def test_paused_period_is_visible_but_not_counted_as_a_conflict(self) -> None:
        zones = default_zones()
        zones[0] = Zone(
            "Paused slot", True,
            schedule_for((0,), ("0600", "0620"), ("1000", "1020")),
            (False, True, False, False),
        )
        zones[1] = Zone("Other", True, schedule_for((0,), ("1005", "1015")))
        rows = overview_rows(zones, 0, 7, False)
        paused = next(row for row in rows if row.channel == 0 and row.period == 2)
        other = next(row for row in rows if row.channel == 1)
        self.assertTrue(paused.paused)
        self.assertFalse(paused.warning)
        self.assertFalse(other.warning)
