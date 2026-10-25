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
    load_rain_pause,
    rain_pause_path,
    parse_time,
    save_zones,
    save_rain_pause,
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
    third: tuple[str, str] = ("", ""),
    fourth: tuple[str, str] = ("", ""),
) -> tuple[tuple[str, ...], ...]:
    schedule = [("",) * 8 for _ in range(7)]
    for day in days:
        schedule[day] = (*first, *second, *third, *fourth)
    return tuple(schedule)


class ValidationTests(unittest.TestCase):
    def test_copy_selected_day_times_to_all_days(self) -> None:
        schedule = list(schedule_for((0, 1, 2, 3, 4, 5, 6)))
        schedule[2] = ("0615", "0630", "1000", "1020", "1400", "1420", "1800", "1820")
        copied = copy_day_times(tuple(schedule), 2)
        self.assertEqual(copied, (schedule[2],) * 7)
        self.assertEqual(schedule[0], ("06:00", "06:20") + ("",) * 6)

    def test_copy_allows_blank_windows(self) -> None:
        schedule = list(schedule_for((0, 1, 2, 3, 4, 5, 6)))
        schedule[6] = ("",) * 8
        self.assertEqual(copy_day_times(tuple(schedule), 6), (("",) * 8,) * 7)

    def test_copy_rejects_invalid_source_day_or_schedule(self) -> None:
        for schedule, source_day in ((schedule_for(), 7), (schedule_for(), True),
                                     (schedule_for()[:6], 0)):
            with self.subTest(source_day=source_day), self.assertRaises(ValueError):
                copy_day_times(schedule, source_day)

    def test_time_format(self) -> None:
        self.assertEqual(parse_time("00:00"), 0)
        self.assertEqual(parse_time("23:59"), 1439)
        self.assertEqual(parse_time("1620"), 16 * 60 + 20)
        self.assertEqual(parse_time("0600"), 6 * 60)
        self.assertEqual(parse_time("0000"), 0)
        self.assertEqual(parse_time("2359"), 1439)
        for value in ("6:00", "24:00", "12:60", "06:00 AM", " 06:00", "",
                      "2400", "1260", "620", "16200", "16:2"):
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
        schedule[0] = ("", "", "17:00", "17:15") + ("",) * 4
        Zone("Lawn", True, tuple(schedule)).validate()

    def test_partial_time_pair_is_invalid(self) -> None:
        for times in (("06:00", "", "", ""), ("", "06:20", "", ""),
                      ("", "", "17:00", "")):
            schedule = list(schedule_for((0,)))
            schedule[0] = times + ("",) * 4
            with self.subTest(times=times), self.assertRaisesRegex(ValueError, "both"):
                Zone("Lawn", schedule=tuple(schedule)).validate()

    def test_overlap_or_invalid_times_in_later_windows_are_rejected(self) -> None:
        for third, fourth in (
            (("0959", "1020"), ("1800", "1820")),
            (("1400", "1420"), ("1419", "1440")),
            (("1400", "1420"), ("1800", "")),
            (("1400", "1420"), ("2400", "2410")),
        ):
            with self.subTest(third=third, fourth=fourth), self.assertRaises(ValueError):
                Zone(
                    "Lawn", True,
                    schedule_for((0,), ("0600", "0620"), ("1000", "1020"), third, fourth),
                ).validate()

    def test_only_fourth_window_and_adjacent_windows_are_valid(self) -> None:
        Zone(
            "Lawn", True,
            schedule_for((0,), ("", ""), ("", ""), ("", ""), ("1620", "1640")),
        ).validate()
        Zone(
            "Lawn", True,
            schedule_for((0,), ("0600", "0620"), ("0620", "0640"),
                         ("0640", "0700"), ("0700", "0720")),
        ).validate()

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
    def test_rain_pause_persists_independently_of_schedules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertFalse(load_rain_pause(path))
            save_zones(path, default_zones())
            original = path.read_bytes()
            save_rain_pause(path, True)
            self.assertTrue(load_rain_pause(path))
            self.assertEqual(path.read_bytes(), original)
            save_zones(path, default_zones())
            self.assertTrue(load_rain_pause(path))
            save_rain_pause(path, False)
            self.assertFalse(load_rain_pause(path))

    def test_invalid_rain_pause_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for data in ('null', '{}', '{"rain_paused": "false"}',
                         '{"rain_paused": 0}', '{"rain_paused": true, "extra": 1}', '{'):
                rain_pause_path(path).write_text(data)
                with self.subTest(data=data), self.assertRaises(ValueError):
                    load_rain_pause(path)

    def test_rain_pause_atomic_write_failure_preserves_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_rain_pause(path, True)
            with patch("controller.os.replace", side_effect=OSError("Disk error")):
                with self.assertRaises(OSError):
                    save_rain_pause(path, False)
            self.assertTrue(load_rain_pause(path))
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

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
        schedule[5] = ("0600", "0610", "1000", "1010", "1400", "1410", "1800", "1820")
        zones[0] = Zone("Front Lawn", True, tuple(schedule))
        save_zones(self.path, zones)
        self.assertEqual(load_zones(self.path), zones)
        self.assertEqual(json.loads(self.path.read_text())["version"], 3)

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
            "not json", "[]", '{"version": 4, "zones": []}',
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

    def test_version_two_migration_preserves_both_windows_and_file(self) -> None:
        row = ["06:00", "06:20", "16:20", "16:40"]
        original = json.dumps({
            "version": 2,
            "zones": [
                {"name": zone.name, "enabled": True, "schedule": [row] * 7}
                for zone in default_zones()
            ],
        })
        self.path.parent.mkdir()
        self.path.write_text(original)
        zones = load_zones(self.path)
        for zone in zones:
            self.assertEqual(zone.schedule, (tuple(row) + ("",) * 4,) * 7)
        self.assertEqual(self.path.read_text(), original)
        save_zones(self.path, zones)
        self.assertEqual(json.loads(self.path.read_text())["version"], 3)
        self.assertEqual(load_zones(self.path), zones)


class ControllerTests(unittest.TestCase):
    def test_rain_pause_stops_all_sources_and_blocks_future_watering(self) -> None:
        self.zones[0] = Zone(
            "Lawn", True, schedule_for((0,), ("0600", "0620"), ("1700", "1720"))
        )
        self.controller.apply(self.zones, MONDAY)
        self.controller.start_manual(1, MONDAY)
        self.controller.start_manual(2, MONDAY, test=True)
        self.controller.set_rain_pause(True, MONDAY)
        self.assertFalse(any(self.backend.states))
        self.assertFalse(self.controller.runs)
        for test in (False, True):
            with self.assertRaisesRegex(RuntimeError, "Rain Pause"):
                self.controller.start_manual(0, MONDAY, test=test)
        with self.assertRaisesRegex(RuntimeError, "Rain Pause"):
            self.controller.toggle_test(0, MONDAY)
        self.controller.apply(self.zones, MONDAY)
        for now in (MONDAY, MONDAY.replace(hour=17), MONDAY + timedelta(days=7)):
            self.controller.tick(now)
            self.assertFalse(any(self.backend.states))
        self.controller.set_rain_pause(False, MONDAY)
        self.assertTrue(self.backend.states[0])
        self.assertEqual(self.controller.runs[0].source, "Routine")
        self.assertNotIn(1, self.controller.runs)
        self.assertNotIn(2, self.controller.runs)

    def test_rain_pause_survives_restart_during_active_schedule(self) -> None:
        restarted = Controller(self.zones, self.backend, rain_paused=True)
        restarted.tick(MONDAY)
        self.assertFalse(any(self.backend.states))
        restarted.set_rain_pause(False, MONDAY)
        self.assertTrue(self.backend.states[0])

    def test_rain_pause_keeps_preexisting_stop_suppression(self) -> None:
        self.controller.tick(MONDAY)
        self.controller.stop(0, MONDAY)
        self.controller.set_rain_pause(True, MONDAY)
        self.controller.set_rain_pause(False, MONDAY)
        self.assertFalse(self.backend.states[0])

    def test_rain_pause_off_failure_still_blocks_on_commands(self) -> None:
        self.controller.tick(MONDAY)
        with patch.object(self.backend, "set_relay", side_effect=OSError("Disconnected")) as write:
            with self.assertRaises(ExceptionGroup):
                self.controller.set_rain_pause(True, MONDAY)
            self.assertEqual(write.call_count, 8)
            self.controller.tick(MONDAY)
            with self.assertRaisesRegex(RuntimeError, "Rain Pause"):
                self.controller.start_manual(0, MONDAY)
            self.assertEqual(write.call_count, 8)
        self.assertTrue(self.controller.rain_paused)

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

    def test_all_four_compact_time_windows_run_each_weekday(self) -> None:
        schedule = schedule_for(
            tuple(range(7)), ("0600", "0620"), ("1000", "1020"),
            ("1400", "1420"), ("1620", "1640"),
        )
        self.zones[0] = Zone("Lawn", True, schedule)
        self.controller.apply(self.zones, MONDAY - timedelta(minutes=1))
        for day in range(7):
            for hour, minute in ((6, 0), (10, 0), (14, 0), (16, 20)):
                start = (MONDAY + timedelta(days=day)).replace(hour=hour, minute=minute)
                with self.subTest(day=day, hour=hour):
                    self.controller.tick(start)
                    self.assertTrue(self.backend.states[0])
                    self.assertEqual(self.controller.runs[0].deadline, self.elapsed + 1200)
                    self.elapsed += 1200
                    self.controller.tick(start + timedelta(minutes=20))
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
        schedule[0] = ("", "", "17:00", "17:15") + ("",) * 4
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

    def test_test_duration_is_exactly_ten_minutes(self) -> None:
        self.assertEqual(TEST_SECONDS, 600)
        self.controller.toggle_test(1, MONDAY)
        self.assertEqual(self.controller.runs[1].source, "Test")
        self.elapsed += TEST_SECONDS - 0.001
        self.controller.tick(MONDAY)
        self.assertTrue(self.backend.states[1])
        self.elapsed += 0.001
        self.controller.tick(MONDAY)
        self.assertFalse(self.backend.states[1])
        self.assertNotIn(1, self.controller.runs)

    def test_test_toggle_stops_immediately_and_can_restart(self) -> None:
        self.controller.toggle_test(1, MONDAY)
        self.assertTrue(self.backend.states[1])
        self.elapsed += 30
        self.controller.toggle_test(1, MONDAY + timedelta(seconds=30))
        self.assertFalse(self.backend.states[1])
        self.assertNotIn(1, self.controller.runs)
        self.controller.toggle_test(1, MONDAY + timedelta(seconds=31))
        self.assertTrue(self.backend.states[1])
        self.assertEqual(self.controller.runs[1].deadline, self.elapsed + 600)

    def test_test_toggle_does_not_replace_manual_or_scheduled_runs(self) -> None:
        self.controller.start_manual(1, MONDAY)
        self.controller.tick(MONDAY)
        for channel in (0, 1):
            run = self.controller.runs[channel]
            with self.subTest(channel=channel), self.assertRaises(RuntimeError):
                self.controller.toggle_test(channel, MONDAY)
            self.assertEqual(self.controller.runs[channel], run)
            self.assertTrue(self.backend.states[channel])

    def test_test_toggle_stops_only_selected_zone(self) -> None:
        self.controller.toggle_test(1, MONDAY)
        self.controller.toggle_test(2, MONDAY)
        self.controller.toggle_test(1, MONDAY)
        self.assertFalse(self.backend.states[1])
        self.assertTrue(self.backend.states[2])

    def test_test_toggle_rejects_closed_controller(self) -> None:
        self.controller.close(MONDAY)
        with self.assertRaises(RuntimeError):
            self.controller.toggle_test(1, MONDAY)

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
            with self.assertRaises(ExceptionGroup):
                self.controller.start_manual(1, MONDAY)
        self.assertNotIn(1, self.controller.runs)
        self.assertTrue(self.controller.closed)

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


class MasterPumpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zones = default_zones()
        self.backend = FakeRelays()
        self.elapsed = 0.0
        writer = patch.object(
            self.backend, "set_relay", wraps=self.backend.set_relay
        )
        self.write = self.enterContext(writer)
        self.controller = Controller(self.zones, self.backend, lambda: self.elapsed)
        self.assertEqual(self.write.call_args_list[0].args, (7, False))
        self.write.reset_mock()

    def calls(self) -> list[tuple[int, bool]]:
        return [item.args for item in self.write.call_args_list]

    def test_first_valve_opens_before_pump_and_pump_stops_before_last_valve(self) -> None:
        self.controller.start_manual(0, MONDAY)
        self.assertEqual(self.calls(), [(0, True), (7, True)])
        self.assertTrue(self.controller.pump_on)
        self.write.reset_mock()
        self.controller.stop(0, MONDAY)
        self.assertEqual(self.calls(), [(7, False), (0, False)])
        self.assertFalse(self.controller.pump_on)

    def test_pump_remains_on_until_last_of_multiple_zones_stops(self) -> None:
        self.controller.start_manual(0, MONDAY)
        self.controller.toggle_test(6, MONDAY)
        self.write.reset_mock()
        self.controller.stop(0, MONDAY)
        self.assertEqual(self.calls(), [(0, False)])
        self.assertTrue(self.backend.states[7])
        self.controller.toggle_test(6, MONDAY)
        self.assertEqual(self.calls()[-2:], [(7, False), (6, False)])
        self.assertFalse(any(self.backend.states))

    def test_legacy_channel_eight_schedule_is_preserved_but_ignored(self) -> None:
        self.zones[7] = Zone("Old garden", True, schedule_for((0,)))
        self.controller.apply(self.zones, MONDAY)
        self.assertFalse(any(self.backend.states))
        self.assertEqual(self.controller.zones[7], self.zones[7])
        for test in (False, True):
            with self.assertRaisesRegex(RuntimeError, "master pump"):
                self.controller.start_manual(7, MONDAY, test=test)
        with self.assertRaisesRegex(RuntimeError, "master pump"):
            self.controller.toggle_test(7, MONDAY)
        with self.assertRaisesRegex(RuntimeError, "master pump"):
            self.controller.stop(7, MONDAY)
        self.write.assert_not_called()

    def test_routine_deadline_and_save_disabling_stop_the_pump(self) -> None:
        self.zones[0] = Zone("Lawn", True, schedule_for((0,)))
        self.controller.apply(self.zones, MONDAY)
        self.assertEqual(self.calls(), [(0, True), (7, True)])
        self.write.reset_mock()
        self.elapsed = 1200
        self.controller.tick(MONDAY + timedelta(minutes=20))
        self.assertEqual(self.calls(), [(7, False), (0, False)])
        self.controller.tick(MONDAY + timedelta(days=7))
        self.zones[0] = replace(self.zones[0], enabled=False)
        self.controller.apply(self.zones, MONDAY + timedelta(days=7))
        self.assertFalse(any(self.backend.states))

    def test_manual_and_test_deadlines_stop_pump(self) -> None:
        for test in (False, True):
            with self.subTest(test=test):
                self.controller.start_manual(0, MONDAY, test=test)
                self.elapsed += 600
                self.write.reset_mock()
                self.controller.tick(MONDAY)
                self.assertEqual(self.calls(), [(7, False), (0, False)])
                self.assertFalse(self.controller.pump_on)

    def test_stop_all_rain_pause_and_close_send_pump_off_first(self) -> None:
        for action in (
            self.controller.stop_all,
            lambda now: self.controller.set_rain_pause(True, now),
            self.controller.close,
        ):
            self.controller.start_manual(0, MONDAY)
            self.write.reset_mock()
            action(MONDAY)
            self.assertEqual(self.calls()[0], (7, False))
            self.assertEqual(len(self.calls()), 8)
            self.assertFalse(self.controller.pump_on)
            self.assertFalse(any(self.backend.states))
            if self.controller.rain_paused:
                self.controller.set_rain_pause(False, MONDAY)

    def test_failed_valve_on_never_starts_pump_and_attempts_shutdown(self) -> None:
        def fail_valve(channel: int, energized: bool) -> None:
            if energized and channel == 0:
                raise OSError("Valve failed")
            self.backend.states[channel] = energized

        self.write.side_effect = fail_valve
        with self.assertRaisesRegex(OSError, "Valve failed"):
            self.controller.start_manual(0, MONDAY)
        self.assertNotIn((7, True), self.calls())
        self.assertEqual(self.calls()[1], (7, False))
        self.assertFalse(any(self.backend.states))
        self.assertTrue(self.controller.closed)

    def test_failed_pump_on_closes_valve_and_prevents_more_watering(self) -> None:
        def fail_pump(channel: int, energized: bool) -> None:
            self.backend.states[channel] = energized
            if energized and channel == 7:
                raise OSError("Pump readback failed")

        self.write.side_effect = fail_pump
        with self.assertRaisesRegex(OSError, "Pump readback"):
            self.controller.start_manual(0, MONDAY)
        self.assertEqual(self.calls()[:3], [(0, True), (7, True), (7, False)])
        self.assertFalse(any(self.backend.states))
        self.assertFalse(self.controller.runs)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.controller.start_manual(1, MONDAY)

    def test_failed_pump_off_does_not_close_last_valve_before_fault_handling(self) -> None:
        self.controller.start_manual(0, MONDAY)
        self.write.reset_mock()
        self.write.side_effect = OSError("Pump OFF failed")
        with self.assertRaises(OSError):
            self.controller.stop(0, MONDAY)
        self.assertEqual(self.calls(), [(7, False)])
        self.assertTrue(self.backend.states[0])
        self.write.reset_mock()
        with self.assertRaises(ExceptionGroup):
            self.controller.stop_all(MONDAY)
        self.assertEqual(len(self.calls()), 8)
        self.assertEqual(self.calls()[0], (7, False))

    def test_none_setting_restores_original_eight_zone_logic(self) -> None:
        with patch("controller.MASTER_PUMP_CHANNEL", None):
            controller = Controller(self.zones, self.backend)
        self.write.reset_mock()
        controller.start_manual(7, MONDAY)
        self.assertEqual(self.calls(), [(7, True)])
        self.assertFalse(controller.pump_on)
        controller.stop(7, MONDAY)
        self.zones[7] = Zone("Restored garden", True, schedule_for((0,)))
        controller.apply(self.zones, MONDAY)
        self.assertIn(7, controller.runs)


if __name__ == "__main__":
    unittest.main()
