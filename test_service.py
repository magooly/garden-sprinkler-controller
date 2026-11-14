"""Headless service and IPC tests: fake hardware, real local Unix sockets."""

from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from controller import (
    Zone, default_zones, encode_zones, load_rain_pause, load_zones,
    save_rain_pause, save_zones,
)
from service import Service, exchange, serve
from service_client import ServiceClient
from waveshare_relay import WaveshareRelays
from test_controller import schedule_for


class FakeDevice(WaveshareRelays):
    def __init__(self) -> None:
        super().__init__()
        self.states = [False] * 8
        self.calls: list[tuple[int, bool]] = []
        self.fail = False

    def set_relay(self, channel: int, energized: bool) -> None:
        self.calls.append((channel, energized))
        if self.fail:
            raise OSError("Device offline")
        self.states[channel] = energized

    def read_states(self) -> list[bool]:
        if self.fail:
            raise OSError("Device offline")
        return list(self.states)


class RunningService(AbstractContextManager):
    def __init__(self, directory: Path, secondary: FakeDevice | None = None) -> None:
        self.config = directory / "settings.json"
        self.path = directory / "control.sock"
        self.device = FakeDevice()
        self.secondary = secondary
        self.stop = threading.Event()
        self.errors: list[Exception] = []

    def __enter__(self) -> "RunningService":
        def run() -> None:
            try:
                serve(self.config, self.device, self.path, self.stop, self.secondary)
            except Exception as error:
                # Surface every background-thread failure in the owning test.
                self.errors.append(error)

        self.socket_patch = patch("service.default_socket", return_value=self.path)
        self.socket_patch.start()
        self.thread = threading.Thread(target=run)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.path.exists() and not self.errors and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.errors or not self.path.exists():
            self.__exit__(None, None, None)
            raise AssertionError("Service failed to become ready")
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.thread.join(5)
        self.socket_patch.stop()
        if self.thread.is_alive():
            raise AssertionError("Service thread did not stop")
        if self.errors:
            raise AssertionError(f"Service thread failed: {self.errors}")


class ServiceTests(unittest.TestCase):
    def test_continuous_reminder_reports_elapsed_without_stopping(self) -> None:
        from dataclasses import replace
        self.service.dispatch({"command": "rain_pause", "paused": False})
        zones = list(self.service.controller.zones)
        zones[1] = replace(zones[1], pump_exempt=True, continuous_reminder_minutes=2)
        self.service.dispatch({"command": "save", "revision": self.service.revision, "settings": encode_zones(zones)})
        self.service.controller.clock = lambda: 100.0
        self.service.dispatch({"command": "continuous", "channel": 1})
        self.service.controller.clock = lambda: 220.0
        run = next(run for run in self.service.status()["runs"] if run["channel"] == 1)
        self.assertEqual(run["elapsed"], 120.0)
        self.assertTrue(self.device.states[1])
        self.assertEqual(self.service.controller.runs[1].source, "Continuous")

    def test_changing_only_reminder_does_not_stop_a_scheduled_run(self) -> None:
        from dataclasses import replace
        with patch("service.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 10, 12, 0)
            self.service.dispatch({"command": "rain_pause", "paused": False})
            runs = self.service.controller.runs.copy()
            self.assertIn(0, runs)
            zones = list(self.service.controller.zones)
            zones[0] = replace(zones[0], continuous_reminder_minutes=30)
            self.service.dispatch({"command": "save", "revision": self.service.revision, "settings": encode_zones(zones)})
            self.assertEqual(self.service.controller.runs, runs)

    def test_history_storage_failure_does_not_stop_relay_control(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        with patch.object(self.service.history, "_connect", side_effect=OSError("disk full")):
            with self.assertLogs(level="ERROR"):
                result = self.service.dispatch({"command": "manual", "channel": 1})
        self.assertTrue(self.device.states[1])
        self.assertFalse(result["fault"])
        self.assertIn("disk full", result["history_warning"])
        self.service.dispatch({"command": "stop", "channel": 1})

    def test_history_records_timer_expiry_and_stop_all(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.service.controller.clock = lambda: 10
        self.service.dispatch({"command": "manual", "channel": 1, "duration_minutes": 1})
        self.service.controller.clock = lambda: 70
        self.service.tick()
        self.service.dispatch({"command": "manual", "channel": 2})
        self.service.dispatch({"command": "stop_all"})
        events = self.service.dispatch({"command": "history"})["events"]
        self.assertTrue(any(event["channel"] == 1 and event["reason"] == "Manual timer ended"
                            for event in events))
        self.assertTrue(any(event["channel"] == 2 and event["reason"] == "Stop all"
                            for event in events))
        self.assertTrue(any(event["event"] == "STOP ALL" for event in events))

    def test_activity_records_starts_stops_rain_and_faults(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.service.dispatch({"command": "manual", "channel": 1})
        self.service.dispatch({"command": "stop", "channel": 1})
        self.service.dispatch({"command": "manual", "channel": 2})
        self.service.dispatch({"command": "rain_pause", "paused": True})
        events = self.service.dispatch({"command": "history"})["events"]
        self.assertTrue(any(event["channel"] == 1 and event["event"] == "ON"
                            and event["reason"] == "Manual" for event in events))
        self.assertTrue(any(event["channel"] == 1 and event["reason"] == "Stop channel"
                            for event in events))
        self.assertTrue(any(event["channel"] == 2 and event["reason"] == "Rain Pause"
                            for event in events))
        self.service.fail(OSError("relay offline"))
        events = self.service.dispatch({"command": "history"})["events"]
        self.assertTrue(any(event["event"] == "FAULT" and "relay offline" in event["reason"]
                            for event in events))
        self.assertTrue(self.service.status()["next_starts"][0])

    def test_manual_timer_limit_and_legacy_default(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.zones[1] = Zone("Fan", pump_exempt=True, max_manual_minutes=30)
        self.service.dispatch({
            "command": "save", "settings": encode_zones(self.zones),
            "revision": self.service.revision,
        })
        self.assertTrue(self.service.status()["manual_timers_supported"])
        self.service.dispatch({"command": "manual", "channel": 1})
        self.assertLessEqual(self.service.status()["runs"][-1]["remaining"], 600)
        self.service.dispatch({"command": "stop", "channel": 1})
        with self.assertRaisesRegex(ValueError, "maximum"):
            self.service.dispatch({"command": "manual", "channel": 1, "duration_minutes": 31})
        with self.assertRaisesRegex(ValueError, "Until stopped"):
            self.service.dispatch({"command": "continuous", "channel": 1})
        self.service.dispatch({"command": "manual", "channel": 1, "duration_minutes": 30})
        self.assertGreater(self.service.status()["runs"][-1]["remaining"], 1790)
        self.assertFalse(self.service.fault)

    def test_continuous_command_null_remaining_and_safety_guards(self) -> None:
        with self.assertRaisesRegex(ValueError, "Rain Pause"):
            self.service.dispatch({"command": "continuous", "channel": 1})
        with self.assertRaisesRegex(ValueError, "master pump"):
            self.service.dispatch({"command": "continuous", "channel": 7})
        self.service.dispatch({"command": "rain_pause", "paused": False})
        calls = list(self.device.calls)
        with self.assertRaisesRegex(ValueError, "master pump exclusion"):
            self.service.dispatch({"command": "continuous", "channel": 1})
        self.assertEqual(calls, self.device.calls)
        self.assertFalse(self.service.fault)
        self.zones[1] = Zone("Lights", pump_exempt=True)
        self.service.dispatch({
            "command": "save", "settings": encode_zones(self.zones),
            "revision": self.service.revision,
        })
        result = self.service.dispatch({"command": "continuous", "channel": 1})
        self.assertEqual(result["protocol"], 2)
        self.assertTrue(result["continuous_supported"])
        run = next(run for run in result["runs"] if run["channel"] == 1)
        self.assertEqual(run["source"], "Continuous")
        self.assertIsNone(run["remaining"])
        self.assertGreaterEqual(run["elapsed"], 0)
        self.service.controller.clock = lambda: 10**12
        self.service.tick()
        self.assertIn(1, self.service.controller.runs)
        self.service.dispatch({"command": "stop", "channel": 1})
        self.assertNotIn(1, self.service.controller.runs)

    def test_second_board_continuous_does_not_use_master_pump(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        service = Service(self.path, self.device, secondary)
        service.dispatch({"command": "rain_pause", "paused": False})
        service.dispatch({"command": "stop_all"})
        service.dispatch({"command": "continuous", "channel": 15})
        self.assertFalse(service.controller.pump_on)
        service.dispatch({"command": "stop", "channel": 15})
        zones = list(service.controller.zones)
        zones[15] = Zone("Lights", pump_exempt=True)
        service.dispatch({
            "command": "save", "settings": encode_zones(zones), "revision": service.revision,
        })
        service.dispatch({"command": "continuous", "channel": 15})
        self.assertTrue(secondary.states[7])
        self.assertFalse(self.device.states[7])
        service.dispatch({"command": "stop_all"})
        self.assertFalse(any(secondary.states))

    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "settings.json"
        self.device = FakeDevice()
        self.zones = default_zones()
        self.zones[0] = Zone("Lawn", True, (("0000", "2359") + ("",) * 6,) * 7)
        save_zones(self.path, self.zones)
        save_rain_pause(self.path, True)
        self.service = Service(self.path, self.device)

    def test_pause_restore_and_scheduler_without_a_gui(self) -> None:
        self.service.tick()
        self.assertFalse(any(self.device.states))
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.assertTrue(self.device.states[0])
        self.assertTrue(self.device.states[7])
        self.assertFalse(load_rain_pause(self.path))
        self.service.dispatch({"command": "rain_pause", "paused": True})
        self.assertFalse(any(self.device.states))

    def test_group_pause_save_and_resume_skip_current_period(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.assertIn(0, self.service.controller.runs)
        zones = list(self.service.controller.zones)
        zones[0] = replace(zones[0], paused_windows=(True, False, False, False))
        self.service.dispatch({
            "command": "save", "settings": encode_zones(zones),
            "revision": self.service.revision,
        })
        self.assertNotIn(0, self.service.controller.runs)
        self.assertTrue(load_zones(self.path)[0].paused_windows[0])
        restored = Service(self.path, self.device)
        restored.tick()
        self.assertNotIn(0, restored.controller.runs)
        zones[0] = replace(zones[0], paused_windows=(False,) * 4)
        restored.dispatch({
            "command": "save", "settings": encode_zones(zones),
            "revision": restored.revision,
        })
        restored.tick()
        self.assertNotIn(0, restored.controller.runs)
        restored.dispatch({"command": "manual", "channel": 0})
        self.assertIn(0, restored.controller.runs)

    def test_legacy_whole_zone_pause_file_no_longer_blocks_runs(self) -> None:
        self.path.with_name(self.path.name + ".zone-pauses.json").write_text(
            '{"0": {"paused": true, "resume_after": null}}'
        )
        service = Service(self.path, self.device)
        service.dispatch({"command": "rain_pause", "paused": False})
        self.assertIn(0, service.controller.runs)

    def test_profiles_preserve_rain_pause_and_reject_stale_loads(self) -> None:
        self.service.dispatch({
            "command": "profile_save", "name": "Summer", "revision": self.service.revision,
        })
        winter = list(self.zones)
        winter[0] = Zone("Renamed", rain_pause_exempt=True, pump_exempt=True)
        self.service.dispatch({
            "command": "save", "settings": encode_zones(winter), "revision": self.service.revision,
        })
        stale = self.service.revision
        result = self.service.dispatch({
            "command": "profile_load", "name": "Summer", "revision": stale,
        })
        zone = self.service.controller.zones[0]
        self.assertTrue(result["rain_paused"])
        self.assertEqual(zone.name, "Renamed")
        self.assertTrue(zone.pump_exempt)
        self.assertTrue(zone.rain_pause_exempt)
        self.assertTrue(zone.enabled)
        self.assertTrue(self.device.states[0])
        self.assertFalse(self.device.states[7])
        with self.assertRaisesRegex(ValueError, "another app"):
            self.service.dispatch({
                "command": "profile_load", "name": "Before last load", "revision": stale,
            })
        self.assertIn("Before last load", result["timetables"]["names"])

    def test_timetable_groups_only_change_the_target_bank_and_preserve_other_runs(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        service = Service(self.path, self.device, secondary)
        service.dispatch({"command": "rain_pause", "paused": False})
        service.dispatch({"command": "stop_all"})
        for bank in (0, 8):
            service.dispatch({
                "command": "profile_save", "name": "Summer", "bank": bank, "revision": service.revision,
            })
        zones = list(service.controller.zones)
        zones[0] = replace(zones[0], enabled=False)
        zones[8] = replace(zones[8], enabled=True, schedule=schedule_for((0,)))
        service.dispatch({"command": "save", "settings": encode_zones(zones), "revision": service.revision})
        service.dispatch({"command": "manual", "channel": 1})
        run = service.controller.runs[1]
        status = service.dispatch({
            "command": "profile_load", "name": "Summer", "bank": 8, "revision": service.revision,
        })
        self.assertEqual(service.controller.zones[:8], zones[:8])
        self.assertIs(service.controller.runs[1], run)
        self.assertFalse(service.controller.zones[8].enabled)
        self.assertIn("Before last load", status["timetable_groups"]["8"]["names"])
        self.assertNotIn("Before last load", status["timetable_groups"]["0"]["names"])
        with self.assertRaisesRegex(ValueError, "another app"):
            service.dispatch({
                "command": "profile_load", "name": "Summer", "bank": 8, "revision": "stale",
            })
        for bank in (True, 1, "8", 16):
            with self.subTest(bank=bank), self.assertRaisesRegex(ValueError, "group"):
                service.dispatch({
                    "command": "profile_save", "name": "Bad", "bank": bank, "revision": service.revision,
                })

    def test_second_board_routes_all_channels_without_operating_the_pump(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        service = Service(self.path, self.device, secondary)
        service.dispatch({"command": "rain_pause", "paused": False})
        service.dispatch({"command": "stop_all"})
        for channel in range(8, 16):
            service.dispatch({"command": "manual", "channel": channel})
            self.assertTrue(secondary.states[channel - 8])
            self.assertFalse(self.device.states[7])
        service.dispatch({"command": "manual", "channel": 1})
        self.assertTrue(self.device.states[7])
        service.dispatch({"command": "stop", "channel": 1})
        self.assertFalse(self.device.states[7])
        self.assertTrue(all(secondary.states))
        service.dispatch({"command": "stop_all"})
        self.assertFalse(any(secondary.states))
        self.assertEqual(service.status()["channel_count"], 16)

    def test_profile_load_stops_routine_but_preserves_manual_run(self) -> None:
        self.service.dispatch({
            "command": "profile_save", "name": "Summer", "revision": self.service.revision,
        })
        self.service.dispatch({
            "command": "save", "settings": encode_zones(default_zones()),
            "revision": self.service.revision,
        })
        self.service.dispatch({
            "command": "profile_save", "name": "Winter", "revision": self.service.revision,
        })
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.service.dispatch({"command": "manual", "channel": 1})
        manual = self.service.controller.runs[1]
        for name, on in (("Summer", True), ("Winter", False)):
            self.service.dispatch({
                "command": "profile_load", "name": name, "revision": self.service.revision,
            })
            self.assertEqual(self.device.states[0], on)
            self.assertIs(self.service.controller.runs[1], manual)
            self.assertTrue(self.device.states[1])

    def test_corrupt_profiles_prevent_startup_before_hardware_writes(self) -> None:
        self.service.timetables.path.write_text("{invalid")
        device = FakeDevice()
        with self.assertRaises(ValueError):
            Service(self.path, device)
        self.assertEqual(device.calls, [])

    def test_failed_profile_load_reports_fault_and_retains_recovery(self) -> None:
        self.service.dispatch({
            "command": "profile_save", "name": "Summer", "revision": self.service.revision,
        })
        winter = default_zones()
        self.service.dispatch({
            "command": "save", "settings": encode_zones(winter), "revision": self.service.revision,
        })
        with patch("service.save_zones", side_effect=OSError("Disk full")):
            result = self.service.dispatch({
                "command": "profile_load", "name": "Summer", "revision": self.service.revision,
            })
        self.assertIn("Disk full", result["fault"])
        self.assertTrue(self.service.controller.closed)
        self.assertFalse(any(self.device.states))
        self.assertEqual(load_zones(self.path), winter)
        self.assertTrue(result["timetables"]["modified"])
        self.assertEqual(self.service.timetables.load("Before last load", winter), winter)

    def test_removing_second_board_preserves_settings_but_blocks_its_outputs(self) -> None:
        zones = default_zones(16)
        zones[8] = Zone("Second", True, self.zones[0].schedule, rain_pause_exempt=True)
        save_zones(self.path, zones)
        service = Service(self.path, self.device)
        service.tick()
        self.assertEqual(service.status()["channel_count"], 8)
        self.assertNotIn(8, service.controller.runs)
        self.assertEqual(load_zones(self.path), zones)
        with self.assertRaisesRegex(ValueError, "not configured"):
            service.dispatch({"command": "manual", "channel": 8})
        self.assertFalse(service.fault)

    def test_second_board_settings_round_trip_and_manual_deadline(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        service = Service(self.path, self.device, secondary)
        zones = list(service.controller.zones)
        zones[15] = Zone("Independent", rain_pause_exempt=True)
        service.dispatch({
            "command": "save", "revision": service.revision,
            "settings": encode_zones(zones),
        })
        self.assertEqual(encode_zones(load_zones(self.path))["version"], 9)
        service.dispatch({"command": "manual", "channel": 15})
        self.assertTrue(secondary.states[7])
        deadline = service.controller.runs[15].deadline
        service.controller.clock = lambda: deadline
        service.tick()
        self.assertFalse(secondary.states[7])
        self.assertNotIn(15, service.controller.runs)
        self.assertFalse(self.device.states[7])
        service.controller.close(datetime.now())
        restarted = Service(self.path, self.device, secondary)
        self.assertEqual(restarted.controller.zones, zones)

    def test_second_board_schedules_rain_pause_and_failure_shutdown(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        zones = default_zones(16)
        zones[8] = Zone("Second", True, self.zones[0].schedule, rain_pause_exempt=True)
        zones[9] = Zone("Included", True, self.zones[0].schedule)
        save_zones(self.path, zones)
        service = Service(self.path, self.device, secondary)
        service.tick()
        self.assertTrue(secondary.states[0])
        self.assertFalse(secondary.states[1])
        self.assertFalse(self.device.states[7])
        service.dispatch({"command": "rain_pause", "paused": False})
        self.assertTrue(secondary.states[1])
        service.dispatch({"command": "rain_pause", "paused": True})
        self.assertTrue(secondary.states[0])
        self.assertFalse(secondary.states[1])
        secondary.fail = True
        with self.assertLogs(level="ERROR"):
            service.dispatch({"command": "manual", "channel": 8})
        self.assertFalse(service.fault)
        self.assertTrue(service.controller.channel_faults[8])
        self.assertFalse(service.controller.closed)
        self.assertFalse(any(self.device.states))

    def test_exempt_zone_manual_watering_is_allowed_during_rain_pause(self) -> None:
        zones = list(self.service.controller.zones)
        zones[1] = Zone("Exempt", rain_pause_exempt=True)
        result = self.service.dispatch({
            "command": "save", "revision": self.service.revision,
            "settings": encode_zones(zones),
        })
        self.assertTrue(result["rain_paused"])
        self.service.dispatch({"command": "manual", "channel": 1})
        self.assertTrue(self.device.states[1])
        self.assertTrue(self.device.states[7])
        with self.assertRaisesRegex(ValueError, "Rain Pause"):
            self.service.dispatch({"command": "manual", "channel": 0})

    def test_save_optimistic_revision_prevents_lost_changes(self) -> None:
        revision = self.service.revision
        zones = default_zones()
        zones[1] = Zone("New name")
        result = self.service.dispatch(
            {"command": "save", "revision": revision, "settings": encode_zones(zones)}
        )
        self.assertNotEqual(revision, result["revision"])
        self.assertEqual(load_zones(self.path)[1].name, "New name")
        with self.assertRaisesRegex(ValueError, "another app"):
            self.service.dispatch(
                {"command": "save", "revision": revision, "settings": encode_zones(self.zones)}
            )
        self.assertEqual(load_zones(self.path)[1].name, "New name")

    def test_pump_exclusion_is_persisted_and_returned_to_desktops(self) -> None:
        zones = list(self.zones)
        zones[1] = Zone("Independent", rain_pause_exempt=True, pump_exempt=True)
        result = self.service.dispatch({
            "command": "save", "revision": self.service.revision,
            "settings": encode_zones(zones),
        })
        self.assertTrue(load_zones(self.path)[1].pump_exempt)
        self.assertTrue(result["settings"]["zones"][1]["pump_exempt"])
        self.service.dispatch({"command": "manual", "channel": 1})
        self.assertTrue(self.device.states[1])
        self.assertFalse(self.device.states[7])

    def test_old_service_settings_error_explains_restart_without_applying(self) -> None:
        client = ServiceClient.__new__(ServiceClient)
        client.path = Path("unused")
        client.revision = "revision"
        with patch(
            "service_client.exchange",
            side_effect=ValueError("Unsupported settings format; expected version 1, 2, or 3."),
        ):
            with self.assertRaisesRegex(ValueError, "Restart garden-sprinkler.service") as error:
                client.apply(default_zones(), datetime.now())
        self.assertIn("Your edits have not been applied", str(error.exception))

    def test_invalid_commands_never_fault_or_energize_hardware(self) -> None:
        calls = list(self.device.calls)
        for request in (
            None, {}, {"command": "bad"}, {"command": "manual", "channel": True},
            {"command": "manual", "channel": -1}, {"command": "manual", "channel": 8},
            {"command": "manual", "channel": 7}, {"command": "manual", "channel": 0},
            {"command": "rain_pause", "paused": "false"},
            {"command": "save", "revision": self.service.revision, "settings": {}},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.service.dispatch(request)
        self.assertEqual(self.device.calls, calls)
        self.assertFalse(self.service.fault)

    def test_hardware_fault_latches_and_stop_all_remains_available(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.device.fail = True
        with self.assertLogs(level="ERROR"):
            self.service.dispatch({"command": "manual", "channel": 1})
        self.assertTrue(self.service.fault)
        self.assertTrue(self.service.controller.closed)
        calls = len(self.device.calls)
        self.service.tick()
        self.assertEqual(len(self.device.calls), calls)
        with self.assertRaisesRegex(ValueError, "Service fault"):
            self.service.dispatch({"command": "manual", "channel": 2})
        self.device.fail = False
        self.service.dispatch({"command": "stop_all"})
        self.assertFalse(any(self.device.states))
        self.assertTrue(self.service.fault)

    def test_pause_save_failure_stops_and_faults(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        with patch("service.save_rain_pause", side_effect=OSError("Disk full")), self.assertLogs(level="ERROR"):
            result = self.service.dispatch({"command": "rain_pause", "paused": True})
        self.assertIn("Disk full", result["fault"])
        self.assertFalse(any(self.device.states))
        self.assertTrue(self.service.controller.rain_paused)

    def test_read_failure_reports_fault_instead_of_success(self) -> None:
        self.device.fail = True
        with self.assertLogs(level="ERROR"):
            result = self.service.dispatch({"command": "read_states"})
        self.assertNotIn("states", result)
        self.assertTrue(result["fault"])

    def test_restart_restores_saved_schedule_pause_and_changes_revision(self) -> None:
        revision = self.service.revision
        self.service.controller.close(datetime.now())
        restarted = Service(self.path, self.device)
        self.assertNotEqual(restarted.revision, revision)
        self.assertEqual(restarted.controller.zones, self.zones)
        self.assertTrue(restarted.controller.rain_paused)
        restarted.tick()
        self.assertFalse(any(self.device.states))
        restarted.dispatch({"command": "rain_pause", "paused": False})
        restarted.controller.close(datetime.now())
        resumed = Service(self.path, self.device)
        resumed.tick()
        self.assertTrue(self.device.states[0])
        self.assertTrue(self.device.states[7])
        self.assertEqual(resumed.controller.runs[0].source, "Routine")

    def test_failed_on_then_lost_reply_never_replays_a_manual_command(self) -> None:
        self.service.dispatch({"command": "rain_pause", "paused": False})
        self.device.calls.clear()
        self.service.dispatch({"command": "manual", "channel": 1})
        self.service.dispatch({"command": "status"})
        self.assertEqual(self.device.calls, [(1, True)])


class SocketTests(unittest.TestCase):
    def test_continuous_run_desktop_sync_disconnect_stop_and_restart(self) -> None:
        setup_client = ServiceClient(self.running.path)
        zones = list(setup_client.zones)
        zones[1] = Zone("Lights", pump_exempt=True)
        setup_client.apply(zones, datetime.now())
        exchange(self.running.path, {"command": "continuous", "channel": 1})
        client = ServiceClient(self.running.path)
        self.assertIsNone(client.runs[1].deadline)
        self.assertEqual(client.runs[1].source, "Continuous")
        client.close(datetime.now())
        time.sleep(0.3)
        self.assertTrue(self.running.device.states[1])
        second = ServiceClient(self.running.path)
        second.stop(1, datetime.now())
        self.assertFalse(self.running.device.states[1])
        exchange(self.running.path, {"command": "continuous", "channel": 1})
        self.running.stop.set()
        self.running.thread.join(5)
        restarted = Service(self.running.config, self.running.device)
        self.assertNotIn(1, restarted.controller.runs)
        self.assertFalse(self.running.device.states[1])

    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.running = self.enterContext(RunningService(Path(self.directory)))

    def test_two_desktops_disconnect_without_stopping_watering(self) -> None:
        first = ServiceClient(self.running.path)
        second = ServiceClient(self.running.path)
        first.start_manual(1, datetime.now())
        second.sync()
        self.assertIn(1, second.runs)
        self.assertTrue(second.pump_on)
        calls = len(self.running.device.calls)
        first.close(datetime.now())
        second.close(datetime.now())
        time.sleep(0.3)
        self.assertTrue(self.running.device.states[1])
        self.assertTrue(self.running.device.states[7])
        self.assertEqual(len(self.running.device.calls), calls)

    def test_client_manual_timer_and_older_service_guard(self) -> None:
        client = ServiceClient(self.running.path)
        zones = list(client.zones)
        zones[1] = Zone("Fan", pump_exempt=True, max_manual_minutes=30)
        client.apply(zones, datetime.now())
        client.start_manual(1, datetime.now(), duration_minutes=25)
        self.assertGreater(client.runs[1].deadline - client.clock(), 1490)
        self.assertTrue(self.running.device.states[1])
        client.stop(1, datetime.now())
        with self.assertRaisesRegex(ValueError, "maximum"):
            client.start_manual(1, datetime.now(), duration_minutes=31)
        self.assertFalse(self.running.device.states[1])
        client.manual_timers_supported = False
        with self.assertRaisesRegex(ValueError, "Restart"):
            client.start_manual(1, datetime.now(), duration_minutes=30)
        self.assertFalse(self.running.device.states[1])

    def test_commands_saves_and_state_roundtrip(self) -> None:
        client = ServiceClient(self.running.path)
        zones = default_zones()
        zones[2] = Zone("Greenhouse")
        client.apply(zones, datetime.now())
        self.assertEqual(load_zones(self.running.config)[2].name, "Greenhouse")
        client.toggle_test(2, datetime.now())
        self.assertEqual(client.runs[2].source, "Test")
        self.assertTrue(client.read_states()[7])
        client.set_rain_pause(True, datetime.now())
        self.assertFalse(client.runs)
        self.assertTrue(load_rain_pause(self.running.config))
        client.set_rain_pause(False, datetime.now())
        client.start_manual(0, datetime.now())
        client.stop_all(datetime.now())
        self.assertFalse(any(self.running.device.states))

    def test_full_backup_exceeds_old_socket_limit_and_restores_with_hold(self) -> None:
        import json
        from activity_history import ActivityHistory
        import system_backup
        password = "four unrelated backup words"
        history = ActivityHistory(self.running.config)
        connection = history._connect()
        try:
            with connection:
                connection.executemany(
                    "INSERT INTO events (timestamp, channel, name, event, reason) VALUES (?, ?, ?, ?, ?)",
                    [("2026-11-10T00:00:00", 0, "Lawn", "ON", "x" * 300)] * 500,
                )
        finally:
            connection.close()
        client = ServiceClient(self.running.path)
        archive = client.backup("backup_export", password=password)["archive"]
        self.assertGreater(len(json.dumps(archive)), 65536)
        preview = client.backup("backup_preview", archive=archive, password=password)
        client.backup("backup_restore", archive=archive, password=password, revision=preview["revision"])
        self.assertTrue(client.operation_held)
        self.assertTrue(client.restore_restart_required)
        self.assertTrue(system_backup.held(self.running.config))
        with self.assertRaisesRegex(ValueError, "held"):
            client.start_manual(1, datetime.now())

    def test_socket_is_private_and_invalid_request_is_explicit(self) -> None:
        self.assertEqual(self.running.path.stat().st_mode & 0o777, 0o600)
        with self.assertLogs(level="WARNING"):
            with self.assertRaisesRegex(ValueError, "Unknown command"):
                exchange(self.running.path, {"command": "not-a-command"})
        self.assertFalse(self.running.device.fail)

    def test_truncated_request_cannot_block_scheduler_indefinitely(self) -> None:
        with self.assertLogs(level="WARNING"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(self.running.path))
                connection.sendall(b'{"command":')
                self.assertEqual(connection.recv(1), b"")
        self.assertEqual(ServiceClient(self.running.path).rain_paused, False)

    def test_duplicate_owner_fails_before_any_hardware_commands(self) -> None:
        calls = len(self.running.device.calls)
        with self.assertRaisesRegex(RuntimeError, "Another controller"):
            serve(self.running.config, self.running.device, self.running.path,
                  threading.Event())
        self.assertEqual(len(self.running.device.calls), calls)
        self.assertTrue(self.running.path.exists())

    def test_an_existing_primary_cannot_be_claimed_as_a_second_board(self) -> None:
        primary = FakeDevice()
        primary.host = "192.0.2.3"
        calls = len(self.running.device.calls)
        with self.assertRaisesRegex(RuntimeError, "Another controller"):
            serve(
                self.running.config.with_name("other.json"), primary,
                self.running.path.with_name("other.sock"), threading.Event(),
                self.running.device,
            )
        self.assertFalse(primary.calls)
        self.assertEqual(len(self.running.device.calls), calls)

    def test_graceful_shutdown_turns_pump_off_first(self) -> None:
        client = ServiceClient(self.running.path)
        client.start_manual(0, datetime.now())
        calls = len(self.running.device.calls)
        self.running.stop.set()
        self.running.thread.join(5)
        self.assertFalse(self.running.thread.is_alive())
        self.assertEqual(self.running.device.calls[calls], (7, False))
        self.assertFalse(any(self.running.device.states))
        self.assertFalse(self.running.path.exists())


if __name__ == "__main__":
    unittest.main()
