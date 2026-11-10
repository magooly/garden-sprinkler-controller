"""Timetable profiles never replace hardware-related zone settings."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from controller import Zone, default_zones
from test_controller import schedule_for
from timetable_profiles import RECOVERY_PROFILE, TimetableProfiles, timetable


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

    def test_eight_channel_profile_loads_into_primary_bank_after_expansion(self) -> None:
        self.store.save("Eight", default_zones())
        zones = default_zones(16)
        zones[8] = Zone("Lights", True, schedule_for((1,)))
        loaded = self.store.load("Eight", zones)
        self.assertEqual(loaded[8:], zones[8:])
        self.assertEqual(self.store.status(loaded, 8)["names"], [])

    def test_invalid_file_is_not_silently_reset(self) -> None:
        self.store.save("Summer", default_zones())
        data = json.loads(self.store.path.read_text())
        data["groups"]["0"]["profiles"]["Summer"][0]["pump_exempt"] = True
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

    def test_banks_have_independent_names_selections_modified_state_and_recovery(self) -> None:
        zones = default_zones(16)
        zones[0] = Zone("Garden", True, schedule_for((0,)))
        zones[8] = Zone("Fan", True, schedule_for((1,)))
        self.store.save("Summer", zones, 0)
        self.store.save("Summer", zones, 8)
        changed = list(zones)
        changed[0] = replace(changed[0], enabled=False)
        changed[8] = replace(changed[8], enabled=False, name="New fan", pump_exempt=True)
        self.store.save("Lights", changed, 8)
        self.assertNotIn("Lights", self.store.status(changed, 0)["names"])
        self.assertEqual(self.store.status(changed, 0)["selected"], "Summer")
        loaded = self.store.load("Summer", changed, 8)
        self.assertEqual(loaded[:8], changed[:8])
        self.assertTrue(loaded[8].enabled)
        self.assertEqual(loaded[8].name, "New fan")
        self.assertTrue(loaded[8].pump_exempt)
        self.assertTrue(self.store.status(loaded, 0)["modified"])
        self.assertFalse(self.store.status(loaded, 8)["modified"])
        loaded = self.store.load("Summer", loaded, 0)
        self.assertEqual(loaded[8].schedule, zones[8].schedule)
        restarted = TimetableProfiles(self.path)
        restored = restarted.load(RECOVERY_PROFILE, loaded, 8)
        self.assertTrue(restored[0].enabled)
        self.assertFalse(restored[8].enabled)
        restored = restarted.load(RECOVERY_PROFILE, restored, 0)
        self.assertEqual(restored, changed)

    def test_legacy_profiles_split_without_losing_names_or_eight_channel_profiles(self) -> None:
        zones = default_zones(16)
        zones[8] = Zone("Fan", True, schedule_for((1,)))
        legacy = {
            "version": 1, "selected": "Both",
            "profiles": {"Both": timetable(zones), "Garden only": timetable(zones[:8])},
        }
        self.store.path.write_text(json.dumps(legacy))
        store = TimetableProfiles(self.path)
        self.assertEqual(store.status(zones, 0)["names"], ["Both", "Garden only"])
        self.assertEqual(store.status(zones, 8)["names"], ["Both"])
        self.assertFalse(store.status(zones, 8)["modified"])
        store.save("New lights", zones, 8)
        self.assertEqual(json.loads(store.path.read_text())["version"], 2)
        reloaded = TimetableProfiles(self.path)
        self.assertEqual(reloaded.status(zones, 0)["selected"], "Both")
        self.assertIn("New lights", reloaded.status(zones, 8)["names"])
        self.assertEqual(reloaded.load("Both", default_zones(16), 8)[8].schedule, zones[8].schedule)

    def test_invalid_banks_do_not_write_or_change_recovery(self) -> None:
        self.store.save("Summer", default_zones())
        original = self.store.path.read_bytes()
        for bank in (True, "8", 1, -8, 8, None):
            with self.subTest(bank=bank):
                with self.assertRaises(ValueError):
                    self.store.save("Bad", default_zones(), bank)
                with self.assertRaises(ValueError):
                    self.store.load("Summer", default_zones(), bank)
        self.assertEqual(self.store.path.read_bytes(), original)
