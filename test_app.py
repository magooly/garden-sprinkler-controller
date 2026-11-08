"""GUI rain-pause and pump checks with all physical relay writes mocked."""

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk

from app import GREY, RUNNING_RED, SCHEDULED_GREEN, SprinklerApp, display_time, overlap_notices
from controller import (
    Zone, default_zones, load_rain_pause, load_zones, rain_pause_path, save_zones,
)
from test_controller import schedule_for
from waveshare_relay import WaveshareRelays
from service_client import ServiceClient
from test_service import FakeDevice, RunningService


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
        self.app.AUTOSAVE_DELAY_MS = 60_000
        self.addCleanup(self.close_app)

    def close_app(self) -> None:
        self.app.dirty = False
        self.app.on_close()

    def pause(self) -> None:
        self.app.rain_pause_var.set(True)
        self.app.toggle_rain_pause()

    def test_weekly_overview_all_days_periods_warnings_and_navigation(self) -> None:
        for day in range(7):
            for slot, value in enumerate(("0600", "0620", "1000", "1020", "1400", "1420", "1800", "1820")):
                self.app.fields[0].schedule[day][slot].set(value)
        self.app.fields[1].enabled.set(True)
        self.app.fields[1].schedule[0][0].set("0610")
        self.app.fields[1].schedule[0][1].set("0630")
        self.app.show_overview()
        self.app.show_weekly_overview()
        weekly = self.app.weekly_overview
        try:
            self.app.update()
            self.assertEqual(self.app.overview.title(), "Daily Schedule")
            self.assertEqual(weekly.title(), "Weekly Overview")
            self.assertEqual(len(weekly.cells), 49)
            for cell in weekly.cells.values():
                self.assertLessEqual(
                    cell.winfo_rootx() + cell.winfo_width(),
                    weekly.winfo_rootx() + weekly.winfo_width(),
                )
            self.assertIn("UNSAVED EDITS", weekly.summary.cget("text"))
            for day in range(7):
                text = weekly.cells[0, day].cget("text")
                self.assertIn("1: 06:00-06:20", text)
                self.assertIn("4: 18:00-18:20", text)
            self.assertEqual(weekly.cells[0, 0].cget("fg_color"), "#51432d")
            self.assertEqual(weekly.cells[1, 0].cget("fg_color"), "#51432d")
            self.assertEqual(weekly.cells[0, 0].cget("text_color"), "#ff2020")
            self.assertEqual(weekly.cells[1, 0].cget("text_color"), "#ff2020")
            self.assertEqual(weekly.cells[0, 1].cget("fg_color"), "#242b34")
            self.assertNotIn((7, 0), weekly.cells)
            weekly.open_channel(1)
            self.assertEqual(self.app.selected, 1)
            self.app.fields[1].schedule[0][1].set("")
            self.assertEqual(weekly.cells[1, 0].cget("fg_color"), "#503238")
            weekly.hide_disabled.set(True)
            weekly.refresh()
            self.assertEqual(len(weekly.cells), 14)
            self.app.show_weekly_overview()
            self.assertIs(self.app.weekly_overview, weekly)
            with patch("app.messagebox.showerror"):
                self.pause()
            self.assertFalse(self.app.controller.rain_paused)
            self.app.fields[1].schedule[0][1].set("0630")
            self.pause()
            self.assertIn("RAIN PAUSE", weekly.summary.cget("text"))
        finally:
            weekly.destroy()
            self.app.overview.destroy()
        self.app.show_weekly_overview()
        self.assertIsNot(self.app.weekly_overview, weekly)
        self.app.weekly_overview.destroy()

    def test_schedule_overview_unsaved_edits_filters_navigation_and_refresh(self) -> None:
        self.app.fields[1].schedule[0][0].set("0600")
        self.app.fields[1].schedule[0][1].set("0610")
        self.app.fields[1].enabled.set(True)
        self.app.show_overview()
        overview = self.app.overview
        self.assertIsNotNone(overview)
        try:
            self.app.update()
            self.assertIn("UNSAVED EDITS", overview.summary.cget("text"))
            self.assertEqual(len(overview.table.get_children()), 2)
            self.assertTrue(all("Same start" in row.warning for row in overview.rows))
            overview.table.selection_set("1")
            overview.open_selected()
            self.assertEqual(self.app.selected, 1)
            self.app.fields[1].enabled.set(False)
            self.assertEqual(overview.rows[0].warning, "")
            overview.hide_disabled.set(True)
            overview.refresh()
            self.assertEqual(len(overview.rows), 1)
            overview.day.set("Tue")
            overview.refresh()
            self.assertEqual(len(overview.rows), 0)
            self.app.show_overview()
            self.assertIs(self.app.overview, overview)
            self.pause()
            self.assertIn("RAIN PAUSE", overview.summary.cget("text"))
        finally:
            overview.destroy()
        self.app.show_overview()
        self.assertIsNot(self.app.overview, overview)
        self.app.overview.destroy()

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

    def test_pause_saves_pending_edits_before_changing_pause_state(self) -> None:
        self.app.fields[0].name.set("Unsaved name")
        self.pause()
        self.assertTrue(load_rain_pause(self.path))
        self.assertFalse(self.app.dirty)
        self.assertEqual(load_zones(self.path)[0].name, "Unsaved name")
        self.assertFalse(self.app.controller.runs)
        self.assertEqual(self.app.manual_button.cget("state"), "disabled")
        self.assertIn("RAIN PAUSE", self.app.system_status.cget("text"))
        self.app.geometry("1100x760")
        self.app.update()
        switch = self.app.rain_pause_switch
        self.assertLessEqual(switch.winfo_rooty() + switch.winfo_height(),
                             self.app.winfo_rooty() + self.app.winfo_height())
        self.assertTrue(self.app.save())
        self.assertTrue(load_rain_pause(self.path))
        self.assertFalse(self.app.controller.runs)

    def test_valid_changes_are_debounced_and_autosaved(self) -> None:
        self.app.AUTOSAVE_DELAY_MS = 20
        self.app.fields[1].name.set("Auto-saved zone")
        self.assertTrue(self.app.dirty)
        self.assertIsNotNone(self.app.autosave_timer)
        deadline = time.monotonic() + 2
        while self.app.dirty and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.dirty)
        self.assertEqual(load_zones(self.path)[1].name, "Auto-saved zone")
        self.assertIn("Auto-saved", self.app.system_status.cget("text"))

    def test_second_bank_is_disabled_until_a_second_board_is_configured(self) -> None:
        self.app.board_selector.set("Channels 9-16")
        self.app.show_board("Channels 9-16")
        self.app.update()
        for channel in range(8, 16):
            self.assertEqual(self.app.zone_buttons[channel].cget("state"), "disabled")
            self.assertIn("not configured", self.app.zone_buttons[channel].cget("text"))
            self.assertEqual(self.app.rain_pause_exempt_buttons[channel].cget("state"), "disabled")
        self.assertNotEqual(self.app.zone_buttons[0].cget("state"), "disabled")
        self.app.select_zone(8)
        self.assertEqual(self.app.selected, 0)

    def test_second_board_desktop_save_manual_and_overviews(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory), secondary) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                other.select_zone(15)
                self.assertEqual(other.selected, 15)
                self.assertEqual(other.board_selector.get(), "Channels 9-16")
                other.fields[15].name.set("Second board end")
                self.assertTrue(other.save())
                self.assertEqual(load_zones(running.config)[15].name, "Second board end")
                other.manual()
                self.assertTrue(secondary.states[7])
                self.assertFalse(running.device.states[7])
                other.show_weekly_overview()
                self.assertEqual(len(other.weekly_overview.cells), 105)
            finally:
                other.dirty = False
                other.on_close()

    def test_invalid_changes_are_not_autosaved(self) -> None:
        original = load_zones(self.path)
        self.app.fields[0].schedule[0][0].set("")
        self.assertTrue(self.app.dirty)
        self.assertIsNone(self.app.autosave_timer)
        time.sleep(0.05)
        self.app.update()
        self.assertTrue(self.app.dirty)
        self.assertEqual(load_zones(self.path), original)
        self.assertIn("invalid settings not saved", self.app.system_status.cget("text"))

    def test_profiles_save_load_confirm_and_restore_previous_timetable(self) -> None:
        self.assertTrue(self.app.save_profile("Summer"))
        self.app.fields[0].enabled.set(False)
        self.assertTrue(self.app.save())
        self.assertIn("Summer (modified)", self.app.system_status.cget("text"))
        self.assertTrue(self.app.save_profile("Winter"))
        with patch("app.messagebox.askyesno", return_value=False):
            self.assertFalse(self.app.load_profile("Summer"))
        self.assertFalse(self.app.controller.zones[0].enabled)
        with patch("app.messagebox.askyesno", return_value=True):
            self.assertTrue(self.app.load_profile("Summer"))
        self.assertTrue(self.app.fields[0].enabled.get())
        self.assertTrue(load_zones(self.path)[0].enabled)
        self.assertFalse(self.app.dirty)
        with patch("app.messagebox.askyesno", return_value=True):
            self.assertTrue(self.app.load_profile("Before last load"))
        self.assertFalse(self.app.fields[0].enabled.get())

    def test_service_profiles_load_and_sync_all_schedule_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                self.assertTrue(other.save_profile("Winter"))
                other.fields[0].enabled.set(True)
                other.fields[0].schedule[0][0].set("0600")
                other.fields[0].schedule[0][1].set("0620")
                other.fields[0].paused_windows[1].set(True)
                other.fields[0].rain_pause_exempt.set(True)
                other.fields[0].pump_exempt.set(True)
                self.assertTrue(other.save_profile("Summer"))
                with patch("app.messagebox.askyesno", return_value=True):
                    self.assertTrue(other.load_profile("Winter"))
                self.assertFalse(other.fields[0].enabled.get())
                self.assertEqual(other.fields[0].schedule[0][0].get(), "")
                self.assertFalse(other.fields[0].paused_windows[1].get())
                self.assertTrue(other.fields[0].rain_pause_exempt.get())
                self.assertTrue(other.fields[0].pump_exempt.get())
                self.assertEqual(ServiceClient(running.path).timetables["selected"], "Winter")
            finally:
                other.dirty = False
                other.on_close()

    def test_profile_autosave_preserves_named_snapshot_and_ignores_name_edits(self) -> None:
        self.app.AUTOSAVE_DELAY_MS = 20
        self.assertTrue(self.app.save_profile("Summer"))
        self.app.fields[0].name.set("Renamed")
        self.app.fields[0].pump_exempt.set(True)
        self.assertFalse(self.app.timetable_status()["modified"])
        self.app.fields[0].enabled.set(False)
        self.assertTrue(self.app.timetable_status()["modified"])
        time.sleep(0.03)
        self.app.update()
        self.assertFalse(self.app.dirty)
        with patch("app.messagebox.askyesno", return_value=True):
            self.assertTrue(self.app.load_profile("Summer"))
        self.assertTrue(self.app.fields[0].enabled.get())
        self.assertEqual(self.app.fields[0].name.get(), "Renamed")
        self.assertTrue(self.app.fields[0].pump_exempt.get())

    def test_invalid_profile_actions_and_cancelled_overwrite_do_not_save_edits(self) -> None:
        self.assertTrue(self.app.save_profile("Summer"))
        self.app.fields[0].name.set("Pending")
        with patch("app.messagebox.showerror") as error:
            for name in (" ", "x" * 41, "Before last load"):
                self.assertFalse(self.app.save_profile(name))
            self.assertEqual(error.call_count, 3)
        with patch("app.messagebox.askyesno", return_value=False):
            self.assertFalse(self.app.save_profile("Summer"))
        self.assertEqual(load_zones(self.path)[0].name, "Lawn")
        self.app.fields[0].schedule[0][1].set("")
        with patch("app.messagebox.showerror"), patch("app.messagebox.askyesno", return_value=True):
            self.assertFalse(self.app.save_profile("Winter"))
            self.assertFalse(self.app.load_profile("Summer"))
        self.assertEqual(self.app.timetable_status()["names"], ["Summer"])

    def test_profile_dialog_reuses_window_and_refreshes_pending_changes(self) -> None:
        self.assertTrue(self.app.save_profile("Summer"))
        self.app.show_profiles()
        window = self.app.profile_window
        self.app.show_profiles()
        self.assertIs(self.app.profile_window, window)
        self.app.fields[0].enabled.set(False)
        labels = [widget for widget in window.winfo_children() if isinstance(widget, ctk.CTkLabel)]
        self.assertIn("Selected: Summer (modified)", [label.cget("text") for label in labels])
        window.destroy()
        self.app._refresh()

    def test_timetable_button_displays_selection_and_modified_state(self) -> None:
        button = self.app.profiles_button
        self.assertEqual(button.cget("text"), "Timetables")
        self.assertTrue(self.app.save_profile("Summer"))
        self.assertEqual(button.cget("text"), "Timetable: Summer")
        self.app.fields[0].name.set("Renamed")
        self.assertEqual(button.cget("text"), "Timetable: Summer")
        self.app.fields[0].enabled.set(False)
        self.assertEqual(button.cget("text"), "Timetable: Summer (modified)")
        self.assertTrue(self.app.save())
        self.assertEqual(button.cget("text"), "Timetable: Summer (modified)")
        with patch("app.messagebox.askyesno", return_value=True):
            self.assertTrue(self.app.load_profile("Summer"))
        self.assertEqual(button.cget("text"), "Timetable: Summer")
        self.assertTrue(self.app.save_profile("A very long timetable name"))
        self.app.fields[0].enabled.set(False)
        self.assertEqual(button.cget("text"), "Timetable: A very long t... (modified)")
        self.app.geometry("940x620")
        self.app.update()
        self.assertLessEqual(
            button.winfo_rootx() + button.winfo_width(),
            button.master.winfo_rootx() + button.master.winfo_width(),
        )
        self.app.show_profiles()
        labels = [
            widget.cget("text") for widget in self.app.profile_window.winfo_children()
            if isinstance(widget, ctk.CTkLabel)
        ]
        self.assertIn("Selected: A very long timetable name (modified)", labels)
        self.app.profile_window.destroy()

    def test_pump_exclusion_autosaves_without_stopping_zone(self) -> None:
        self.app.AUTOSAVE_DELAY_MS = 20
        control = self.app.pump_exempt_buttons[0]
        self.assertEqual(control.cget("text"), "P")
        control.invoke()
        deadline = time.monotonic() + 2
        while self.app.dirty and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.dirty)
        self.assertTrue(load_zones(self.path)[0].pump_exempt)
        self.assertEqual(control.cget("text"), "P-")
        self.assertIn(0, self.app.controller.runs)
        self.assertFalse(self.app.controller.pump_on)
        self.assertFalse(self.app.fields[0].rain_pause_exempt.get())
        self.assertEqual(self.app.pump_exempt_buttons[7].cget("state"), "disabled")

    def test_exempt_zone_control_saves_and_marks_the_zone_red_during_pause(self) -> None:
        control = self.app.rain_pause_exempt_buttons[0]
        self.assertEqual(control.cget("text"), "+")
        control.invoke()
        self.assertTrue(self.app.fields[0].rain_pause_exempt.get())
        self.assertEqual(control.cget("text"), "X")
        self.assertTrue(self.app.dirty)
        self.assertTrue(self.app.save())
        self.pause()
        self.assertEqual(control.cget("text"), "X")
        self.assertEqual(control.cget("text_color"), RUNNING_RED)
        self.assertIn(0, self.app.controller.runs)
        self.assertEqual(self.app.manual_button.cget("state"), "normal")

    def test_start_header_pauses_and_saves_that_window_number(self) -> None:
        times = next(
            widget for widget in self.app.editor.winfo_children()
            if int(widget.grid_info()["row"]) == 5
        )
        start_two = next(
            widget for widget in times.winfo_children()
            if isinstance(widget, ctk.CTkButton) and widget.cget("text") == "Start 2"
        )
        start_two.invoke()
        self.assertTrue(self.app.fields[0].paused_windows[1].get())
        self.assertEqual(self.app.window_pause_labels[1].cget("text"), "Paused")
        self.assertLess(
            self.app.window_pause_labels[1].cget("font")[1],
            start_two.cget("font")[1],
        )
        self.assertTrue(self.app.dirty)
        self.assertTrue(self.app.save())
        self.assertTrue(load_zones(self.path)[0].paused_windows[1])

    def test_short_time_inputs_display_as_hhmm_and_use_narrow_fields(self) -> None:
        start = self.app.fields[0].schedule[0][0]
        stop = self.app.fields[0].schedule[0][1]
        start.set("815")
        stop.set("840")
        self.app._format_time_field(start)
        self.app._format_time_field(stop)
        self.assertEqual(start.get(), "08:15")
        self.assertEqual(stop.get(), "08:40")
        self.assertEqual(self.app.fields[0].read().schedule[0][:2], ("08:15", "08:40"))

        table = next(
            widget for widget in self.app.editor.winfo_children()
            if int(widget.grid_info()["row"]) == 5
        )
        entries = [
            widget for widget in table.winfo_children()
            if isinstance(widget, ctk.CTkEntry)
        ]
        self.assertTrue(all(entry.cget("width") == 60 for entry in entries))

    def test_display_time_normalizes_all_supported_input_forms(self) -> None:
        for value in ("815", "0815", "08:15"):
            with self.subTest(value=value):
                self.assertEqual(display_time(value), "08:15")

    def test_overlap_message_updates_while_entering_time_and_names_groups(self) -> None:
        self.app.fields[0].schedule[0][2].set("06:10")
        self.app.fields[0].schedule[0][3].set("06:30")
        self.assertIn(
            "Mon Start/Stop group 1 overlaps Start/Stop group 2",
            self.app.validation.cget("text"),
        )
        self.assertEqual(self.app.validation.cget("text_color"), "#ff9999")
        self.assertEqual(self.app.save_button.cget("state"), "disabled")
        conflicting_entries = [
            self.app.schedule_entries[0, index]
            for index in range(4)
        ]
        self.assertTrue(all(entry.cget("border_color") == "#ff5555"
                            for entry in conflicting_entries))
        self.assertTrue(all(entry.cget("border_width") == 2
                            for entry in conflicting_entries))
        self.assertNotEqual(self.app.schedule_entries[0, 4].cget("border_color"), "#ff5555")

    def test_overlap_popup_appears_when_clicking_conflicting_stop_once(self) -> None:
        self.app.fields[0].schedule[0][2].set("06:10")
        self.app.fields[0].schedule[0][3].set("06:30")
        self.app.update()
        with patch("app.messagebox.showwarning") as warning:
            self.app.schedule_entries[0, 5]._entry.event_generate("<Button-1>")
            warning.assert_not_called()
            self.app.schedule_entries[0, 3]._entry.event_generate("<Button-1>")
            warning.assert_called_once()
            self.assertIn("Mon Start/Stop group 1 overlaps Start/Stop group 2",
                          warning.call_args.args[1])
            self.app.schedule_entries[0, 1]._entry.event_generate("<Button-1>")
            warning.assert_called_once()

            self.app.fields[0].schedule[0][2].set("")
            self.app.fields[0].schedule[0][3].set("")
            self.app.fields[0].schedule[0][2].set("06:10")
            self.app.fields[0].schedule[0][3].set("06:30")
            self.app.schedule_entries[0, 3]._entry.event_generate("<Button-1>")
            self.assertEqual(warning.call_count, 2)

    def test_clicking_stop_after_overlapping_start_warns_before_stop_is_entered(self) -> None:
        self.app.fields[0].schedule[0][2].set("06:10")
        self.assertIn(
            "start time falls within",
            overlap_notices(tuple(variable.get() for variable in self.app.fields[0].schedule[0]))[(0, 1)],
        )
        self.app.update()
        with patch("app.messagebox.showwarning") as warning:
            self.app.schedule_entries[0, 3]._canvas.event_generate(
                "<Button-1>", x=0, y=0, when="now"
            )
            warning.assert_called_once()
            self.assertIn("Mon Start/Stop group 2 start time falls within",
                          warning.call_args.args[1])

    def test_run_stop_button_replaces_manual_and_test_controls(self) -> None:
        self.assertEqual(self.app.manual_button.cget("text"), "STOP THIS ZONE")
        self.app.manual_button.invoke()
        self.assertNotIn(0, self.app.controller.runs)
        self.assertEqual(self.app.manual_button.cget("text"), "RUN ZONE (10 min max)")
        self.app.manual_button.invoke()
        self.assertEqual(self.app.controller.runs[0].source, "Manual")
        self.assertEqual(self.app.manual_button.cget("text"), "STOP THIS ZONE")

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
        finally:
            other.on_close()

    def test_compact_layout_keeps_actions_visible_at_opening_and_minimum_sizes(self) -> None:
        for size in ("940x760", "940x620"):
            with self.subTest(size=size):
                self.app.geometry(size)
                self.app.update()
                for widget in (
                    self.app.manual_button,
                    self.app.stop_all_button, self.app.save_button,
                    self.app.overview_button,
                    self.app.weekly_button, self.app.profiles_button,
                    self.app.rain_pause_switch, *self.app.zone_buttons[:8],
                    *self.app.rain_pause_exempt_buttons[:8],
                    *self.app.pump_exempt_buttons[:8], self.app.board_selector,
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
                monday = next(
                    widget for widget in table.winfo_children()
                    if isinstance(widget, ctk.CTkLabel) and widget.cget("text") == "Mon"
                )
                first_entry = min(entries, key=lambda entry: (entry.grid_info()["row"], entry.grid_info()["column"]))
                self.assertLessEqual(
                    first_entry.winfo_rootx() - (monday.winfo_rootx() + monday.winfo_width()),
                    20,
                )
                last_entry = max(
                    entries,
                    key=lambda entry: (entry.grid_info()["row"], entry.grid_info()["column"]),
                )
                self.assertLessEqual(
                    table.winfo_rootx() + table.winfo_width()
                    - last_entry.winfo_rootx() - last_entry.winfo_width(),
                    20,
                )
                for entry in entries:
                    self.assertLessEqual(
                        entry.winfo_rootx() + entry.winfo_width(),
                        self.app.winfo_rootx() + self.app.winfo_width(),
                    )
                    if size == "940x760":
                        self.assertLessEqual(
                            entry.winfo_rooty() + entry.winfo_height(),
                            self.app.editor.master.winfo_rooty()
                            + self.app.editor.master.winfo_height(),
                        )
                if size == "940x760":
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
