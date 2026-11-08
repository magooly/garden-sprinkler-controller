"""Timetable profiles never replace hardware-related zone settings."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from controller import Zone, default_zones
from test_controller import schedule_for
from timetable_profiles import RECOVERY_PROFILE, TimetableProfiles


class TimetableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "settings.json"
        self.store = TimetableProfiles(self.path)

    def test_named_profiles_persist_and_preserve_exclusions(self) -> None:
        for count in (8, 16):
            zones = default_zones(count)
            zones[0] = Zone("Summer name", True, schedule_for((0,)))
            self.store.save("Summer", zones)
            winter = list(zones)
            winter[0] = Zone("Current name", False, schedule_for((1,)), rain_pause_exempt=True, pump_exempt=True)
            self.store.save("Winter", winter)
            loaded = self.store.load("Summer", winter)
            self.assertEqual(loaded[0].name, "Current name")
            self.assertTrue(loaded[0].rain_pause_exempt)
            self.assertTrue(loaded[0].pump_exempt)
            self.assertEqual(loaded[0].schedule, zones[0].schedule)
            self.assertTrue(loaded[0].enabled)
            restarted = TimetableProfiles(self.path)
            self.assertEqual(restarted.status(loaded)["selected"], "Summer")
            self.assertFalse(restarted.status(loaded)["modified"])
            self.assertTrue(restarted.status(winter)["modified"])
            recovery = restarted.load(RECOVERY_PROFILE, loaded)
            self.assertEqual(recovery, winter)

    def test_different_channel_count_rejected_without_replacing_recovery(self) -> None:
        self.store.save("Eight", default_zones())
        original = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "channel count"):
            self.store.load("Eight", default_zones(16))
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_invalid_file_is_not_silently_reset(self) -> None:
        self.store.save("Summer", default_zones())
        data = json.loads(self.store.path.read_text())
        data["profiles"]["Summer"][0]["pump_exempt"] = True
        self.store.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            TimetableProfiles(self.path)

    def test_failed_write_preserves_saved_profile(self) -> None:
        zones = default_zones()
        self.store.save("Summer", zones)
        original = self.store.path.read_bytes()
        with patch("controller.os.replace", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.store.save("Winter", zones)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.status(zones)["names"], ["Summer"])

    def test_paused_windows_are_loaded_but_names_do_not_mark_modified(self) -> None:
        zones = default_zones()
        zones[0] = replace(zones[0], paused_windows=(True, False, False, False))
        self.store.save("Summer", zones)
        changed = list(zones)
        changed[0] = replace(changed[0], name="Renamed", pump_exempt=True)
        self.assertFalse(self.store.status(changed)["modified"])
        loaded = self.store.load("Summer", default_zones())
        self.assertTrue(loaded[0].paused_windows[0])

    def test_invalid_and_reserved_names_are_rejected(self) -> None:
        for name in ("", " " * 3, "x" * 41, RECOVERY_PROFILE):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.save(name, default_zones())
