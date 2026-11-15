"""Saved appliance 24/7 routines, using fake hardware only."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from controller import Controller, decode_zones, default_zones, encode_zones, load_zones, next_scheduled_start, save_zones
from schedule_overview import overview_rows
from service import Service
from test_service import FakeDevice
from timetable_profiles import TimetableProfiles


class ApplianceRelays:
    def __init__(self):
        self.calls = []
        self.states = [False] * 16

    def set_relay(self, channel, state):
        self.calls.append((channel, state))
        self.states[channel] = state


class ContinuousScheduleTests(unittest.TestCase):
    def setUp(self):
        self.zones = default_zones(16)
        self.zones[15] = replace(self.zones[15], enabled=True, continuous_schedule=True)
        self.relays = ApplianceRelays()
        self.controller = Controller(self.zones, self.relays)
        self.now = datetime(2026, 11, 14, 23, 58)

    def test_stays_on_across_midnight_and_week_boundary_without_any_off_command(self):
        self.controller.tick(self.now)
        run = self.controller.runs[15]
        self.assertIsNone(run.deadline)
        self.relays.calls.clear()
        for now in (self.now.replace(minute=59), self.now + timedelta(minutes=2),
                    self.now + timedelta(days=1), self.now + timedelta(days=8)):
            self.controller.tick(now)
            self.assertIs(self.controller.runs[15], run)
            self.assertTrue(self.relays.states[15])
        self.assertEqual(self.relays.calls, [])
        self.assertIsNone(next_scheduled_start(self.zones[15], self.now))

    def test_stop_disables_saved_routine_and_apply_reenable_resumes(self):
        self.controller.tick(self.now)
        self.controller.stop(15, self.now)
        self.assertFalse(self.controller.zones[15].enabled)
        self.controller.tick(self.now + timedelta(days=1))
        self.assertFalse(self.relays.states[15])
        self.controller.apply(self.zones, self.now + timedelta(days=1))
        self.assertTrue(self.relays.states[15])

    def test_group_stops_pause_and_restore_hold_are_respected(self):
        self.controller.tick(self.now)
        self.controller.stop_group(0, self.now)
        self.assertTrue(self.relays.states[15])
        self.controller.set_rain_pause(True, self.now)
        self.assertFalse(self.relays.states[15])
        self.controller.set_rain_pause(False, self.now)
        self.assertTrue(self.relays.states[15])
        paused = list(self.zones)
        paused[15] = replace(paused[15], paused_windows=(True, False, False, False))
        self.controller.apply(paused, self.now)
        self.assertFalse(self.relays.states[15])
        self.controller.apply(self.zones, self.now)
        self.assertTrue(self.relays.states[15])
        self.controller.stop_group(8, self.now)
        self.controller.tick(self.now + timedelta(days=1))
        self.assertFalse(self.relays.states[15])
        self.controller.operation_held = True
        self.controller.apply(self.zones, self.now)
        self.assertFalse(self.relays.states[15])

    def test_shutdown_preserves_enabled_mode_for_restart(self):
        self.controller.tick(self.now)
        self.controller.close(self.now)
        self.assertFalse(self.relays.states[15])
        self.assertTrue(self.controller.zones[15].enabled)
        restarted = Controller(self.controller.zones, self.relays)
        restarted.tick(self.now)
        self.assertTrue(self.relays.states[15])

    def test_rejects_garden_mode_and_nonboolean_but_preserves_version9_times(self):
        bad = list(self.zones)
        bad[0] = replace(bad[0], continuous_schedule=True)
        with self.assertRaisesRegex(ValueError, "stations 9-16"):
            encode_zones(bad)
        bad[0] = replace(bad[0], continuous_schedule="true")
        with self.assertRaises(ValueError):
            encode_zones(bad)
        old = encode_zones(default_zones(16))
        old["version"] = 9
        for zone in old["zones"]:
            del zone["continuous_schedule"]
        old["zones"][15].update(enabled=True, schedule=[["00:00", "23:59"] + [""] * 6] * 7)
        decoded = decode_zones(old)
        self.assertFalse(decoded[15].continuous_schedule)
        self.assertEqual(decoded[15].schedule[0][:2], ("00:00", "23:59"))

    def test_saved_settings_profiles_and_overviews_retain_24_7_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_zones(path, self.zones)
            self.assertEqual(load_zones(path), self.zones)
            store = TimetableProfiles(path)
            store.save("Always on", self.zones, 8)
            loaded = TimetableProfiles(path).load("Always on", default_zones(16), 8)
            self.assertTrue(loaded[15].continuous_schedule)
            self.assertTrue(loaded[15].enabled)
        rows = overview_rows(self.zones, 0, 7, False)
        self.assertEqual([(row.start, row.stop, row.warning) for row in rows], [("24/7", "No stop", "")])

    def test_service_stop_persists_disabled_mode_and_does_not_resume_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_zones(path, self.zones)
            secondary = FakeDevice()
            secondary.host = "192.0.2.2"
            service = Service(path, FakeDevice(), secondary)
            service.tick()
            self.assertIn(15, service.controller.runs)
            old_revision = service.revision
            response = service.dispatch({"command": "stop", "channel": 15})
            self.assertFalse(load_zones(path)[15].enabled)
            self.assertNotEqual(old_revision, response["revision"])
            restarted = Service(path, FakeDevice(), secondary)
            restarted.tick()
            self.assertNotIn(15, restarted.controller.runs)

    def test_service_group_stops_and_backups_preserve_saved_mode(self):
        import system_backup
        for command in ("stop_appliances", "stop_all"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                save_zones(path, self.zones)
                secondary = FakeDevice()
                secondary.host = "192.0.2.2"
                service = Service(path, FakeDevice(), secondary)
                service.tick()
                payload = system_backup.capture(path, service.relay, service.history, service.controller.zones)
                system_backup.validate(payload)
                self.assertTrue(decode_zones(payload["settings"])[15].continuous_schedule)
                service.dispatch({"command": command})
                self.assertFalse(load_zones(path)[15].enabled)
                self.assertTrue(load_zones(path)[15].continuous_schedule)
