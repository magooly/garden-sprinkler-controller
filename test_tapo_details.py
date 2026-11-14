"""Read-only telemetry and non-blocking cache tests; no hardware access."""

import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kasa import Credentials, KasaException, Module
from kasa.smart.smartdevice import SmartDevice

from tapo_details import TapoDetails, read_details
from tapo_plugs import Plug


PLUG = Plug("192.168.0.50", "12:34:56:78:9a:bc")
CREDENTIALS = Credentials("test@example.invalid", "private")


class ReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = MagicMock(spec=SmartDevice)
        self.device.mac = PLUG.mac
        self.device.model = "P110M"
        self.device.is_on = False
        self.device.update = AsyncMock()
        self.device.disconnect = AsyncMock()
        self.device.protocol = MagicMock()
        self.device.protocol.query = AsyncMock()
        self.device.modules = {}
        self.enterContext(patch("tapo_details.Discover.discover_single", AsyncMock(return_value=self.device)))

    async def test_missing_measurements_are_not_zero_and_never_switches(self):
        row = await read_details(PLUG, CREDENTIALS)
        self.assertIsNone(row["power_w"])
        self.assertFalse(row["state"])
        self.device.protocol.query.assert_not_awaited()
        self.device.disconnect.assert_awaited_once()

    async def test_energy_properties_keep_library_units(self):
        self.device.modules = {Module.Energy: MagicMock(
            current_consumption=123.4, consumption_today=0.25,
            consumption_this_month=2.5, voltage=240.1, current=0.52,
        )}
        row = await read_details(PLUG, CREDENTIALS)
        self.assertEqual([row[k] for k in ("power_w", "today_kwh", "month_kwh", "voltage_v", "current_a")],
                         [123.4, 0.25, 2.5, 240.1, 0.52])
        self.device.protocol.query.assert_not_awaited()

    async def test_identity_mismatch_never_reads_or_switches(self):
        self.device.mac = "00:00:00:00:00:01"
        with self.assertRaises(OSError):
            await read_details(PLUG, CREDENTIALS)
        self.device.update.assert_not_awaited()
        self.device.disconnect.assert_awaited_once()

    async def test_library_error_is_sanitized_and_connection_closed(self):
        self.device.update.side_effect = KasaException("private protocol data")
        with self.assertRaises(OSError) as caught:
            await read_details(PLUG, CREDENTIALS)
        self.assertNotIn("private", str(caught.exception))
        self.device.disconnect.assert_awaited_once()

    async def test_stalled_read_times_out_and_closes_without_switching(self):
        async def stall():
            await asyncio.sleep(10)
        self.device.update.side_effect = stall
        timeout = asyncio.timeout
        with patch("tapo_details.asyncio.timeout", side_effect=lambda seconds: timeout(min(seconds, 0.01))):
            with self.assertRaises(OSError):
                await read_details(PLUG, CREDENTIALS)
        self.device.protocol.query.assert_not_awaited()
        self.device.disconnect.assert_awaited_once()


class CacheTests(unittest.TestCase):
    def test_failed_refresh_is_nonblocking_coalesced_and_clears_current_readings(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        cache = TapoDetails((PLUG,), CREDENTIALS)
        cache.rows[0] = {
            "state": True, "power_w": 12.3, "today_kwh": 0.5, "month_kwh": 2.5,
            "voltage_v": 240.0, "current_a": 0.05,
            "updated": "2026-01-01T00:00:00+00:00",
        }

        async def slow(*_args):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            raise OSError("unavailable")

        original = cache._refresh
        def worker():
            try:
                original()
            finally:
                done.set()

        with patch("tapo_details.read_details", side_effect=slow) as read, patch.object(cache, "_refresh", worker):
            try:
                start = time.monotonic()
                result = cache.snapshot(["name"] * 16, True)
                self.assertLess(time.monotonic() - start, 0.2)
                self.assertTrue(result["reading"])
                self.assertTrue(entered.wait(1))
                cache.snapshot(["name"] * 16, True)
            finally:
                release.set()
                self.assertTrue(done.wait(2))
            self.assertEqual(read.call_count, 1)
            row = cache.snapshot(["name"] * 16)["rows"][0]
            for key in ("state", "power_w", "today_kwh", "month_kwh", "voltage_v", "current_a"):
                self.assertIsNone(row[key], key)
            self.assertEqual(row["updated"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(row["error"], "unavailable")
            cache.snapshot(["name"] * 16, True)
            self.assertEqual(read.call_count, 1)

    def test_no_assignments_returns_empty_table(self):
        self.assertEqual(TapoDetails((), None).snapshot([])["rows"], [])
