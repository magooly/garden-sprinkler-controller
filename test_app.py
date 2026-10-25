"""GUI rain-pause and pump checks with all physical relay writes mocked."""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk

from app import GREY, RUNNING_RED, SCHEDULED_GREEN, SprinklerApp
from controller import (
    Zone, default_zones, load_rain_pause, load_zones, rain_pause_path, save_zones,
)
from test_controller import schedule_for
from waveshare_relay import WaveshareRelays
from service_client import ServiceClient
from test_service import RunningService


@unittest.skipUnless(os.environ.get("DISPLAY"), "Requires a graphical display")
class RainPauseGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        original_destroy = SprinklerApp.destroy

        def destroy(app: SprinklerApp) -> None:
            # CustomTkinter leaves internal callbacks queued when a test root closes.
            for timer in app.tk.call("after", "info"):
                app.after_cancel(timer)
            original_destroy(app)

        self.enterContext(patch.object(SprinklerApp, "destroy", new=destroy))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"
        zones = default_zones()
        zones[0] = Zone("Lawn", True, schedule_for((0,)))
        save_zones(self.path, zones)
        self.write = self.enterContext(patch.object(WaveshareRelays, "set_relay"))
        self.time = self.enterContext(patch("app.datetime"))
        self.time.now.return_value = datetime(2026, 10, 26, 6, 0)
        self.app = SprinklerApp(self.path, WaveshareRelays())
        self.addCleanup(self.close_app)

    def close_app(self) -> None:
        self.app.dirty = False
        self.app.on_close()

    def pause(self) -> None:
        self.app.rain_pause_var.set(True)
        self.app.toggle_rain_pause()

    def test_scheduled_dot_is_bright_green_with_watering_red_and_pause_grey(self) -> None:
        self.assertEqual(self.app.zone_dots[0].cget("text_color"), RUNNING_RED)
        self.app.controller.stop(0, self.time.now.return_value)
        self.app._refresh()
        self.assertEqual(SCHEDULED_GREEN, "#20ff20")
        self.assertEqual(self.app.zone_dots[0].cget("text_color"), SCHEDULED_GREEN)
        self.assertEqual(self.app.zone_dots[1].cget("text_color"), GREY)
        self.assertEqual(self.app.zone_dots[7].cget("text_color"), GREY)
        self.pause()
        self.assertEqual(self.app.zone_dots[0].cget("text_color"), GREY)

    def test_pause_disables_controls_and_preserves_unsaved_edits(self) -> None:
        self.app.fields[0].name.set("Unsaved name")
        self.pause()
        self.assertTrue(load_rain_pause(self.path))
        self.assertTrue(self.app.dirty)
        self.assertEqual(load_zones(self.path)[0].name, "Lawn")
        self.assertFalse(self.app.controller.runs)
        self.assertEqual(self.app.manual_button.cget("state"), "disabled")
        self.assertEqual(self.app.test_button.cget("state"), "disabled")
        self.assertIn("RAIN PAUSE", self.app.system_status.cget("text"))
        self.app.geometry("1100x760")
        self.app.update()
        switch = self.app.rain_pause_switch
        self.assertLessEqual(switch.winfo_rooty() + switch.winfo_height(),
                             self.app.winfo_rooty() + self.app.winfo_height())
        self.assertTrue(self.app.save())
        self.assertTrue(load_rain_pause(self.path))
        self.assertFalse(self.app.controller.runs)

    def test_resume_confirmation_can_cancel_or_start_active_routine(self) -> None:
        self.pause()
        for accepted in (False, True):
            with self.subTest(accepted=accepted), patch(
                "app.messagebox.askyesno", return_value=accepted
            ) as confirm:
                self.app.rain_pause_var.set(False)
                self.app.toggle_rain_pause()
                confirm.assert_called_once()
                self.assertEqual(load_rain_pause(self.path), not accepted)
                self.assertEqual(self.app.controller.rain_paused, not accepted)
                self.assertEqual(bool(self.app.controller.runs), accepted)

    def test_restart_restores_pause_before_initial_tick(self) -> None:
        self.pause()
        other = SprinklerApp(self.path, WaveshareRelays())
        try:
            self.assertTrue(other.controller.rain_paused)
            self.assertFalse(other.controller.runs)
            self.assertEqual(other.manual_button.cget("state"), "disabled")
        finally:
            other.on_close()

    def test_persistence_failure_stops_watering_and_reports_fault(self) -> None:
        with patch("app.save_rain_pause", side_effect=OSError("Disk full")), patch(
            "app.messagebox.showerror"
        ) as error, self.assertLogs(level="ERROR"):
            self.pause()
        error.assert_called_once()
        self.assertTrue(self.app.faulted)
        self.assertTrue(self.app.controller.closed)
        self.assertFalse(self.app.controller.runs)
        self.assertTrue(self.app.controller.rain_paused)

    def test_relay_failure_retains_saved_pause_for_restart(self) -> None:
        with patch.object(
            WaveshareRelays, "set_relay", side_effect=OSError("Disconnected")
        ) as write, patch("app.messagebox.showerror") as error, self.assertLogs(level="ERROR"):
            self.pause()
        self.assertEqual(write.call_count, 16)
        error.assert_called_once()
        self.assertTrue(self.app.faulted)
        self.assertTrue(load_rain_pause(self.path))

    def test_resume_save_failure_leaves_pause_enabled(self) -> None:
        self.pause()
        with patch("app.save_rain_pause", side_effect=OSError("Disk full")), patch(
            "app.messagebox.askyesno", return_value=True
        ), patch("app.messagebox.showerror") as error, self.assertLogs(level="ERROR"):
            self.app.rain_pause_var.set(False)
            self.app.toggle_rain_pause()
        error.assert_called_once()
        self.assertTrue(self.app.controller.rain_paused)
        self.assertTrue(load_rain_pause(self.path))
        self.assertFalse(self.app.controller.runs)

    def test_invalid_pause_file_prevents_startup_before_hardware_access(self) -> None:
        rain_pause_path(self.path).write_text('{"rain_paused": "false"}')
        self.write.reset_mock()
        with patch("app.messagebox.showerror") as error:
            with self.assertRaises(SystemExit):
                SprinklerApp(self.path, WaveshareRelays())
        error.assert_called_once()
        self.write.assert_not_called()

    def test_master_pump_panel_has_no_schedule_or_independent_controls(self) -> None:
        self.app.select_zone(7)
        self.app.update()
        self.assertEqual(self.app.zone_buttons[7].cget("text"), "8  Master Pump")
        self.assertIn("MASTER PUMP / ON", self.app.status.cget("text"))
        self.assertEqual(self.app.manual_button.cget("state"), "disabled")
        self.assertEqual(self.app.test_button.cget("state"), "disabled")
        self.assertEqual(len(self.app.editor.winfo_children()), 3)
        original = load_zones(self.path)[7]
        self.assertTrue(self.app.save())
        self.assertEqual(load_zones(self.path)[7], original)
        self.app.select_zone(0)
        self.assertEqual(self.app.manual_button.cget("state"), "normal")
        self.pause()
        self.assertFalse(self.app.controller.pump_on)
        self.app.select_zone(7)
        self.assertIn("RAIN PAUSE", self.app.status.cget("text"))

    def test_none_setting_restores_channel_eight_editor(self) -> None:
        with patch("controller.MASTER_PUMP_CHANNEL", None):
            other = SprinklerApp(self.path, WaveshareRelays())
        try:
            other.select_zone(7)
            self.assertNotIn("Master Pump", other.zone_buttons[7].cget("text"))
            self.assertGreater(len(other.editor.winfo_children()), 3)
            self.assertEqual(other.manual_button.cget("state"), "normal")
            self.assertEqual(other.test_button.cget("state"), "normal")
        finally:
            other.on_close()

    def test_compact_layout_keeps_actions_visible_at_opening_and_minimum_sizes(self) -> None:
        for size in ("1180x760", "1100x620"):
            with self.subTest(size=size):
                self.app.geometry(size)
                self.app.update()
                for widget in (
                    self.app.manual_button, self.app.test_button,
                    self.app.stop_all_button, self.app.save_button,
                    self.app.rain_pause_switch, *self.app.zone_buttons,
                ):
                    self.assertGreaterEqual(widget.winfo_rooty(), self.app.winfo_rooty())
                    self.assertLessEqual(
                        widget.winfo_rooty() + widget.winfo_height(),
                        self.app.winfo_rooty() + self.app.winfo_height(),
                    )
                    self.assertLessEqual(
                        widget.winfo_rootx() + widget.winfo_width(),
                        self.app.winfo_rootx() + self.app.winfo_width(),
                    )
                self.assertEqual(
                    self.app.manual_button.winfo_rooty(), self.app.test_button.winfo_rooty()
                )
                self.assertLess(
                    self.app.editor.master.winfo_rooty() + self.app.editor.master.winfo_height(),
                    self.app.manual_button.winfo_rooty(),
                )
                table = next(
                    widget for widget in self.app.editor.winfo_children()
                    if int(widget.grid_info()["row"]) == 5
                )
                entries = [
                    widget for widget in table.winfo_children()
                    if isinstance(widget, ctk.CTkEntry)
                ]
                self.assertEqual(len(entries), 56)
                for entry in entries:
                    self.assertLessEqual(
                        entry.winfo_rootx() + entry.winfo_width(),
                        self.app.winfo_rootx() + self.app.winfo_width(),
                    )
                    if size == "1180x760":
                        self.assertLessEqual(
                            entry.winfo_rooty() + entry.winfo_height(),
                            self.app.editor.master.winfo_rooty()
                            + self.app.editor.master.winfo_height(),
                        )
                if size == "1180x760":
                    self.assertLessEqual(
                        self.app.validation.winfo_rooty() + self.app.validation.winfo_height(),
                        self.app.editor.master.winfo_rooty()
                        + self.app.editor.master.winfo_height(),
                    )
                viewport = self.app.editor.master
                viewport.tk.call(str(viewport), "yview", "moveto", 1)
                self.app.update()
                self.assertLessEqual(
                    self.app.validation.winfo_rooty() + self.app.validation.winfo_height(),
                    viewport.winfo_rooty() + viewport.winfo_height(),
                )

    def test_service_desktop_controls_and_close_do_not_reset_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            client = ServiceClient(running.path)
            calls = len(running.device.calls)
            other = SprinklerApp(running.config, client)
            try:
                self.assertEqual(len(running.device.calls), calls)
                other.manual()
                self.assertTrue(running.device.states[0])
                self.assertTrue(running.device.states[7])
                other.fields[0].name.set("Service garden")
                self.assertTrue(other.save())
                self.assertEqual(load_zones(running.config)[0].name, "Service garden")
                other.rain_pause_var.set(True)
                other.toggle_rain_pause()
                self.assertTrue(load_rain_pause(running.config))
                self.assertFalse(any(running.device.states))
                with patch("app.messagebox.askyesno", return_value=True):
                    other.rain_pause_var.set(False)
                    other.toggle_rain_pause()
                other.manual()
                calls = len(running.device.calls)
            finally:
                other.dirty = False
                other.on_close()
            self.assertEqual(len(running.device.calls), calls)
            self.assertTrue(running.device.states[0])
            self.assertTrue(running.device.states[7])

    def test_service_desktop_sync_preserves_edits_and_rejects_stale_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                other.fields[0].name.set("Unsaved")
                second = ServiceClient(running.path)
                zones = default_zones()
                zones[0] = Zone("Changed elsewhere")
                second.apply(zones, datetime.now())
                if other.timer:
                    other.after_cancel(other.timer)
                other._tick()
                self.assertEqual(other.fields[0].name.get(), "Unsaved")
                with patch("app.messagebox.showerror") as error:
                    self.assertFalse(other.save())
                error.assert_called_once()
                self.assertEqual(load_zones(running.config)[0].name, "Changed elsewhere")
                other.dirty = False
                if other.timer:
                    other.after_cancel(other.timer)
                other._tick()
                self.assertEqual(other.fields[0].name.get(), "Changed elsewhere")
            finally:
                other.dirty = False
                other.on_close()

    def test_service_disconnect_disables_controls_and_reconnects(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                with patch.object(other.controller, "sync", side_effect=OSError("Disconnected")), self.assertLogs(level="ERROR"):
                    if other.timer:
                        other.after_cancel(other.timer)
                    other._tick()
                self.assertTrue(other.faulted)
                self.assertEqual(other.manual_button.cget("state"), "disabled")
                self.assertIn("UNAVAILABLE", other.system_status.cget("text"))
                if other.timer:
                    other.after_cancel(other.timer)
                other._tick()
                self.assertFalse(other.faulted)
                self.assertEqual(other.manual_button.cget("state"), "normal")
            finally:
                other.on_close()
