"""Off-peak requests, reconnection and fault isolation without real switching."""

import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from kasa import Credentials

from controller import default_zones, save_zones
from service import Service
from phone_server import PhoneRequestHandler
from tapo_plugs import HotWaterOutput, Plug, credentials_path, decode_mapping, mapping_path
from test_service import FakeDevice

MAC = "12:34:56:78:9a:bc"


class HotWaterWorkerTests(unittest.TestCase):
    def setUp(self):
        self.output = HotWaterOutput(
            Plug("192.168.0.121", MAC, True), Credentials("test@example.invalid", "test"),
        )
        self.events = []
        self.output.activity = lambda *event: self.events.append(event)

    def test_offline_request_is_unknown_not_confirmed_and_recovers(self):
        self.output.request(True)
        with patch("tapo_plugs.plug_operation", AsyncMock(side_effect=OSError("offline"))):
            with self.assertLogs(level="WARNING"):
                self.output._run()
        status = self.output.status()
        self.assertTrue(status["requested_state"])
        self.assertIsNone(status["state"])
        self.assertIsNone(status["last_confirmed_state"])
        self.assertTrue(status["pending"])
        self.assertIn("Unavailable", status["availability"])
        self.assertEqual(self.events[-1][2], "UNAVAILABLE")
        with patch("tapo_plugs.plug_operation", AsyncMock(side_effect=[False, True])) as operation:
            self.output._run()
        self.assertEqual([call.args[2] for call in operation.call_args_list], [None, True])
        self.assertTrue(self.output.status()["state"])
        self.assertFalse(self.output.status()["pending"])
        self.assertTrue(self.output.status()["confirmed_at"])
        self.assertEqual(self.events[-1][2], "ON")

    def test_expired_on_is_not_replayed(self):
        self.output.request(True)
        self.output.request(False)
        with patch("tapo_plugs.plug_operation", AsyncMock(return_value=False)) as operation:
            self.output._run()
        self.assertEqual([call.args[2] for call in operation.call_args_list], [None])
        self.assertFalse(self.output.status()["state"])

    def test_request_changes_during_read_use_latest_request(self):
        self.output.request(True)
        async def read(*args, **kwargs):
            if self.output.desired:
                self.output.request(False)
            return False
        with patch("tapo_plugs.plug_operation", AsyncMock(side_effect=read)) as operation:
            self.output._run()
        self.assertEqual([call.args[2] for call in operation.call_args_list], [None, None])
        self.assertFalse(self.output.status()["requested_state"])

    def test_request_changes_during_write_discovery_cancel_stale_write(self):
        self.output.request(True)
        async def operation(plug, credentials, state, *, permit_write=None):
            if state is not None:
                self.output.request(False)
                self.assertFalse(permit_write())
            return False
        with patch("tapo_plugs.plug_operation", AsyncMock(side_effect=operation)) as transport:
            self.output._run()
        self.assertEqual([call.args[2] for call in transport.call_args_list], [None, True, None])
        self.assertFalse(self.output.status()["state"])

    def test_inflight_on_is_followed_by_latest_off_and_shutdown_rejects_on(self):
        entered, release = threading.Event(), threading.Event()
        writes = []
        physical = False
        async def operation(plug, credentials, state, *, permit_write=None):
            nonlocal physical
            if state is True:
                entered.set()
                if not await asyncio.to_thread(release.wait, 2):
                    raise OSError("test release timed out")
            if state is not None:
                writes.append(state)
                physical = state
            return physical
        self.output.request(True)
        with patch("tapo_plugs.plug_operation", AsyncMock(side_effect=operation)):
            self.output.reconcile()
            try:
                self.assertTrue(entered.wait(2))
                self.output.request(False)
                release.set()
                self.output.shutdown()
            finally:
                release.set()
                self.output.worker.join(3)
        self.assertEqual(writes, [True, False])
        self.assertFalse(self.output.worker.is_alive())
        self.assertFalse(self.output.status()["state"])
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            self.output.request(True)

    def test_poll_cadence_single_worker_and_last_confirmation_survives_failure(self):
        with patch("tapo_plugs.plug_operation", AsyncMock(return_value=False)) as operation:
            self.output.reconcile()
            self.output.worker.join(2)
            self.assertFalse(self.output.worker.is_alive())
            self.output.reconcile()
            operation.assert_awaited_once()
            self.output.request(True)
            self.output.reconcile()
            self.output.worker.join(2)
            self.assertFalse(self.output.worker.is_alive())
        with patch("tapo_plugs.plug_operation", AsyncMock(side_effect=OSError("offline"))):
            with self.assertLogs(level="WARNING"):
                self.output._run()
        status = self.output.status()
        self.assertIsNone(status["state"])
        self.assertIsNotNone(status["last_confirmed_state"])
        self.assertIsNotNone(status["confirmed_at"])


class HotWaterServiceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = Path(directory) / "settings.json"
        zones = default_zones(16)
        zones[15] = replace(zones[15], name="Hot Water System", rain_pause_exempt=True)
        save_zones(self.config, zones)
        mapping_path(self.config).write_text(json.dumps({
            "version": 1, "plugs": [None] * 7 + [
                {"host": "192.168.0.121", "mac": MAC, "off_peak": True},
            ],
        }))
        credentials_path(self.config).write_text(json.dumps({"username": "test", "password": "private"}))
        credentials_path(self.config).chmod(0o600)
        self.operation = self.enterContext(patch("tapo_plugs.plug_operation", AsyncMock(side_effect=OSError("offline"))))
        self.primary = FakeDevice()
        self.service = Service(self.config, self.primary)
        self.output = self.service.relay.hot_water
        self.now = datetime.now()

    def test_startup_is_queued_off_no_network_or_global_fault(self):
        self.operation.assert_not_called()
        self.assertFalse(self.output.status()["requested_state"])
        self.assertIsNone(self.output.status()["state"])
        self.assertFalse(self.service.fault)
        self.assertEqual(self.service.relay.read_states()[-1], None)
        self.operation.assert_not_called()

    def test_offline_hot_water_does_not_stop_garden_or_pump(self):
        self.service.controller.start_manual(0, self.now)
        self.service.controller.start_manual(15, self.now)
        with self.assertLogs(level="WARNING"):
            self.output._run()
        self.assertTrue(self.primary.states[0])
        self.assertTrue(self.primary.states[7])
        self.assertFalse(self.service.fault)
        events = self.service.history.page(0, "15")["events"]
        self.assertIn("REQUESTED ON", [row["event"] for row in events])
        self.assertNotIn("ON", [row["event"] for row in events])
        self.service.controller.stop(15, self.now)
        self.assertFalse(self.output.status()["requested_state"])
        self.assertTrue(self.primary.states[0])

    def test_timer_expiry_while_offline_reconnects_off_only(self):
        clock = [0]
        self.service.controller.clock = lambda: clock[0]
        self.service.controller.start_manual(15, self.now, duration_minutes=1)
        clock[0] = 61
        self.service.controller.tick(self.now)
        self.assertNotIn(15, self.service.controller.runs)
        self.assertFalse(self.output.status()["requested_state"])
        self.operation.side_effect = None
        self.operation.return_value = False
        self.output._run()
        self.assertEqual([call.args[2] for call in self.operation.call_args_list], [None])

    def test_rain_pause_exemption_then_removal_requests_off(self):
        self.service.controller.start_manual(15, self.now)
        self.service.controller.set_rain_pause(True, self.now)
        self.assertTrue(self.output.status()["requested_state"])
        zones = list(self.service.controller.zones)
        zones[15] = replace(zones[15], rain_pause_exempt=False)
        self.service.controller.apply(zones, self.now)
        self.assertFalse(self.output.status()["requested_state"])

    def test_global_fault_and_restore_hold_clear_hot_water_request(self):
        self.service.controller.start_manual(15, self.now)
        with self.assertLogs(level="ERROR"):
            self.service.fail(OSError("garden relay offline"))
        self.assertFalse(self.output.status()["requested_state"])
        self.assertTrue(self.service.fault)
        self.service.controller.operation_held = True
        self.service.tick()
        self.assertFalse(self.output.status()["requested_state"])

    def test_off_request_precedes_other_blocking_or_failed_outputs(self):
        self.service.controller.start_manual(15, self.now)
        original = self.primary.set_relay
        def verify(channel, energized):
            self.assertFalse(self.output.status()["requested_state"])
            return original(channel, energized)
        with patch.object(self.primary, "set_relay", side_effect=verify):
            self.service.controller.stop_all(self.now)

    def test_primary_failure_stops_garden_but_preserves_hot_water(self):
        self.service.controller.start_manual(15, self.now)
        self.primary.fail = True
        with self.assertLogs(level="ERROR"):
            self.service.controller.start_manual(0, self.now)
        self.assertFalse(self.service.controller.closed)
        self.assertTrue(self.service.controller.garden_fault)
        self.assertTrue(self.output.status()["requested_state"])

    def test_details_and_timeline_expose_policy_and_not_confirmed_on(self):
        self.service.controller.start_manual(15, self.now)
        details = self.service.dispatch({"command": "tapo_details"})["rows"]
        row = next(row for row in details if row["channel"] == 15)
        self.assertTrue(row["off_peak"])
        self.assertTrue(row["requested_state"])
        self.assertIsNone(row["state"])
        result = self.service.dispatch({"command": "tapo_timeline", "channel": 15})
        self.assertTrue(result["off_peak"])
        self.assertEqual(result["segments"][-1]["state"], "Unknown")
        self.assertEqual(result["requested_segments"][-1]["state"], "ON")
        self.assertEqual(sum(row["seconds"] for row in result["segments"]), 86400)
        self.assertEqual(sum(row["seconds"] for row in result["requested_segments"]), 86400)
        public = PhoneRequestHandler._public_status(self.service.status())
        self.assertTrue(public["channels"][15]["tapo"]["off_peak"])
        self.assertTrue(public["channels"][15]["tapo"]["requested_state"])
        self.assertNotIn("private", json.dumps(public))

    def test_stop_with_no_run_still_requests_off(self):
        self.output.request(True)
        self.service.controller.stop(15, self.now)
        self.assertFalse(self.output.status()["requested_state"])

    def test_policy_is_explicit_device_bound_and_zone16_only(self):
        mapping = json.loads(mapping_path(self.config).read_text())
        record = mapping["plugs"][7]
        self.assertTrue(decode_mapping(mapping)[7].off_peak)
        mapping["plugs"][0], mapping["plugs"][7] = record, None
        with self.assertRaisesRegex(ValueError, "zone 16"):
            decode_mapping(mapping)
        mapping["plugs"] = [None] * 7 + [{**record, "off_peak": "true"}]
        with self.assertRaises(ValueError):
            decode_mapping(mapping)
