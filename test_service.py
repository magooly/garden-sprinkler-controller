"""Headless service and IPC tests: fake hardware, real local Unix sockets."""

from contextlib import AbstractContextManager
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
    def __init__(self, directory: Path) -> None:
        self.config = directory / "settings.json"
        self.path = directory / "control.sock"
        self.device = FakeDevice()
        self.stop = threading.Event()
        self.errors: list[Exception] = []

    def __enter__(self) -> "RunningService":
        def run() -> None:
            try:
                serve(self.config, self.device, self.path, self.stop)
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
