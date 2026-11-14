"""GUI rain-pause and pump checks with all physical relay writes mocked."""

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk

from app import (
    GREY, RUNNING_RED, SCHEDULED_GREEN, SprinklerApp, display_time,
    overlap_notices,
)
from controller import (
    Zone, default_zones, load_rain_pause, load_zones, rain_pause_path, save_zones,
)
from test_controller import schedule_for
from waveshare_relay import WaveshareRelays
from service_client import ServiceClient
from test_service import FakeDevice, RunningService


@unittest.skipUnless(os.environ.get("DISPLAY"), "Requires a graphical display")
class RainPauseGuiTests(unittest.TestCase):
    def test_group_buttons_and_station_retry_do_not_fault_desktop_or_stop_garden(self):
        from dataclasses import replace
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            save_zones(config, [replace(zone, rain_pause_exempt=True) for zone in default_zones(16)])
            with RunningService(Path(directory), secondary) as running:
                client = ServiceClient(running.path)
                other = SprinklerApp(running.config, client)
                try:
                    client.start_manual(0, datetime.now())
                    client.start_manual(9, datetime.now())
                    secondary.fail = True
                    with patch("app.messagebox.showerror") as error, patch("app.datetime", datetime):
                        other.select_zone(8)
                        other.manual()
                        self.assertTrue(error.called)
                        self.assertFalse(other.faulted)
                        self.assertTrue(running.device.states[0])
                        self.assertEqual(other.manual_button.cget("text"), "RETRY STATION (OFF)")
                        self.assertIn("STATION FAULT", other.status.cget("text"))
                        secondary.fail = False
                        other.manual()
                        self.assertEqual(other.manual_button.cget("text"), "RUN ZONE")
                        self.assertNotIn(8, client.runs)
                        self.assertIn(9, client.runs)
                        other.stop_appliances_button.invoke()
                        self.assertIn(0, client.runs)
                        self.assertNotIn(9, client.runs)
                        self.assertTrue(running.device.states[7])
                        other.stop_garden_button.invoke()
                        self.assertFalse(client.runs)
                        self.assertFalse(running.device.states[7])
                finally:
                    secondary.fail = False
                    other.on_close()

    def test_tapo_timeline_graph_renders_recorded_and_unknown_periods(self) -> None:
        from activity_history import ActivityHistory
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory:
            with RunningService(Path(directory)) as running:
                client = ServiceClient(running.path)
                other = SprinklerApp(running.config, client)
                result = ActivityHistory(running.config).timeline(8)
                result["name"] = "Tapo Plug 1"
                result["off_peak"] = True
                result["samples"] = [{
                    "timestamp": result["end"], "updated": result["end"], "state": False,
                    "power_w": 12.5, "today_kwh": 0.1, "month_kwh": 1.0,
                    "voltage_v": 240.0, "current_a": 0.05, "error": "",
                }]
                try:
                    with patch("app.datetime", datetime), patch.object(client, "tapo_timeline", return_value=result) as read:
                        other.show_tapo_timeline(8)
                        window = next(w for w in other.winfo_children()
                                      if isinstance(w, ctk.CTkToplevel) and "24-hour" in w.title())
                        canvas = next(w for w in window.winfo_children() if isinstance(w, tk.Canvas))
                        deadline = time.monotonic() + 2
                        while not canvas.find_all() and time.monotonic() < deadline:
                            other.update()
                            time.sleep(0.01)
                        self.assertTrue(canvas.find_all())
                        self.assertTrue(any(
                            canvas.type(item) == "rectangle" and canvas.coords(item)[1:4:2] == [63.0, 72.0]
                            for item in canvas.find_all()
                        ))
                        sample_canvas = [w for w in window.winfo_children() if isinstance(w, tk.Canvas)][1]
                        self.assertTrue(any(sample_canvas.type(item) == "oval" for item in sample_canvas.find_all()))
                        labels = [w.cget("text") for w in window.winfo_children() if isinstance(w, ctk.CTkLabel)]
                        self.assertTrue(any("Controller switching history only" in value for value in labels))
                        read.assert_called_once_with(8)
                finally:
                    other.on_close()

    def test_tapo_details_window_is_read_only_and_shows_unavailable_values(self) -> None:
        from tkinter import ttk
        with tempfile.TemporaryDirectory() as directory:
            with RunningService(Path(directory)) as running:
                client = ServiceClient(running.path)
                other = SprinklerApp(running.config, client)
                row = {
                    "channel": 15, "name": "Hot Water System", "state": True,
                    "power_w": 0.0, "today_kwh": None, "month_kwh": 1.25,
                    "voltage_v": 234.0, "current_a": None,
                    "updated": "2026-01-01T00:00:00+00:00", "error": "",
                }
                try:
                    with (
                        patch("app.datetime", datetime),
                        patch.object(client, "tapo_details", return_value={"reading": False, "rows": [row]}) as read,
                    ):
                        other.show_tapo_details()
                        deadline = time.monotonic() + 2
                        table = next(w for w in other.tapo_window.winfo_children() if isinstance(w, ttk.Treeview))
                        while not table.get_children() and time.monotonic() < deadline:
                            other.update()
                            time.sleep(0.01)
                        values = table.item(table.get_children()[0], "values")
                        self.assertEqual(values[:3], ("16", "Hot Water System", "ON"))
                        self.assertIn("Unavailable", values)
                        self.assertIn("Stale", values)
                        read.assert_called_once_with(True)
                        row.update({
                            "state": None, "off_peak": True, "requested_state": True,
                            "last_confirmed_state": True, "confirmed_at": row["updated"],
                            "availability": "Unavailable: supply or connection unknown",
                        })
                        refresh = next(w for w in other.tapo_window.winfo_children()
                                       if isinstance(w, ctk.CTkButton))
                        refresh.invoke()
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            other.update()
                            values = table.item(table.get_children()[0], "values")
                            if "Requested ON" in str(values):
                                break
                            time.sleep(0.01)
                        self.assertEqual(values[2], "Unavailable")
                        self.assertIn("last confirmed ON", values[-1])
                        self.assertIn("2026-01-01", values[-1])
                        first = other.tapo_window
                        other.show_tapo_details()
                        self.assertIs(other.tapo_window, first)
                finally:
                    other.on_close()

    def test_custom_hours_summary_run_and_maximum(self) -> None:
        self.app.select_zone(1)
        self.app.manual_duration.set("Custom")
        self.app.custom_duration.set(" 2.5H ")
        self.assertIn("2 hours 30 minutes (150 minutes)", self.app.duration_summary.cget("text"))
        with patch.object(self.app.controller, "start_manual") as start:
            self.app.manual()
        self.assertEqual(start.call_args.kwargs["duration_minutes"], 150)
        for value in ("0.01h", "1.5", "24.1h"):
            self.app.custom_duration.set(value)
            with patch("app.messagebox.showerror") as error, \
                    patch.object(self.app.controller, "start_manual") as start:
                self.app.manual()
            error.assert_called_once()
            start.assert_not_called()
        self.app.fields[1].max_manual_minutes.set("30")
        self.assertTrue(self.app.save())
        self.app.custom_duration.set("2.5h")
        with patch("app.messagebox.showerror") as error:
            self.app.manual()
        self.assertIn("maximum", error.call_args.args[1])
        self.assertNotIn(1, self.app.controller.runs)

    def test_unassigned_tapo_channel_is_editable_but_cannot_run(self) -> None:
        import json
        from tapo_plugs import mapping_path

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            mapping_path(config).write_text(json.dumps({"version": 1, "plugs": [None] * 8}))
            with RunningService(Path(directory)) as running:
                other = SprinklerApp(running.config, ServiceClient(running.path))
                try:
                    other.select_zone(8)
                    other.update()
                    self.assertIn("UNASSIGNED", other.status.cget("text"))
                    self.assertEqual(other.manual_button.cget("state"), "disabled")
                    self.assertEqual(other.duration_menu.cget("state"), "disabled")
                    self.assertEqual(other.zone_buttons[8].cget("state"), "normal")
                    self.assertIn("Unassigned", other.zone_buttons[8].cget("text"))
                    other.fields[8].schedule[0][0].set("0900")
                    other.fields[8].schedule[0][1].set("0910")
                    other.show_overview()
                    other.overview.day.set("Mon")
                    other.overview.refresh()
                    other.show_weekly_overview()
                    self.assertEqual([row.channel for row in other.overview.rows], [8])
                    self.assertEqual({channel for channel, _day in other.weekly_overview.cells}, set(range(8, 16)))
                    other.geometry("940x620")
                    other.update()
                    self.assertLessEqual(
                        other.manual_button.winfo_rooty() + other.manual_button.winfo_height(),
                        other.winfo_rooty() + other.winfo_height(),
                    )
                    other.fields[8].name.set("Shed Fan")
                    self.assertTrue(other.save())
                    self.assertEqual(other.controller.zones[8].name, "Shed Fan")
                    self.assertNotIn(8, other.controller.runs)
                finally:
                    other.dirty = False
                    other.on_close()

    def test_restore_rejects_settings_changed_during_confirmation(self) -> None:
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            path = Path(directory) / "backup.json"
            password = "four unrelated backup words"
            import json
            path.write_text(json.dumps(other.controller.backup("backup_export", password=password)["archive"]))

            def confirm(*_args, **_kwargs):
                client = ServiceClient(running.path)
                zones = list(client.zones)
                zones[1] = replace(zones[1], name="Changed elsewhere")
                client.apply(zones, datetime.now())
                other._tick()
                return True

            try:
                with patch("app.filedialog.askopenfilename", return_value=str(path)), \
                        patch.object(other, "_backup_password", return_value=password), \
                        patch("app.messagebox.askyesno", side_effect=confirm), \
                        patch("app.messagebox.showerror") as error:
                    other.restore_backup()
                self.assertIn("Settings changed", error.call_args.args[1])
                self.assertEqual(load_zones(running.config)[1].name, "Changed elsewhere")
            finally:
                other.on_close()

    def test_continuous_reminder_is_informational(self) -> None:
        self.app.select_zone(1)
        self.app.fields[1].pump_exempt.set(True)
        self.app.fields[1].continuous_reminder_minutes.set("1")
        self.assertTrue(self.app.save())
        with patch.object(self.app.controller, "clock", return_value=100):
            self.app.controller.start_manual(1, self.time.now(), continuous=True)
        with patch.object(self.app.controller, "clock", return_value=159):
            self.app._refresh()
            self.assertNotIn("REMINDERS", self.app.running_text)
        with patch.object(self.app.controller, "clock", return_value=160):
            self.app._refresh()
            self.assertIn("continuous run reached 1 minutes", self.app.running_text)
            self.assertIn("Reminder", self.app.running_button.cget("text"))
            self.assertIn(1, self.app.controller.runs)

    def test_backup_restore_validates_and_confirms_before_applying(self) -> None:
        import json
        import system_backup
        password = "four unrelated backup words"
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                path = Path(directory) / "backup.sprinkler-backup"
                with patch("app.filedialog.asksaveasfilename", return_value=str(path)), \
                        patch.object(other, "_backup_password", return_value=password), \
                        patch("app.messagebox.showinfo"):
                    other.export_backup()
                payload = system_backup.decrypt(json.loads(path.read_text()), password)
                self.assertEqual(payload["settings"]["zones"][1]["name"], other.controller.zones[1].name)
                payload["settings"]["zones"][1]["name"] = "Backup fan"
                path.write_text(json.dumps(system_backup.encrypt(payload, password)))
                with patch("app.filedialog.askopenfilename", return_value=str(path)), \
                        patch.object(other, "_backup_password", return_value=password), \
                        patch("app.messagebox.askyesno", return_value=False):
                    other.restore_backup()
                self.assertNotEqual(load_zones(running.config)[1].name, "Backup fan")
                with patch("app.filedialog.askopenfilename", return_value=str(path)), \
                        patch.object(other, "_backup_password", return_value=password), \
                        patch("app.messagebox.askyesno", return_value=True), \
                        patch("app.messagebox.showinfo"):
                    other.restore_backup()
                self.assertEqual(load_zones(running.config)[1].name, "Backup fan")
                self.assertTrue(other.controller.operation_held)
                self.assertEqual(other.manual_button.cget("state"), "disabled")
                self.assertIn("SAFETY HOLD", other.status.cget("text"))
            finally:
                other.dirty = False
                other.on_close()

    def test_backup_password_dialog_confirms_and_shows_password(self) -> None:
        observed = {}

        def fill_dialog() -> None:
            window = next(widget for widget in self.app.winfo_children() if isinstance(widget, ctk.CTkToplevel))
            try:
                entries = [widget for widget in window.winfo_children() if isinstance(widget, ctk.CTkEntry)]
                entries[0].insert(0, "four unrelated backup words")
                entries[1].insert(0, "wrong confirmation")
                proceed = next(widget for widget in window.winfo_children()
                               if isinstance(widget, ctk.CTkButton) and widget.cget("text") == "Continue")
                proceed.invoke()
                observed["mismatch"] = any(
                    isinstance(widget, ctk.CTkLabel) and "do not match" in widget.cget("text")
                    for widget in window.winfo_children()
                )
                show = next(widget for widget in window.winfo_children() if isinstance(widget, ctk.CTkCheckBox))
                show.toggle()
                observed["shown"] = all(entry.cget("show") == "" for entry in entries)
                entries[1].delete(0, "end")
                entries[1].insert(0, "four unrelated backup words")
                proceed.invoke()
            finally:
                if window.winfo_exists():
                    window.destroy()

        self.app.after(100, fill_dialog)
        self.assertEqual(self.app._backup_password(confirm=True), "four unrelated backup words")
        self.assertEqual(observed, {"mismatch": True, "shown": True})

    def test_running_summary_shows_all_runs_and_unknown_fault(self) -> None:
        self.app.controller.start_manual(1, self.time.now(), duration_minutes=30)
        self.app._refresh()
        self.app.show_running()
        self.app.update()
        summary = self.app.running_summary.get("1.0", "end")
        self.assertIn("Channel 1, Lawn - Routine - until about", summary)
        self.assertIn("Channel 2,", summary)
        self.assertIn("Manual - until about", summary)
        self.app.faulted = True
        self.app._refresh()
        self.assertIn("Output states unknown", self.app.running_summary.get("1.0", "end"))
        self.app.running_window.destroy()
        self.app.faulted = False

    def test_duration_summary_and_custom_visibility(self) -> None:
        self.app.select_zone(1)
        self.app.update()
        runs_before = self.app.controller.runs.copy()
        self.assertFalse(self.app.duration_entry.winfo_ismapped())
        self.assertIn("10 minutes", self.app.duration_summary.cget("text"))
        self.app.duration_menu._dropdown_callback("Custom")
        self.app.update()
        self.assertTrue(self.app.duration_entry.winfo_ismapped())
        self.assertIn("custom minutes", self.app.duration_summary.cget("text"))
        self.app.custom_duration.set("45")
        self.assertIn("45 minutes", self.app.duration_summary.cget("text"))
        self.app.duration_menu._dropdown_callback("30 min")
        self.app.update()
        self.assertFalse(self.app.duration_entry.winfo_ismapped())
        self.assertIn("30 minutes", self.app.duration_summary.cget("text"))
        self.assertEqual(self.app.controller.runs, runs_before)

    def test_duration_menu_stays_open_across_refresh_and_avoids_stop_buttons(self) -> None:
        self.app.select_zone(1)
        self.app.update()
        menu = self.app.duration_menu._dropdown_menu
        self.app.duration_menu._open_dropdown_menu()
        self.app.update()
        try:
            menu.activate(1)
            self.app._refresh()
            self.app.update()
            self.assertEqual(menu.index("active"), 1)
            menu.invoke(1)
            self.assertEqual(self.app.manual_duration.get(), "30 min")
            menu.activate(3)
            self.app._refresh()
            self.app.update()
            self.assertEqual(menu.index("active"), 3)
            menu.invoke(3)
            self.assertEqual(self.app.manual_duration.get(), "Custom")
            self.assertEqual(self.app.duration_entry.cget("state"), "normal")
            self.assertLessEqual(
                menu.winfo_rootx() + menu.winfo_width(),
                self.app.manual_button.winfo_rootx(),
            )
            self.assertLessEqual(
                menu.winfo_rootx() + menu.winfo_width(),
                self.app.stop_all_button.winfo_rootx(),
            )
        finally:
            menu.unpost()

    def test_history_newest_visible_on_open_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            events = [
                {"timestamp": "2026-11-10T12:49:16+10:00", "channel": 6,
                 "name": f"Fan {index}", "event": "ON", "reason": "Manual"}
                for index in range(25)
            ]
            events[-1] = {
                "timestamp": "2026-11-10T12:18:09+10:00", "channel": None,
                "name": "Controller", "event": "STARTED", "reason": "Startup",
            }
            try:
                self.time.fromisoformat.side_effect = datetime.fromisoformat
                with patch.object(other.controller, "history", return_value={
                    "events": events, "offset": 0, "total": 25, "page_size": 25, "warning": "",
                }):
                    other.show_history()
                    other.update()
                    contents = next(
                        widget for widget in other.history_window.winfo_children()
                        if isinstance(widget, ctk.CTkTextbox)
                    )
                    self.assertTrue(contents.get("1.0", "end").startswith(
                        "10/11 12:49 - Channel 7, Fan 0 - ON - Manual"
                    ))
                    self.assertIn("10/11 12:18 - System - STARTED - Startup",
                                  contents.get("1.0", "end"))
                    self.assertEqual(contents.yview()[0], 0.0)
                    contents.yview_moveto(1)
                    controls = next(
                        widget for widget in other.history_window.winfo_children()
                        if isinstance(widget, ctk.CTkFrame) and any(
                            isinstance(child, ctk.CTkButton) and child.cget("text") == "Refresh"
                            for child in widget.winfo_children()
                        )
                    )
                    next(child for child in controls.winfo_children()
                         if isinstance(child, ctk.CTkButton) and child.cget("text") == "Refresh").invoke()
                    other.update()
                    self.assertEqual(contents.yview()[0], 0.0)
                    other.history_window.destroy()
            finally:
                other.on_close()

    def test_custom_duration_accepts_keyboard_input_during_refresh(self) -> None:
        self.app.select_zone(1)
        self.app.update()
        self.app.duration_menu._dropdown_callback("Custom")
        self.app.update()
        entry = self.app.duration_entry._entry
        entry.focus_force()
        self.app.update()
        entry.event_generate("<KeyPress>", keysym="4")
        self.app.update()
        self.assertEqual(self.app.custom_duration.get(), "4")
        self.app._refresh()
        self.app.update()
        entry.event_generate("<KeyPress>", keysym="5")
        self.app.update()
        self.assertEqual(self.app.custom_duration.get(), "45")

    def test_selected_status_reports_next_schedule_and_unknown_fault(self) -> None:
        self.app._refresh()
        self.assertIn("Next scheduled start:", self.app.status.cget("text"))
        self.app.faulted = True
        self.app._refresh()
        self.assertIn("unknown", self.app.status.cget("text"))
        self.assertNotIn("Next scheduled start", self.app.status.cget("text"))
        self.app.faulted = False

    def test_history_window_shows_service_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory)) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                other.manual()
                other.show_history()
                other.update()
                boxes = [widget for widget in other.history_window.winfo_children()
                         if isinstance(widget, ctk.CTkTextbox)]
                self.assertEqual(len(boxes), 1)
                self.assertIn("Manual", boxes[0].get("1.0", "end"))
                other.history_window.destroy()
            finally:
                other.on_close()

    def test_manual_timer_controls_and_saved_maximum(self) -> None:
        self.app.select_zone(1)
        self.app.fields[1].pump_exempt.set(True)
        self.assertTrue(self.app.save())
        self.assertIn("Until stopped", self.app.duration_menu.cget("values"))
        self.app.fields[1].max_manual_minutes.set("30")
        self.assertNotIn("Until stopped", self.app.duration_menu.cget("values"))
        with patch("app.messagebox.showerror") as error:
            self.app.manual()
        self.assertIn("Save the manual maximum", error.call_args.args[1])
        self.assertTrue(self.app.save())
        self.app.manual_duration.set("Custom")
        self.app._refresh()
        self.assertEqual(self.app.duration_entry.cget("state"), "normal")
        self.app.custom_duration.set("31")
        with patch("app.messagebox.showerror") as error:
            self.app.manual()
        self.assertIn("maximum", error.call_args.args[1])
        self.app.custom_duration.set("30")
        self.app.manual()
        self.assertEqual(self.app.controller.runs[1].source, "Manual")
        self.assertGreater(self.app.controller.runs[1].deadline - self.app.controller.clock(), 1790)
        self.app.manual()
        self.assertNotIn(1, self.app.controller.runs)
        self.app.fields[1].max_manual_minutes.set("0")
        self.assertEqual(self.app.save_button.cget("state"), "disabled")
        self.app.fields[1].max_manual_minutes.set("")
        self.assertTrue(self.app.save())
        self.app.manual_duration.set("Until stopped")
        with patch("app.messagebox.askyesno", return_value=True):
            self.app.manual()
        self.assertIsNone(self.app.controller.runs[1].deadline)
        self.app.manual()

    def test_continuous_status_and_existing_stop_button(self) -> None:
        self.app.fields[0].pump_exempt.set(True)
        self.app.save()
        self.app.controller.start_manual(0, self.time.now(), continuous=True)
        self.app._refresh()
        self.assertIn("Continuous / Until stopped", self.app.status.cget("text"))
        self.assertEqual(self.app.manual_button.cget("text"), "STOP THIS ZONE")
        self.app.manual()
        self.assertNotIn(0, self.app.controller.runs)

    def setUp(self) -> None:
        original_destroy = SprinklerApp.destroy

        def destroy(app: SprinklerApp) -> None:
            # CustomTkinter leaves internal callbacks queued when a test root closes.
            for timer in app.tk.call("after", "info"):
                app.tk.call("after", "cancel", timer)
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

    def test_schedule_day_and_week_views_only_change_visibility(self) -> None:
        self.app.update()
        self.assertEqual(self.app.schedule_day.get(), "Mon")
        self.assertFalse(self.app.schedule_week_view)
        original = [fields.read() for fields in self.app.fields]
        for day in range(7):
            self.assertEqual(
                sum(entry.winfo_ismapped() for (index, _), entry in self.app.schedule_entries.items() if index == day),
                8 if day == 0 else 0,
            )
        self.app.schedule_day_menu._dropdown_callback("Wed")
        self.app.update()
        self.assertEqual(
            {day for (day, _), entry in self.app.schedule_entries.items() if entry.winfo_ismapped()},
            {2},
        )
        self.app.schedule_view_button.invoke()
        self.app.update()
        self.assertEqual(self.app.schedule_view_button.cget("text"), "See one day view")
        self.assertEqual(sum(entry.winfo_ismapped() for entry in self.app.schedule_entries.values()), 56)
        self.app.select_zone(1)
        self.app.update()
        self.assertEqual(self.app.schedule_day.get(), "Wed")
        self.assertTrue(self.app.schedule_week_view)
        self.app.schedule_view_button.invoke()
        self.app.select_zone(7)
        self.app.select_zone(0)
        self.app.update()
        self.assertEqual(
            {day for (day, _), entry in self.app.schedule_entries.items() if entry.winfo_ismapped()},
            {2},
        )
        self.assertEqual([fields.read() for fields in self.app.fields], original)
        self.assertFalse(self.app.dirty)
        self.assertIsNone(self.app.autosave_timer)

    def test_copy_day_uses_displayed_day_and_preserves_hidden_edits(self) -> None:
        self.app.schedule_day_menu._dropdown_callback("Wed")
        self.app.fields[0].schedule[2][0].set("08:15")
        self.app.fields[0].schedule[2][1].set("08:40")
        self.app.schedule_day_menu._dropdown_callback("Fri")
        self.app.schedule_day_menu._dropdown_callback("Wed")
        self.assertEqual(self.app.schedule_entries[2, 0].get(), "08:15")
        self.app.copy_day_button.toggle()
        for row in self.app.fields[0].schedule:
            self.assertEqual([value.get() for value in row[:2]], ["08:15", "08:40"])
        self.assertTrue(self.app.save())
        self.assertEqual(load_zones(self.path)[0].schedule, (("08:15", "08:40") + ("",) * 6,) * 7)

    def test_one_day_view_saves_six_rows_and_validates_hidden_days(self) -> None:
        self.app.geometry("940x620")
        self.app.update()
        table = self.app.schedule_entries[0, 0].master
        compact_height = table.winfo_height()
        for control in (self.app.schedule_day_menu, self.app.schedule_view_button, self.app.copy_day_button):
            self.assertLessEqual(
                control.winfo_rootx() + control.winfo_width(),
                self.app.winfo_rootx() + self.app.winfo_width(),
            )
        self.app.schedule_view_button.invoke()
        self.app.update()
        self.assertGreaterEqual(table.winfo_height() - compact_height, 6 * 26)
        self.app.schedule_view_button.invoke()
        self.app.fields[0].schedule[2][0].set("invalid")
        self.app.update()
        self.assertFalse(self.app.schedule_entries[2, 0].winfo_ismapped())
        self.assertEqual(self.app.save_button.cget("state"), "disabled")
        self.assertIsNone(self.app.autosave_timer)

    def test_group_pause_button_stops_only_its_group_and_resume_waits(self) -> None:
        self.app.update()
        self.assertIn(0, self.app.controller.runs)
        button = self.app.window_pause_buttons[0]
        self.assertEqual(button.cget("text"), "Not Paused")
        button.invoke()
        self.assertTrue(self.app.save())
        self.app.update()
        self.assertNotIn(0, self.app.controller.runs)
        self.assertEqual(button.cget("text"), "Paused")
        self.assertEqual(button.cget("fg_color"), "#b63e4b")
        self.assertEqual(self.app.manual_button.cget("state"), "normal")
        button.invoke()
        self.assertTrue(self.app.save())
        self.app._tick()
        self.assertNotIn(0, self.app.controller.runs)
        self.assertEqual(self.app.manual_button.cget("state"), "normal")
        self.assertEqual(button.cget("text"), "Not Paused")
        self.assertFalse(any(self.app.fields[0].paused_windows[index].get() for index in (1, 2, 3)))
        self.app.select_zone(7)
        self.assertEqual(self.app.window_pause_buttons, [])

    def test_linux_mouse_wheel_scrolls_editor_only(self) -> None:
        self.app.geometry("940x620")
        self.app.update()
        viewport = self.app.editor.master
        for channel in (0, 1):
            self.app.select_zone(channel)
            self.app.update()
            entry = next(
                widget for widget in self.app.editor.winfo_children()
                if isinstance(widget, ctk.CTkEntry)
            )
            for target in (viewport, self.app.editor, entry._entry):
                with self.subTest(channel=channel, target=str(target)):
                    viewport.yview_moveto(0)
                    self.app.update()
                    target.event_generate("<Button-5>")
                    self.app.update()
                    self.assertGreater(viewport.yview()[0], 0)
                    target.event_generate("<Button-4>")
                    self.app.update()
                    self.assertAlmostEqual(viewport.yview()[0], 0)

            viewport.yview_moveto(0.5)
            self.app.update()
            position = viewport.yview()
            for target in (self.app.manual_button, self.app.zone_buttons[channel]):
                for event in ("<Button-4>", "<Button-5>"):
                    target.event_generate(event)
                    self.app.update()
                    self.assertEqual(viewport.yview(), position)

    def test_existing_mouse_wheel_and_scrollbar_still_scroll_editor(self) -> None:
        self.app.geometry("940x620")
        self.app.update()
        viewport = self.app.editor.master
        viewport.yview_moveto(0)
        viewport.event_generate("<MouseWheel>", delta=-120)
        self.app.update()
        self.assertGreater(viewport.yview()[0], 0)
        viewport.event_generate("<MouseWheel>", delta=120)
        self.app.update()
        self.assertAlmostEqual(viewport.yview()[0], 0)
        self.app.editor._scrollbar.cget("command")("moveto", 1)
        self.app.update()
        self.assertAlmostEqual(viewport.yview()[1], 1)

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
                self.assertIsNone(other.pump_setting)
                self.assertIn("Pump: independent", other.channel_summary.cget("text"))
                other.rain_setting.toggle()
                self.assertTrue(other.fields[15].rain_pause_exempt.get())
                self.assertEqual(other.rain_pause_exempt_buttons[15].cget("text"), "R-")
                other.fields[15].name.set("Second board end")
                self.assertTrue(other.save())
                self.assertEqual(load_zones(running.config)[15].name, "Second board end")
                other.manual()
                self.assertTrue(secondary.states[7])
                self.assertFalse(running.device.states[7])
                other.show_weekly_overview()
                self.assertEqual(len(other.weekly_overview.cells), 56)
                other.fields[0].schedule[0][0].set("0900")
                other.fields[0].schedule[0][1].set("0910")
                other.fields[15].schedule[0][0].set("1000")
                other.fields[15].schedule[0][1].set("1010")
                other.show_overview()
                other.overview.day.set("Mon")
                other.overview.refresh()
                self.assertEqual([row.channel for row in other.overview.rows], [15])
                self.assertIn("Channels 9-16", other.overview.summary.cget("text"))
                self.assertIn("Channels 9-16", other.weekly_overview.summary.cget("text"))
                other.overview.table.selection_set("0")
                other.overview.open_selected()
                self.assertEqual(other.selected, 15)
                other.board_selector.set("Channels 1-8")
                other.show_board("Channels 1-8")
                self.assertEqual([row.channel for row in other.overview.rows], [0])
                self.assertEqual({channel for channel, _day in other.weekly_overview.cells}, set(range(7)))
                self.assertIn("Channels 1-8", other.weekly_overview.summary.cget("text"))
                other.select_zone(15)
                self.assertEqual([row.channel for row in other.overview.rows], [15])
                self.assertEqual({channel for channel, _day in other.weekly_overview.cells}, set(range(8, 16)))
                other.weekly_overview.open_channel(15)
                self.assertEqual(other.selected, 15)
            finally:
                other.dirty = False
                other.on_close()

    def test_switching_banks_switches_schedule_editor_without_changing_times(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory), secondary) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                other.AUTOSAVE_DELAY_MS = 60_000
                other.schedule_day_menu._dropdown_callback("Wed")
                other.fields[0].schedule[2][0].set("08:00")
                other.fields[0].schedule[2][1].set("08:10")
                other.fields[8].schedule[2][0].set("09:00")
                other.fields[8].schedule[2][1].set("09:10")
                original = [fields.read() for fields in other.fields]
                other.board_selector.set("Channels 9-16")
                other.show_board("Channels 9-16")
                other.update()
                self.assertEqual(other.selected, 8)
                self.assertEqual(other.heading.cget("text"), "ZONE 9 CONFIGURATION")
                self.assertEqual(other.schedule_day.get(), "Wed")
                self.assertEqual(other.schedule_entries[2, 0].get(), "09:00")
                self.assertTrue(other.schedule_entries[2, 0].winfo_ismapped())
                other.schedule_entries[2, 0].delete(0, "end")
                other.schedule_entries[2, 0].insert(0, "09:01")
                self.assertEqual(other.fields[8].schedule[2][0].get(), "09:01")
                self.assertEqual(other.fields[0].read(), original[0])
                other.select_zone(15)
                other.show_board("Channels 9-16")
                self.assertEqual(other.selected, 15)
                other.schedule_view_button.invoke()
                other.board_selector.set("Channels 1-8")
                other.show_board("Channels 1-8")
                other.update()
                self.assertEqual(other.selected, 0)
                self.assertEqual(other.schedule_entries[2, 0].get(), "08:00")
                self.assertTrue(other.schedule_week_view)
                self.assertEqual(sum(entry.winfo_ismapped() for entry in other.schedule_entries.values()), 56)
                self.assertEqual(other.fields[8].schedule[2][0].get(), "09:01")
                other.select_zone(12)
                self.assertEqual(other.selected, 12)
                self.assertEqual(other.board_selector.get(), "Channels 9-16")
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

    def test_timetable_dialog_tracks_bank_and_load_preserves_other_bank_edits(self) -> None:
        secondary = FakeDevice()
        secondary.host = "192.0.2.2"
        with tempfile.TemporaryDirectory() as directory, RunningService(Path(directory), secondary) as running:
            other = SprinklerApp(running.config, ServiceClient(running.path))
            try:
                self.assertTrue(other.save_profile("Garden"))
                other.show_profiles()
                window = other.profile_window
                self.assertIn("Channels 1-8", window.title())
                other.select_zone(8)
                self.assertIn("Channels 9-16", window.title())
                self.assertEqual(other.timetable_status()["names"], [])
                other.fields[8].schedule[0][0].set("0900")
                other.fields[8].schedule[0][1].set("0910")
                self.assertTrue(other.save_profile("Fans"))
                other.fields[8].schedule[0][0].set("1000")
                other.fields[8].schedule[0][1].set("1010")
                self.assertTrue(other.save_profile("Evening"))
                other.fields[0].schedule[0][0].set("invalid")
                primary = list(other.controller.zones[:8])
                with patch("app.messagebox.askyesno", return_value=True) as confirm:
                    self.assertTrue(other.load_profile("Fans"))
                self.assertIn("channels 9-16 only", confirm.call_args.args[1])
                self.assertEqual(other.controller.zones[:8], primary)
                self.assertEqual(other.fields[0].schedule[0][0].get(), "invalid")
                self.assertTrue(other.dirty)
                self.assertEqual(other.fields[8].schedule[0][0].get(), "09:00")
                self.assertFalse(other.timetable_status()["modified"])
                with patch("app.messagebox.askyesno", return_value=True):
                    self.assertTrue(other.load_profile("Before last load"))
                self.assertEqual(other.fields[8].schedule[0][0].get(), "10:00")
                other.select_zone(0)
                self.assertIn("Channels 1-8", window.title())
                self.assertEqual(other.timetable_status()["names"], ["Garden"])
                self.assertEqual(other.timetable_status()["selected"], "Garden")
                self.assertTrue(other.timetable_status()["modified"])
                other.select_zone(8)
                self.assertFalse(other.timetable_status()["modified"])
            finally:
                other.dirty = False
                other.on_close()

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
        self.assertEqual(control.cget("text_color"), GREY)
        control.invoke()
        deadline = time.monotonic() + 2
        while self.app.dirty and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.dirty)
        self.assertTrue(load_zones(self.path)[0].pump_exempt)
        self.assertEqual(control.cget("text"), "P-")
        self.assertEqual(control.cget("text_color"), RUNNING_RED)
        self.assertIn(0, self.app.controller.runs)
        self.assertFalse(self.app.controller.pump_on)
        self.assertFalse(self.app.fields[0].rain_pause_exempt.get())
        self.assertEqual(self.app.pump_exempt_buttons[7].cget("state"), "disabled")
        control.invoke()
        self.assertTrue(self.app.save())
        self.assertEqual(control.cget("text"), "P")
        self.assertEqual(control.cget("text_color"), GREY)

    def test_selected_channel_exclusion_controls_and_pending_summary(self) -> None:
        self.assertIn("Lawn / Pump: uses pump / Rain Pause: stops", self.app.channel_summary.cget("text"))
        self.assertEqual(self.app.pump_setting.cget("text"), "Master pump: uses pump")
        self.assertEqual(self.app.rain_setting.cget("text"), "Rain Pause: stops this channel")
        self.app.pump_setting.toggle()
        self.app.rain_setting.toggle()
        self.assertTrue(self.app.fields[0].pump_exempt.get())
        self.assertTrue(self.app.fields[0].rain_pause_exempt.get())
        self.assertEqual(self.app.pump_exempt_buttons[0].cget("text"), "P-")
        self.assertEqual(self.app.rain_pause_exempt_buttons[0].cget("text"), "R-")
        self.assertIn("Unsaved - not yet applied", self.app.channel_summary.cget("text"))
        self.assertTrue(self.app.save())
        self.assertIn("Pump: excluded / Rain Pause: continues", self.app.channel_summary.cget("text"))
        self.assertNotIn("Unsaved", self.app.channel_summary.cget("text"))
        self.app.pump_exempt_buttons[0].invoke()
        self.assertEqual(self.app.pump_setting.cget("text"), "Master pump: uses pump")
        self.app.rain_pause_exempt_buttons[0].invoke()
        self.assertEqual(self.app.rain_setting.cget("text"), "Rain Pause: stops this channel")
        self.app.faulted = True
        self.app._refresh()
        self.assertEqual(self.app.pump_setting.cget("state"), "disabled")
        self.assertEqual(self.app.rain_setting.cget("state"), "disabled")
        self.app.faulted = False

    def test_exempt_zone_control_saves_and_marks_the_zone_red_during_pause(self) -> None:
        control = self.app.rain_pause_exempt_buttons[0]
        self.assertEqual(control.cget("text"), "R")
        control.invoke()
        self.assertTrue(self.app.fields[0].rain_pause_exempt.get())
        self.assertEqual(control.cget("text"), "R-")
        self.assertTrue(self.app.dirty)
        self.assertTrue(self.app.save())
        self.pause()
        self.assertEqual(control.cget("text"), "R-")
        self.assertEqual(control.cget("text_color"), RUNNING_RED)
        self.assertIn(0, self.app.controller.runs)
        self.assertEqual(self.app.manual_button.cget("state"), "normal")
        control.invoke()
        self.assertFalse(self.app.fields[0].rain_pause_exempt.get())
        self.assertEqual(control.cget("text"), "R")

    def test_pause_button_sits_below_start_and_saves_that_group(self) -> None:
        times = next(
            widget for widget in self.app.editor.winfo_children()
            if int(widget.grid_info()["row"]) == 5
        )
        start_two = next(
            widget for widget in times.winfo_children()
            if isinstance(widget, ctk.CTkLabel) and widget.cget("text") == "Start 2"
        )
        button = self.app.window_pause_buttons[1]
        self.assertEqual(button.grid_info()["column"], start_two.grid_info()["column"])
        self.assertEqual(int(button.grid_info()["row"]), int(start_two.grid_info()["row"]) + 1)
        button.invoke()
        self.assertTrue(self.app.fields[0].paused_windows[1].get())
        self.assertEqual(button.cget("text"), "Paused")
        self.assertEqual(button.cget("fg_color"), "#b63e4b")
        self.assertTrue(self.app.dirty)
        self.assertTrue(self.app.save())
        self.assertTrue(load_zones(self.path)[0].paused_windows[1])
        self.app.schedule_view_button.invoke()
        self.assertEqual(self.app.window_pause_buttons[1].cget("text"), "Paused")
        self.app.select_zone(1)
        self.assertTrue(all(button.cget("text") == "Not Paused" for button in self.app.window_pause_buttons))
        self.app.select_zone(0)
        self.assertEqual(self.app.window_pause_buttons[1].cget("text"), "Paused")
        self.assertEqual(len(self.app.window_pause_buttons), 4)

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
        self.assertEqual(self.app.manual_button.cget("text"), "RUN ZONE")
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
        self.app.schedule_view_button.invoke()
        for size in ("940x760", "940x620"):
            with self.subTest(size=size):
                self.app.geometry(size)
                self.app.update()
                for widget in (
                    self.app.manual_button,
                    self.app.stop_all_button, self.app.save_button, self.app.backup_button,
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
                self.app.editor.master.yview_moveto(1)
                self.app.update()
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
                for control in (self.app.pump_setting, self.app.rain_setting):
                    self.assertGreaterEqual(control.winfo_rooty(), viewport.winfo_rooty())
                    self.assertLessEqual(
                        control.winfo_rooty() + control.winfo_height(),
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
