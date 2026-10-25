"""Run with python3 -m unittest -v."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from controller import (
    MANUAL_SECONDS,
    TEST_SECONDS,
    Controller,
    Zone,
    copy_day_times,
    default_zones,
    load_zones,
    parse_time,
    save_zones,
    validate_zones,
)

MONDAY = datetime(2026, 10, 26, 6, 0)


class FakeRelays:
    def __init__(self) -> None:
        self.states = [False] * 8

    def set_relay(self, channel: int, energized: bool) -> None:
        self.states[channel] = energized


def schedule_for(
    days: tuple[int, ...] = (),
    first: tuple[str, str] = ("06:00", "06:20"),
    second: tuple[str, str] = ("", ""),
) -> tuple[tuple[str, str, str, str], ...]:
    schedule = [("", "", "", "") for _ in range(7)]
    for day in days:
        schedule[day] = (*first, *second)
    return tuple(schedule)


class ValidationTests(unittest.TestCase):
    def test_copy_selected_day_times_to_all_days(self) -> None:
        schedule = list(schedule_for((0, 1, 2, 3, 4, 5, 6)))
        schedule[2] = ("06:15", "06:30", "18:00", "18:20")
        copied = copy_day_times(tuple(schedule), 2)
        self.assertEqual(copied, (schedule[2],) * 7)
        self.assertEqual(schedule[0], ("06:00", "06:20", "", ""))

    def test_copy_allows_blank_windows(self) -> None:
        schedule = list(schedule_for((0, 1, 2, 3, 4, 5, 6)))
        schedule[6] = ("", "", "", "")
        self.assertEqual(copy_day_times(tuple(schedule), 6), (("", "", "", ""),) * 7)

    def test_copy_rejects_invalid_source_day_or_schedule(self) -> None:
        for schedule, source_day in ((schedule_for(), 7), (schedule_for(), True),
                                     (schedule_for()[:6], 0)):
            with self.subTest(source_day=source_day), self.assertRaises(ValueError):
                copy_day_times(schedule, source_day)

    def test_time_format(self) -> None:
        self.assertEqual(parse_time("00:00"), 0)
        self.assertEqual(parse_time("23:59"), 1439)
        for value in ("6:00", "24:00", "12:60", "06:00 AM", " 06:00", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time(value)

    def test_stop_must_follow_start(self) -> None:
        for stop in ("06:00", "05:59"):
            with self.subTest(stop=stop), self.assertRaises(ValueError):
                Zone("Lawn", schedule=schedule_for((0,), ("06:00", stop))).validate()

    def test_second_window_must_follow_first_without_overlapping(self) -> None:
        for second in (("06:19", "06:30"), ("06:00", "06:30")):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, "window 2"):
                Zone(
                    "Lawn",
                    schedule=schedule_for((0,), ("06:00", "06:20"), second),
                ).validate()

    def test_second_window_can_be_the_only_window_for_a_day(self) -> None:
        schedule = list(schedule_for((0,)))
        schedule[0] = ("", "", "17:00", "17:15")
        Zone("Lawn", True, tuple(schedule)).validate()

    def test_partial_time_pair_is_invalid(self) -> None:
        for times in (("06:00", "", "", ""), ("", "06:20", "", ""),
                      ("", "", "17:00", "")):
            schedule = list(schedule_for((0,)))
            schedule[0] = times
            with self.subTest(times=times), self.assertRaisesRegex(ValueError, "both"):
                Zone("Lawn", schedule=tuple(schedule)).validate()

    def test_enabled_requires_day(self) -> None:
        with self.assertRaises(ValueError):
            Zone("Lawn", enabled=True).validate()

    def test_disabled_can_have_no_days(self) -> None:
        Zone("Lawn").validate()

    def test_rejects_invalid_metadata(self) -> None:
        for zone in (
            Zone(" "), Zone("x" * 41), Zone("Lawn", schedule=(("", "", "", ""),)),
            Zone("Lawn", enabled=1), Zone("Lawn", schedule=(("06:00", 600, "", ""),) * 7),
        ):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                zone.validate()

    def test_eight_zones_required(self) -> None:
        with self.assertRaises(ValueError):
            validate_zones(default_zones()[:7])


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "settings.json"

    def test_missing_file_defaults_to_eight_disabled_zones(self) -> None:
        zones = load_zones(self.path)
        self.assertEqual(len(zones), 8)
        self.assertFalse(any(zone.enabled for zone in zones))

    def test_round_trip(self) -> None:
        zones = default_zones()
        schedule = list(schedule_for((0, 2, 4), ("05:30", "05:50")))
        schedule[5] = ("06:00", "06:10", "18:00", "18:20")
        zones[0] = Zone("Front Lawn", True, tuple(schedule))
        save_zones(self.path, zones)
        self.assertEqual(load_zones(self.path), zones)
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)

    def test_invalid_save_preserves_file(self) -> None:
        zones = default_zones()
        save_zones(self.path, zones)
        original = self.path.read_bytes()
        zones[0] = Zone("Invalid", schedule=schedule_for((0,), ("06:00", "01:00")))
        with self.assertRaises(ValueError):
            save_zones(self.path, zones)
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_replace_preserves_file_and_cleans_temporary(self) -> None:
        save_zones(self.path, default_zones())
        original = self.path.read_bytes()
        with patch("controller.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                save_zones(self.path, default_zones())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_settings_are_not_silently_reset(self) -> None:
        self.path.parent.mkdir()
        for content in (
            "not json", "[]", '{"version": 3, "zones": []}',
            '{"version": true, "zones": []}', '{"version": 2, "zones": []}',
            '{"version": 1, "zones": [null]}',
        ):
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                load_zones(self.path)

    def test_json_field_types_checked(self) -> None:
        save_zones(self.path, default_zones())
        original = json.loads(self.path.read_text())
        for field, value in (("enabled", "false"), ("schedule", "Mon"),
                             ("name", None), ("schedule", [[False, "", "", ""]] * 7)):
            data = json.loads(json.dumps(original))
            data["zones"][0][field] = value
            self.path.write_text(json.dumps(data))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                load_zones(self.path)

    def test_version_one_schedule_is_migrated_without_losing_times(self) -> None:
        legacy = {
            "version": 1,
            "zones": [
                {"name": zone.name, "enabled": False, "days": [],
                 "start": "06:00", "stop": "06:20"}
                for zone in default_zones()
            ],
        }
        legacy["zones"][0] = {
            "name": "Lawn", "enabled": True, "days": [0, 2, 4],
            "start": "05:30", "stop": "05:50",
        }
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(legacy))
        migrated = load_zones(self.path)
        self.assertEqual(
            migrated[0].schedule,
            schedule_for((0, 2, 4), ("05:30", "05:50")),
        )


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.elapsed = 100.0
        self.zones = default_zones()
        self.zones[0] = Zone("Lawn", True, schedule_for((0,)))
        self.backend = FakeRelays()
        self.controller = Controller(self.zones, self.backend, lambda: self.elapsed)

    def test_schedule_boundaries(self) -> None:
        self.controller.tick(MONDAY - timedelta(seconds=1))
        self.assertFalse(self.backend.states[0])
        self.controller.tick(MONDAY)
        self.assertTrue(self.backend.states[0])
        self.controller.tick(MONDAY + timedelta(minutes=19, seconds=59))
        self.assertTrue(self.backend.states[0])
        self.controller.tick(MONDAY + timedelta(minutes=20))
        self.assertFalse(self.backend.states[0])

    def test_wrong_weekday_does_not_run(self) -> None:
        self.controller.tick(MONDAY + timedelta(days=1))
        self.assertFalse(any(self.backend.states))

    def test_starting_inside_window_runs_remaining_time(self) -> None:
        self.controller.tick(MONDAY + timedelta(minutes=12))
        self.assertEqual(self.controller.runs[0].deadline, self.elapsed + 8 * 60)

    def test_both_daily_windows_run_at_their_own_times(self) -> None:
        self.zones[0] = Zone(
            "Lawn", True, schedule_for((0,), ("06:00", "06:20"), ("17:00", "17:15"))
        )
        self.controller.apply(self.zones, MONDAY)
        self.controller.tick(MONDAY)
        self.assertEqual(self.controller.runs[0].window_key, (MONDAY.date(), "06:00", "06:20"))
        self.elapsed += 20 * 60
        self.controller.tick(MONDAY + timedelta(minutes=20))
        self.assertFalse(self.backend.states[0])
        self.controller.tick(MONDAY.replace(hour=17))
        self.assertTrue(self.backend.states[0])
        self.assertEqual(self.controller.runs[0].window_key, (MONDAY.date(), "17:00", "17:15"))

    def test_stopping_first_window_does_not_suppress_second(self) -> None:
        self.zones[0] = Zone(
            "Lawn", True, schedule_for((0,), ("06:00", "06:20"), ("17:00", "17:15"))
        )
        self.controller.apply(self.zones, MONDAY)
        self.controller.tick(MONDAY)
        self.controller.stop(0, MONDAY + timedelta(minutes=5))
        self.controller.tick(MONDAY + timedelta(minutes=6))
        self.assertFalse(self.backend.states[0])
        self.controller.tick(MONDAY.replace(hour=17))
        self.assertTrue(self.backend.states[0])

    def test_manual_override_does_not_resume_or_suppress_later_window(self) -> None:
        self.zones[0] = Zone(
            "Lawn", True, schedule_for((0,), ("06:00", "06:20"), ("17:00", "17:15"))
        )
        self.controller.apply(self.zones, MONDAY)
        self.controller.tick(MONDAY)
        self.controller.start_manual(0, MONDAY, test=True)
        self.elapsed += TEST_SECONDS
        self.controller.tick(MONDAY + timedelta(minutes=1))
        self.assertFalse(self.backend.states[0])
        self.controller.tick(MONDAY.replace(hour=17))
        self.assertTrue(self.backend.states[0])

    def test_second_window_can_be_the_only_window_on_a_weekday(self) -> None:
        schedule = list(schedule_for())
        schedule[0] = ("", "", "17:00", "17:15")
        self.zones[0] = Zone("Lawn", True, tuple(schedule))
        self.controller.apply(self.zones, MONDAY)
        self.controller.tick(MONDAY.replace(hour=17))
        self.assertTrue(self.backend.states[0])

    def test_manual_deadline_uses_monotonic_time(self) -> None:
        self.controller.start_manual(1, MONDAY)
        self.elapsed += MANUAL_SECONDS - 0.1
        self.controller.tick(MONDAY - timedelta(hours=2))
        self.assertTrue(self.backend.states[1])
        self.elapsed += 0.1
        self.controller.tick(MONDAY - timedelta(hours=2))
        self.assertFalse(self.backend.states[1])

    def test_test_duration_is_exactly_sixty_seconds(self) -> None:
        self.controller.start_manual(1, MONDAY, test=True)
        self.assertEqual(self.controller.runs[1].source, "Test")
        self.elapsed += TEST_SECONDS - 0.001
        self.controller.tick(MONDAY)
        self.assertTrue(self.backend.states[1])
        self.elapsed += 0.001
        self.controller.tick(MONDAY)
        self.assertFalse(self.backend.states[1])

    def test_manual_during_routine_does_not_resume_when_finished(self) -> None:
        self.controller.tick(MONDAY)
        self.controller.start_manual(0, MONDAY, test=True)
        self.elapsed += TEST_SECONDS
        self.controller.tick(MONDAY + timedelta(minutes=1))
        self.controller.tick(MONDAY + timedelta(minutes=2))
        self.assertFalse(self.backend.states[0])

    def test_stop_suppresses_window_but_not_next_week(self) -> None:
        self.controller.tick(MONDAY)
        self.controller.stop(0, MONDAY)
        self.controller.tick(MONDAY + timedelta(minutes=1))
        self.assertFalse(self.backend.states[0])
        self.controller.tick(MONDAY + timedelta(days=7))
        self.assertTrue(self.backend.states[0])

    def test_routine_does_not_extend_if_wall_clock_moves_back(self) -> None:
        self.controller.tick(MONDAY)
        self.elapsed += 20 * 60
        self.controller.tick(MONDAY + timedelta(minutes=5))
        self.controller.tick(MONDAY + timedelta(minutes=6))
        self.assertFalse(self.backend.states[0])

    def test_save_disabling_routine_stops_it(self) -> None:
        self.controller.tick(MONDAY)
        self.zones[0] = replace(self.zones[0], enabled=False)
        self.controller.apply(self.zones, MONDAY)
        self.assertFalse(self.backend.states[0])

    def test_rename_does_not_reset_deadline(self) -> None:
        self.controller.tick(MONDAY)
        deadline = self.controller.runs[0].deadline
        self.elapsed += 10
        self.zones[0] = replace(self.zones[0], name="New name")
        self.controller.apply(self.zones, MONDAY)
        self.assertEqual(self.controller.runs[0].deadline, deadline)

    def test_disabling_routine_does_not_cancel_manual(self) -> None:
        self.controller.start_manual(0, MONDAY)
        self.zones[0] = replace(self.zones[0], enabled=False)
        self.controller.apply(self.zones, MONDAY)
        self.assertTrue(self.backend.states[0])

    def test_multiple_zones_can_water(self) -> None:
        self.controller.start_manual(2, MONDAY)
        self.controller.tick(MONDAY)
        self.assertTrue(self.backend.states[0])
        self.assertTrue(self.backend.states[2])

    def test_stop_all_suppresses_schedule(self) -> None:
        self.controller.start_manual(1, MONDAY)
        self.controller.tick(MONDAY)
        self.controller.stop_all(MONDAY)
        self.controller.tick(MONDAY + timedelta(seconds=1))
        self.assertFalse(any(self.backend.states))

    def test_shutdown_turns_all_off_and_prevents_restart(self) -> None:
        self.controller.start_manual(1, MONDAY)
        self.controller.close(MONDAY)
        self.controller.tick(MONDAY)
        self.assertFalse(any(self.backend.states))
        with self.assertRaises(RuntimeError):
            self.controller.start_manual(1, MONDAY)

    def test_failed_on_does_not_record_a_running_zone(self) -> None:
        with patch.object(self.backend, "set_relay", side_effect=OSError("relay failed")):
            with self.assertRaises(OSError):
                self.controller.start_manual(1, MONDAY)
        self.assertNotIn(1, self.controller.runs)

    def test_shutdown_attempts_every_channel_after_failure(self) -> None:
        with patch.object(self.backend, "set_relay", side_effect=OSError("relay failed")) as setter:
            with self.assertRaises(ExceptionGroup) as caught:
                self.controller.close(MONDAY)
        self.assertEqual(setter.call_count, 8)
        self.assertEqual(len(caught.exception.exceptions), 8)

    def test_startup_attempts_off_on_every_channel_after_failure(self) -> None:
        with patch.object(self.backend, "set_relay", side_effect=OSError("relay failed")) as setter:
            with self.assertRaises(ExceptionGroup):
                Controller(self.zones, self.backend)
        self.assertEqual(setter.call_count, 8)


if __name__ == "__main__":
    unittest.main()
