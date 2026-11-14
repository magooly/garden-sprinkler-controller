"""Independent fault groups and explicit appliance recovery; all hardware mocked."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from controller import default_zones, save_zones
from phone_server import PhoneRequestHandler
from service import Service
from service_client import ServiceClient
from tapo_plugs import credentials_path, mapping_path
from test_service import FakeDevice


class GroupIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(directory) / "settings.json"
        self.now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        zones = default_zones(16)
        zones = [replace(zone, enabled=False, rain_pause_exempt=True) for zone in zones]
        save_zones(self.config, zones)
        mapping_path(self.config).write_text(json.dumps({
            "version": 1, "plugs": [
                {"host": "192.168.0.50", "mac": "00:00:00:00:00:01"},
                {"host": "192.168.0.51", "mac": "00:00:00:00:00:02"},
                *([None] * 5),
                {"host": "192.168.0.121", "mac": "00:00:00:00:00:03", "off_peak": True},
            ],
        }))
        credentials_path(self.config).write_text(json.dumps({"username": "test", "password": "private"}))
        credentials_path(self.config).chmod(0o600)
        self.states = {}
        self.offline = set()
        async def operation(plug, credentials, state, **kwargs):
            if plug.host in self.offline:
                raise OSError("plug unavailable")
            if state is not None:
                self.states[plug.host] = state
            return self.states.get(plug.host, False)
        self.transport = self.enterContext(patch("tapo_plugs.plug_operation", AsyncMock(side_effect=operation)))
        self.primary = FakeDevice()
        self.service = Service(self.config, self.primary)
        self.controller = self.service.controller

    def start(self, channel):
        self.controller.start_manual(channel, self.now)

    def fault_plug(self, channel=8):
        self.offline.add("192.168.0.50" if channel == 8 else "192.168.0.51")

    def test_startup_offline_appliance_does_not_prevent_garden_or_other_appliances(self):
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            service = Service(self.config, self.primary)
        self.assertIn(8, service.controller.channel_faults)
        self.assertFalse(service.fault)
        self.assertFalse(service.controller.closed)
        service.controller.start_manual(0, self.now)
        service.controller.start_manual(9, self.now)
        self.assertTrue(self.primary.states[0])
        self.assertTrue(self.primary.states[7])
        self.assertTrue(self.states["192.168.0.51"])

    def test_startup_garden_failure_does_not_prevent_appliances(self):
        self.primary.fail = True
        with self.assertLogs(level="ERROR"):
            service = Service(self.config, self.primary)
        self.assertTrue(service.controller.garden_fault)
        self.assertFalse(service.fault)
        service.controller.start_manual(9, self.now)
        self.assertTrue(self.states["192.168.0.51"])

    def test_on_failure_never_writes_to_garden_or_other_appliances(self):
        self.start(0)
        self.start(9)
        before = list(self.primary.calls)
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.start(8)
        self.assertEqual(self.primary.calls, before)
        self.assertEqual(set(self.controller.runs), {0, 9})
        self.assertTrue(self.primary.states[7])
        self.assertTrue(self.states["192.168.0.51"])
        self.assertFalse(self.service.fault)
        self.assertIn(8, self.controller.channel_faults)
        self.assertIsNone(self.service.relay.states[8])

    def test_off_failure_preserves_garden_and_other_appliances(self):
        for channel in (0, 8, 9):
            self.start(channel)
        self.fault_plug()
        before = list(self.primary.calls)
        with self.assertLogs(level="ERROR"):
            self.controller.stop(8, self.now)
        self.assertEqual(self.primary.calls, before)
        self.assertEqual(set(self.controller.runs), {0, 9})
        self.assertIn(8, self.controller.channel_faults)
        self.assertTrue(self.states["192.168.0.50"])  # Unreachable OFF cannot be guaranteed.

    def test_expiry_failure_does_not_stop_garden_timer_processing(self):
        clock = [0]
        self.controller.clock = lambda: clock[0]
        self.controller.start_manual(8, self.now, duration_minutes=1)
        self.controller.start_manual(0, self.now, duration_minutes=2)
        self.fault_plug()
        clock[0] = 61
        with self.assertLogs(level="ERROR"):
            self.controller.tick(self.now)
        self.assertTrue(self.primary.states[0])
        clock[0] = 121
        self.controller.tick(self.now)
        self.assertFalse(self.primary.states[0])
        self.assertFalse(self.primary.states[7])
        self.assertFalse(self.service.fault)

    def test_reconnect_does_not_retry_until_explicit_retry_and_retry_only_confirms_off(self):
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.start(8)
        self.offline.clear()
        self.transport.reset_mock()
        self.controller.tick(self.now)
        self.transport.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Retry station"):
            self.start(8)
        self.controller.retry_station(8, self.now)
        self.assertNotIn(8, self.controller.channel_faults)
        self.assertFalse(self.states["192.168.0.50"])
        self.assertNotIn(8, self.controller.runs)
        self.assertEqual([call.args[2] for call in self.transport.call_args_list], [False])
        self.start(8)
        self.assertTrue(self.states["192.168.0.50"])

    def test_failed_retry_keeps_fault_and_does_not_touch_garden(self):
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.start(8)
            self.controller.retry_station(8, self.now)
        self.assertIn(8, self.controller.channel_faults)
        self.start(0)
        self.assertTrue(self.primary.states[0])

    def test_retry_does_not_resume_a_missed_schedule_but_next_window_can_run(self):
        zones = list(self.controller.zones)
        zones[8] = replace(zones[8], enabled=True, schedule=(("0000", "2359") + ("",) * 6,) * 7)
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.controller.apply(zones, self.now)
        self.offline.clear()
        self.controller.retry_station(8, self.now)
        self.controller.tick(self.now)
        self.assertNotIn(8, self.controller.runs)
        self.controller.tick(self.now + timedelta(days=1))
        self.assertIn(8, self.controller.runs)

    def test_read_failure_is_local_and_successful_read_does_not_clear_latched_fault(self):
        self.start(0)
        self.start(9)
        self.fault_plug()
        before = list(self.primary.calls)
        with self.assertLogs(level="ERROR"):
            result = self.service.dispatch({"command": "read_states"})
        self.assertIsNone(result["states"][8])
        self.assertEqual(self.primary.calls, before)
        self.assertEqual(set(self.controller.runs), {0, 9})
        self.offline.clear()
        self.service.dispatch({"command": "read_states"})
        self.assertIn(8, self.controller.channel_faults)

    def test_garden_on_failure_stops_garden_only_and_latches_until_restart(self):
        self.start(1)
        self.start(9)
        self.start(15)
        original = self.primary.set_relay
        def fail_on(channel, state):
            if channel == 0 and state:
                raise OSError("garden ON failed")
            original(channel, state)
        with patch.object(self.primary, "set_relay", side_effect=fail_on), self.assertLogs(level="ERROR"):
            self.start(0)
        self.assertTrue(self.controller.garden_fault)
        self.assertFalse(any(self.primary.states))
        self.assertEqual(set(self.controller.runs), {9, 15})
        self.assertTrue(self.service.relay.hot_water.status()["requested_state"])
        self.assertFalse(self.service.fault)
        with self.assertRaisesRegex(ValueError, "Garden group"):
            self.start(2)
        self.start(8)
        self.assertIn(8, self.controller.runs)

    def test_garden_pump_and_off_failures_do_not_stop_appliances(self):
        self.start(9)
        original = self.primary.set_relay
        def fail_pump(channel, state):
            if channel == 7 and state:
                raise OSError("pump ON failed")
            original(channel, state)
        with patch.object(self.primary, "set_relay", side_effect=fail_pump), self.assertLogs(level="ERROR"):
            self.start(0)
        self.assertTrue(self.controller.garden_fault)
        self.assertFalse(self.primary.states[0])
        self.assertTrue(self.states["192.168.0.51"])
        self.assertIn(9, self.controller.runs)

    def test_garden_read_failure_preserves_appliances(self):
        self.start(9)
        self.primary.fail = True
        with self.assertLogs(level="ERROR"):
            result = self.service.dispatch({"command": "read_states"})
        self.assertTrue(result["garden_fault"])
        self.assertFalse(result["fault"])
        self.assertIn(9, self.controller.runs)
        self.assertTrue(self.states["192.168.0.51"])

    def test_garden_valve_off_failure_preserves_appliance_run(self):
        for channel in (0, 9):
            self.start(channel)
        original = self.primary.set_relay
        def fail_off(channel, state):
            if channel == 0 and not state:
                raise OSError("garden OFF failed")
            original(channel, state)
        with patch.object(self.primary, "set_relay", side_effect=fail_off), self.assertLogs(level="ERROR"):
            self.controller.stop(0, self.now)
        self.assertTrue(self.controller.garden_fault)
        self.assertEqual(set(self.controller.runs), {9})
        self.assertFalse(self.primary.states[7])
        self.assertTrue(self.states["192.168.0.51"])

    def test_explicit_group_stop_suppresses_only_its_active_schedule_windows(self):
        zones = list(self.controller.zones)
        for channel in (0, 8):
            zones[channel] = replace(zones[channel], enabled=True, schedule=(("0000", "2359") + ("",) * 6,) * 7)
        self.controller.apply(zones, self.now)
        self.controller.stop_group(8, self.now)
        self.controller.tick(self.now)
        self.assertEqual(set(self.controller.runs), {0})
        self.assertNotIn(0, self.controller.blocked)
        self.assertIn(8, self.controller.blocked)

    def test_shared_storage_fault_and_restore_hold_still_request_all_off(self):
        for channel in (0, 8, 15):
            self.start(channel)
        with self.assertLogs(level="ERROR"):
            self.service.fail(OSError("settings storage failed"))
        self.assertTrue(self.service.fault)
        self.assertTrue(self.controller.closed)
        self.assertFalse(self.controller.runs)
        self.assertFalse(any(self.primary.states))
        self.assertFalse(self.states["192.168.0.50"])
        self.assertFalse(self.service.relay.hot_water.status()["requested_state"])

    def test_stop_garden_leaves_appliances_and_hot_water_requested_on(self):
        for channel in (0, 9, 15):
            self.start(channel)
        self.service.dispatch({"command": "stop_garden"})
        self.assertEqual(set(self.controller.runs), {9, 15})
        self.assertFalse(any(self.primary.states))
        self.assertTrue(self.states["192.168.0.51"])
        self.assertTrue(self.service.relay.hot_water.status()["requested_state"])

    def test_stop_appliances_leaves_garden_pump_and_timer_unchanged(self):
        for channel in (0, 8, 9, 15):
            self.start(channel)
        run = self.controller.runs[0]
        before = list(self.primary.calls)
        self.service.dispatch({"command": "stop_appliances"})
        self.assertEqual(self.primary.calls, before)
        self.assertEqual(self.controller.runs, {0: run})
        self.assertTrue(self.primary.states[7])
        self.assertFalse(any(self.states.values()))
        self.assertFalse(self.service.relay.hot_water.status()["requested_state"])

    def test_stop_everything_attempts_all_outputs_even_when_one_is_unreachable(self):
        for channel in (0, 8, 9, 15):
            self.start(channel)
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.service.dispatch({"command": "stop_all"})
        self.assertFalse(self.controller.runs)
        self.assertFalse(any(self.primary.states))
        self.assertFalse(self.states["192.168.0.51"])
        self.assertFalse(self.service.relay.hot_water.status()["requested_state"])
        self.assertIn(8, self.controller.channel_faults)

    def test_rain_pause_preserves_exempt_garden_if_an_appliance_off_fails(self):
        zones = list(self.controller.zones)
        zones[8] = replace(zones[8], rain_pause_exempt=False)
        self.controller.apply(zones, self.now)
        self.start(0)
        self.start(8)
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.controller.set_rain_pause(True, self.now)
        self.assertIn(0, self.controller.runs)
        self.assertTrue(self.primary.states[7])
        self.assertFalse(self.service.fault)

    def test_public_status_and_client_surface_fault_without_global_failure(self):
        self.fault_plug()
        with self.assertLogs(level="ERROR"):
            self.start(8)
        data = self.service.status()
        public = PhoneRequestHandler._public_status(data)
        self.assertFalse(public["fault"])
        self.assertFalse(public["channels"][0]["fault"])
        self.assertTrue(public["channels"][8]["fault"])
        self.assertTrue(public["group_controls_supported"])
        with patch("service_client.exchange", return_value=data):
            client = ServiceClient(Path("/unused"))
            self.assertFalse(client.fault)
            with self.assertRaisesRegex(ValueError, "other stations"):
                client.start_manual(8, self.now)
        self.assertTrue(client.channel_faults[8])

    def test_secondary_board_read_fault_is_not_a_garden_fault(self):
        mapping_path(self.config).unlink()
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        service = Service(self.config, self.primary, secondary)
        service.controller.start_manual(0, self.now)
        secondary.fail = True
        with self.assertLogs(level="ERROR"):
            result = service.dispatch({"command": "read_states"})
        self.assertEqual(result["states"][8:], (None,) * 8)
        self.assertFalse(service.controller.garden_fault)
        self.assertEqual(set(service.controller.channel_faults), set(range(8, 16)))
        self.assertTrue(self.primary.states[7])
