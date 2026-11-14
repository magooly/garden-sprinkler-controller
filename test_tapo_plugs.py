"""Tapo setup and transport tests with no real network or relay writes."""

import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
import fcntl
import contextlib
import io
from unittest.mock import AsyncMock, MagicMock, patch

from kasa import Credentials, KasaException
from kasa.smart.smartdevice import SmartDevice

from controller import save_zones, default_zones
from phone_server import PhoneRequestHandler
from service import Service
from service_client import ServiceClient
from tapo_plugs import (
    Plug, credentials_path, decode_mapping, load_credentials,
    load_mapping, mapping_path, plug_operation,
    main,
)
from test_service import FakeDevice

MAC = "12:34:56:78:9a:bc"


class TapoTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = MagicMock(spec=SmartDevice)
        self.device.mac = MAC
        self.device.model = "P110M"
        self.device.is_on = True
        self.device.update = AsyncMock()
        self.device.disconnect = AsyncMock()
        self.device.protocol = MagicMock()
        self.device.protocol.query = AsyncMock()
        self.discover = self.enterContext(patch(
            "tapo_plugs.Discover.discover_single", new_callable=AsyncMock, return_value=self.device,
        ))
        self.plug = Plug("192.168.0.50", MAC)
        self.credentials = Credentials("test@example.invalid", "not-a-real-password")

    async def test_switch_is_verified_and_write_is_not_retried(self) -> None:
        self.assertTrue(await plug_operation(self.plug, self.credentials, True))
        self.device.protocol.query.assert_awaited_once_with(
            {"set_device_info": {"device_on": True}}, retry_count=0,
        )
        self.assertEqual(self.device.update.await_count, 2)
        self.device.disconnect.assert_awaited_once()

    async def test_wrong_identity_or_model_never_switches(self) -> None:
        for mac, model in (("00:00:00:00:00:01", "P110M"), (MAC, "P100"), ("bad", "P110M")):
            self.device.mac, self.device.model = mac, model
            with self.assertRaises(OSError):
                await plug_operation(self.plug, self.credentials, True)
        self.device.protocol.query.assert_not_awaited()

    async def test_read_only_request_never_switches(self) -> None:
        self.assertTrue(await plug_operation(self.plug, self.credentials, None))
        self.device.protocol.query.assert_not_awaited()

    async def test_superseded_request_is_not_written_after_discovery(self) -> None:
        self.assertTrue(await plug_operation(
            self.plug, self.credentials, False, permit_write=lambda: False,
        ))
        self.device.protocol.query.assert_not_awaited()

    async def test_readback_mismatch_is_not_success(self) -> None:
        self.device.is_on = False
        with self.assertRaisesRegex(OSError, "readback"):
            await plug_operation(self.plug, self.credentials, True)
        self.device.disconnect.assert_awaited_once()

    async def test_library_failure_is_sanitized_and_session_closed(self) -> None:
        self.device.update.side_effect = KasaException("sensitive protocol content")
        with self.assertRaises(OSError) as caught:
            await plug_operation(self.plug, self.credentials, True)
        self.assertNotIn("sensitive", str(caught.exception))
        self.device.protocol.query.assert_not_awaited()
        self.device.disconnect.assert_awaited_once()

    async def test_total_operation_timeout_is_bounded(self) -> None:
        async def stall():
            await asyncio.sleep(10)

        self.device.update.side_effect = stall
        with patch("tapo_plugs.OPERATION_SECONDS", 0.01):
            with self.assertRaisesRegex(OSError, "timed out"):
                await plug_operation(self.plug, self.credentials, True)
        self.device.protocol.query.assert_not_awaited()
        self.device.disconnect.assert_awaited_once()

    async def test_timeout_after_write_is_reported_as_unknown_without_retrying(self) -> None:
        calls = 0

        async def update():
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(10)

        self.device.update.side_effect = update
        with patch("tapo_plugs.OPERATION_SECONDS", 0.05):
            with self.assertRaisesRegex(OSError, "state is unknown"):
                await plug_operation(self.plug, self.credentials, True)
        self.device.protocol.query.assert_awaited_once_with(
            {"set_device_info": {"device_on": True}}, retry_count=0,
        )
        self.device.disconnect.assert_awaited_once()


class TapoSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(self.directory) / "settings.json"
        self.primary = FakeDevice()
        self.mapping = {"version": 1, "plugs": [None] * 8}
        self.write_mapping()

    def write_mapping(self) -> None:
        mapping_path(self.config).write_text(json.dumps(self.mapping), encoding="utf-8")

    def credentials(self) -> None:
        path = credentials_path(self.config)
        path.write_text(json.dumps({"username": "test@example.invalid", "password": "private-value"}))
        path.chmod(0o600)

    def test_bad_assignments_are_rejected(self) -> None:
        for record in ({"host": "8.8.8.8", "mac": MAC}, {"host": "192.168.0.50", "mac": "bad"},
                       {"host": "127.0.0.1", "mac": MAC}):
            with self.assertRaises(ValueError):
                decode_mapping({"version": 1, "plugs": [record] + [None] * 7})
        with self.assertRaisesRegex(ValueError, "once"):
            decode_mapping({"version": 1, "plugs": [{"host": "192.168.0.50", "mac": MAC}] * 8})
        self.assertEqual(load_mapping(mapping_path(self.config)), (None,) * 8)

    def test_assignment_preserves_other_policy_but_does_not_transfer_it(self) -> None:
        self.mapping["plugs"][7] = {"host": "192.168.0.121", "mac": MAC, "off_peak": True}
        self.write_mapping()
        for channel, host, mac, enabled in (
            (9, "192.168.0.50", "00:00:00:00:00:01", True),
            (16, "192.168.0.122", "00:00:00:00:00:02", False),
        ):
            with patch("sys.argv", [
                "tapo_plugs.py", "--config", str(self.config), "assign", str(channel), host, mac,
            ]), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(load_mapping(mapping_path(self.config))[7].off_peak, enabled)

    def test_credentials_are_private_and_not_in_status(self) -> None:
        self.credentials()
        path = credentials_path(self.config)
        self.assertEqual(load_credentials(path).username, "test@example.invalid")
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            load_credentials(path)
        path.chmod(0o600)
        service = Service(self.config, self.primary)
        self.assertNotIn("private-value", json.dumps(service.status()))
        self.assertNotIn("test@example.invalid", json.dumps(service.status()))

    def test_credentials_setup_explains_backup_inclusion(self) -> None:
        output = io.StringIO()
        with (
            patch("sys.argv", ["tapo_plugs.py", "--config", str(self.config), "credentials"]),
            patch("builtins.input", return_value="test@example.invalid"),
            patch("tapo_plugs.getpass", return_value="private-value"),
            contextlib.redirect_stdout(output),
        ):
            main()
        self.assertIn("Encrypted full-system backups include them", output.getvalue())
        self.assertIn("settings exports do not", output.getvalue())
        self.assertNotIn("private-value", output.getvalue())

    def test_unassigned_slots_do_not_send_fake_off_or_start_schedules(self) -> None:
        zones = default_zones(16)
        zones[8] = replace(zones[8], enabled=True, schedule=(("00:00", "23:59") + ("",) * 6,) * 7)
        save_zones(self.config, zones)
        service = Service(self.config, self.primary)
        self.assertEqual(len(self.primary.calls), 8)
        self.assertEqual(service.controller.available_channels, frozenset(range(8)))
        service.tick()
        self.assertNotIn(8, service.controller.runs)
        self.assertIsNone(service.status()["next_starts"][8])
        for command in ("manual", "continuous", "test", "stop"):
            with self.assertRaisesRegex(ValueError, "no assigned"):
                service.dispatch({"command": command, "channel": 8})
        self.assertFalse(service.fault)
        public = PhoneRequestHandler._public_status(service.status())
        self.assertFalse(public["channels"][8]["available"])
        self.assertFalse(public["channels"][8]["tapo"]["assigned"])
        self.assertIsNone(service.relay.read_states()[8])

    def test_five_slots_are_named_without_enabling_outputs(self) -> None:
        service = Service(self.config, self.primary)
        self.assertEqual(service.controller.channel_count, 16)
        self.assertEqual([zone.name for zone in service.controller.zones[8:13]],
                         [f"Tapo Plug {number}" for number in range(1, 6)])
        self.assertFalse(any(zone.enabled for zone in service.controller.zones[8:]))
        client = ServiceClient.__new__(ServiceClient)
        client._update(service.status())
        self.assertNotIn(8, client.available_channels)
        self.assertIsNotNone(client.tapo)

    def test_assigned_plug_routes_independently_and_failures_are_explicit(self) -> None:
        self.mapping["plugs"][0] = {"host": "192.168.0.50", "mac": MAC}
        self.write_mapping()
        self.credentials()

        async def operation(_plug, _credentials, state):
            return state

        with patch("tapo_plugs.plug_operation", side_effect=operation) as transport:
            service = Service(self.config, self.primary)
            self.assertIn(8, service.controller.available_channels)
            transport.assert_called_once()
            self.assertFalse(transport.call_args.args[2])
            service.dispatch({"command": "continuous", "channel": 8})
            self.assertFalse(service.controller.pump_on)
            self.assertFalse(self.primary.states[7])
            self.assertEqual(service.controller.runs[8].source, "Continuous")
            service.dispatch({"command": "stop", "channel": 8})
            self.assertNotIn(8, service.controller.runs)
            events = service.history.page(0)["events"]
            self.assertTrue(any(event["channel"] == 8 and event["event"] == "ON" for event in events))
            transport.side_effect = OSError("offline")
            service.dispatch({"command": "manual", "channel": 8})
            self.assertFalse(service.fault)
            self.assertIn("offline", service.controller.channel_faults[8])
            self.assertFalse(service.controller.closed)
            self.assertIsNone(service.relay.states[8])
            self.assertFalse(any(self.primary.states))

    def test_tapo_and_second_board_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "either Tapo"):
            Service(self.config, self.primary, FakeDevice())

    def test_five_assigned_plugs_have_independent_timers_and_rain_pause(self) -> None:
        for index in range(5):
            self.mapping["plugs"][index] = {
                "host": f"192.168.0.{50 + index}", "mac": f"12:34:56:78:9a:{index:02x}",
            }
        self.write_mapping()
        self.credentials()
        states = {}

        async def operation(plug, _credentials, state):
            if state is not None:
                states[plug.mac] = state
            return states[plug.mac]

        with patch("tapo_plugs.plug_operation", side_effect=operation):
            service = Service(self.config, self.primary)
            self.assertEqual(len(states), 5)
            self.assertFalse(any(states.values()))
            service.controller.clock = lambda: 100
            for channel in range(8, 13):
                service.dispatch({"command": "manual", "channel": channel, "duration_minutes": channel - 7})
            self.assertTrue(all(states.values()))
            self.assertFalse(service.controller.pump_on)
            service.controller.clock = lambda: 161
            service.tick()
            self.assertEqual(set(service.controller.runs), {9, 10, 11, 12})
            self.assertEqual(sum(states.values()), 4)
            service.dispatch({"command": "rain_pause", "paused": True})
            self.assertFalse(any(states.values()))
            self.assertFalse(service.controller.runs)
            zones = list(service.controller.zones)
            zones[8] = replace(zones[8], rain_pause_exempt=True)
            service.controller.apply(zones, datetime.now())
            service.dispatch({"command": "continuous", "channel": 8})
            self.assertEqual(sum(states.values()), 1)
            service.dispatch({"command": "stop_all"})
            self.assertFalse(any(states.values()))
            self.assertFalse(any(self.primary.states))

    def test_missing_credentials_prevents_any_startup_writes(self) -> None:
        self.mapping["plugs"][0] = {"host": "192.168.0.50", "mac": MAC}
        self.write_mapping()
        with self.assertRaises(FileNotFoundError):
            Service(self.config, self.primary)
        self.assertFalse(self.primary.calls)

    def test_setup_assignments_are_locked_and_prepare_is_idempotent(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.argv", [
            "tapo_plugs.py", "--config", str(self.config), "prepare",
        ]):
            main()
        with self.config.with_name(self.config.name + ".service.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("sys.argv", [
                "tapo_plugs.py", "--config", str(self.config), "assign", "9", "192.168.0.50", MAC,
            ]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()
        self.assertTrue(all(plug is None for plug in load_mapping(mapping_path(self.config))))
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.argv", [
            "tapo_plugs.py", "--config", str(self.config), "assign", "9", "192.168.0.50", MAC,
        ]):
            main()
        self.assertEqual(load_mapping(mapping_path(self.config))[0].mac, MAC)
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.argv", [
            "tapo_plugs.py", "--config", str(self.config), "prepare",
        ]):
            main()
        self.assertEqual(load_mapping(mapping_path(self.config))[0].mac, MAC)
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.argv", [
            "tapo_plugs.py", "--config", str(self.config), "unassign", "9",
        ]):
            main()
        self.assertTrue(all(plug is None for plug in load_mapping(mapping_path(self.config))))
