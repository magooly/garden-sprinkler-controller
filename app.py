"""Dark-mode desktop interface for the garden sprinkler controller."""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable

import customtkinter as ctk
import system_backup

from controller import (
    DAY_NAMES,
    MAX_ZONE_COUNT,
    MAX_MANUAL_MINUTES,
    WINDOW_COUNT,
    Controller,
    Zone,
    copy_day_times,
    decode_zones,
    load_zones,
    load_rain_pause,
    next_scheduled_start,
    save_rain_pause,
    save_zones,
    parse_time,
    overlapping_windows,
    validate_zones,
)
from waveshare_relay import WaveshareRelays
from service import DEFAULT_CONFIG, default_socket
from service_client import ServiceClient
from schedule_overview import ScheduleOverview, WeeklyOverview
from timetable_profiles import RECOVERY_PROFILE, TimetableProfiles, profile_name, timetable

GREEN = "#37cc87"
RUNNING_RED = "#ff2020"
SCHEDULED_GREEN = "#20ff20"
GREY = "#8b98ab"
RED = "#b63e4b"
WINDOW_COLORS = (
    ("#283c50", "#526e89"),
    ("#2e433b", "#587b69"),
    ("#49402e", "#87734e"),
    ("#40344c", "#78618b"),
)


def display_time(value: str) -> str:
    if not value:
        return ""
    minutes = parse_time(value)
    return f"{minutes // 60:02}:{minutes % 60:02}"


def format_history_event(event: dict) -> str:
    try:
        date = datetime.fromisoformat(event["timestamp"])
        timestamp = date.strftime("%d/%m/%Y %H:%M" if date.year != datetime.now().year else "%d/%m %H:%M")
    except (TypeError, ValueError):
        timestamp = f"Invalid timestamp: {event['timestamp']}"
    channel = event["channel"]
    subject = "System" if channel is None else f"Channel {channel + 1}, {event['name']}"
    return f"{timestamp} - {subject} - {event['event']} - {event['reason']}"


def overlap_notices(row: tuple[str, ...]) -> dict[tuple[int, int], str]:
    notices = {
        pair: f"Start/Stop group {pair[0] + 1} overlaps Start/Stop group {pair[1] + 1}."
        for pair in overlapping_windows(row)
    }
    complete_windows: list[tuple[int, int, int]] = []
    for slot, (start, stop) in enumerate(zip(row[::2], row[1::2])):
        if not start or not stop:
            continue
        try:
            begin, end = parse_time(start), parse_time(stop)
        except ValueError:
            continue
        if end > begin:
            complete_windows.append((slot, begin, end))
    for slot, start in enumerate(row[::2]):
        if not start or row[slot * 2 + 1]:
            continue
        try:
            start_minute = parse_time(start)
        except ValueError:
            continue
        for other, begin, end in complete_windows:
            if other != slot and begin <= start_minute < end:
                pair = tuple(sorted((slot, other)))
                notices[pair] = (
                    f"Start/Stop group {slot + 1} start time falls within "
                    f"Start/Stop group {other + 1}."
                )
    return notices


@dataclass
class ZoneFields:
    name: tk.StringVar
    enabled: tk.BooleanVar
    schedule: list[list[tk.StringVar]]
    paused_windows: list[tk.BooleanVar]
    rain_pause_exempt: tk.BooleanVar
    pump_exempt: tk.BooleanVar
    max_manual_minutes: tk.StringVar
    continuous_reminder_minutes: tk.StringVar

    def manual_maximum(self) -> int | None:
        maximum = self.max_manual_minutes.get().strip()
        if maximum and (not maximum.isascii() or not maximum.isdecimal()):
            raise ValueError("Manual maximum must be blank or 1 to 1440 minutes.")
        value = int(maximum) if maximum else None
        if value is not None and not 1 <= value <= MAX_MANUAL_MINUTES:
            raise ValueError("Manual maximum must be blank or 1 to 1440 minutes.")
        return value

    def read(self) -> Zone:
        reminder = self.continuous_reminder_minutes.get().strip()
        if reminder and (not reminder.isascii() or not reminder.isdecimal() or len(reminder) > 4):
            raise ValueError("Continuous reminder must be blank or 1 to 1440 minutes.")
        return Zone(
            self.name.get().strip(),
            self.enabled.get(),
            tuple(
                tuple(display_time(time.get().strip()) for time in day)
                for day in self.schedule
            ),
            tuple(paused.get() for paused in self.paused_windows),
            self.rain_pause_exempt.get(),
            self.pump_exempt.get(),
            self.manual_maximum(),
            int(reminder) if reminder else None,
        )


class SprinklerApp(ctk.CTk):
    AUTOSAVE_DELAY_MS = 1000

    def __init__(
        self,
        settings_path: Path,
        backend: WaveshareRelays | ServiceClient,
    ) -> None:
        super().__init__()
        self.withdraw()
        self.title("Garden Sprinkler Controller")
        self.geometry("940x760")
        self.minsize(940, 620)
        self.settings_path = settings_path
        self.selected = 0
        self.schedule_day = tk.StringVar(self, DAY_NAMES[datetime.now().weekday()])
        self.schedule_week_view = False
        self.timer: str | None = None
        self.autosave_timer: str | None = None
        self.dirty = False
        self.save_status = "Ready"
        self.faulted = False
        self.syncing = False
        self.service_error = ""
        self.acknowledged_overlaps: set[tuple[int, int, int, int]] = set()
        self.remote = isinstance(backend, ServiceClient)
        self.overview: ScheduleOverview | None = None
        self.weekly_overview: WeeklyOverview | None = None
        self.profile_window: ctk.CTkToplevel | None = None
        self.history_window: ctk.CTkToplevel | None = None
        self.running_window: ctk.CTkToplevel | None = None
        self.profile_refresh: Callable[[], None] | None = None
        self.relay = backend
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            zones = backend.zones if isinstance(backend, ServiceClient) else load_zones(settings_path)
            rain_paused = backend.rain_paused if isinstance(backend, ServiceClient) else load_rain_pause(settings_path)
            self.timetables = None if self.remote else TimetableProfiles(settings_path)
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "Cannot load settings",
                f"{settings_path}\n\n{exc}\n\nThe file has not been changed. "
                "Fix it or select a different path using --config.",
                parent=self,
            )
            self.destroy()
            raise SystemExit(1) from exc
        try:
            # Startup intentionally turns every physical relay OFF before
            # applying routines that may be active at the current time.
            self.controller: Controller | ServiceClient
            if isinstance(backend, ServiceClient):
                self.controller = backend
                self.edit_revision = backend.revision
            else:
                self.controller = Controller(zones, backend, rain_paused=rain_paused)
        except ExceptionGroup as exc:
            logging.exception("Relay startup failed")
            messagebox.showerror(
                "Relay startup failed",
                f"{self._error_details(exc)}\n\n"
                "OFF was attempted on every channel, but output states may be unknown. "
                "Check the device and disconnect valve power if necessary.",
                parent=self,
            )
            self.destroy()
            raise SystemExit(1) from exc
        self.fields = [
            ZoneFields(
                tk.StringVar(self, zone.name),
                tk.BooleanVar(self, zone.enabled),
                [
                    [tk.StringVar(self, display_time(value)) for value in day]
                    for day in zone.schedule
                ],
                [tk.BooleanVar(self, paused) for paused in zone.paused_windows],
                tk.BooleanVar(self, zone.rain_pause_exempt),
                tk.BooleanVar(self, zone.pump_exempt),
                tk.StringVar(self, str(zone.max_manual_minutes) if zone.max_manual_minutes else ""),
                tk.StringVar(self, str(zone.continuous_reminder_minutes) if zone.continuous_reminder_minutes else ""),
            )
            for zone in zones
        ]
        self.window_pause_buttons: list[ctk.CTkButton] = []
        self._build_layout()
        for fields in self.fields:
            for variable in (
                fields.name,
                fields.enabled,
                fields.rain_pause_exempt,
                fields.pump_exempt,
                fields.max_manual_minutes,
                fields.continuous_reminder_minutes,
                *(time for day in fields.schedule for time in day),
                *fields.paused_windows,
            ):
                variable.trace_add("write", self._edited)
        self.select_zone(0)
        self.deiconify()
        self._tick()

    def _scroll_editor(self, event: tk.Event) -> None:
        if self.editor.check_if_master_is_canvas(event.widget):
            self.editor.master.yview_scroll(-3 if event.num == 4 else 3, "units")

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="#172638", corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(
            header, text="16 Station Relay Controller", font=("", 20, "bold")
        ).pack(side="left", padx=20, pady=8)
        self.connection_label = ctk.CTkLabel(
            header,
            text="",
            text_color="#ffcb70",
        )
        self.connection_label.pack(side="right", padx=(12, 24))
        self.connection_button = ctk.CTkButton(
            header,
            text="Read Relay Status",
            command=self.read_relay_status,
            width=160,
        )
        self.connection_button.pack(side="right", padx=8)

        sidebar = ctk.CTkFrame(self, width=250, corner_radius=0)
        sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        sidebar.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(sidebar, text="RELAY ZONES", font=("", 14, "bold")).grid(
            row=0, column=0, columnspan=4, padx=20, pady=(10, 6), sticky="w"
        )
        self.zone_buttons: list[ctk.CTkButton] = []
        self.zone_dots: list[ctk.CTkLabel] = []
        self.rain_pause_exempt_buttons: list[ctk.CTkButton] = []
        self.pump_exempt_buttons: list[ctk.CTkButton] = []
        self.board_selector = ctk.CTkSegmentedButton(
            sidebar, values=["Channels 1-8", "Channels 9-16"],
            command=self.show_board,
        )
        self.board_selector.grid(row=1, column=0, columnspan=4, padx=8, pady=4)
        self.board_selector.set("Channels 1-8")
        for channel in range(MAX_ZONE_COUNT):
            dot = ctk.CTkLabel(
                sidebar,
                text="\u25cf",
                text_color=GREY,
                width=24,
                font=("", 22, "bold"),
            )
            dot.grid(row=channel % 8 + 2, column=0, padx=(12, 0))
            exempt_button = ctk.CTkButton(
                sidebar,
                text="R",
                width=28,
                height=30,
                fg_color="transparent",
                hover_color="#35465a",
                text_color=GREY,
                command=lambda index=channel: self.toggle_rain_pause_exemption(index),
            )
            exempt_button.grid(row=channel % 8 + 2, column=1, padx=(2, 2), pady=3)
            pump_button = ctk.CTkButton(
                sidebar, text="P", width=28, height=30,
                fg_color="transparent", hover_color="#35465a", text_color=GREY,
                command=lambda index=channel: self.toggle_pump_exemption(index),
            )
            pump_button.grid(row=channel % 8 + 2, column=2, padx=2, pady=3)
            button = ctk.CTkButton(
                sidebar,
                text="",
                anchor="w",
                width=156,
                height=35,
                command=lambda index=channel: self.select_zone(index),
            )
            button.grid(row=channel % 8 + 2, column=3, padx=(2, 8), pady=3, sticky="ew")
            self.zone_buttons.append(button)
            self.zone_dots.append(dot)
            self.rain_pause_exempt_buttons.append(exempt_button)
            self.pump_exempt_buttons.append(pump_button)
            if channel >= 8:
                for widget in (dot, exempt_button, pump_button, button):
                    widget.grid_remove()
        ctk.CTkLabel(
            sidebar,
            text="Click R to exempt; red R- stays active in Rain Pause\n"
            "P: uses pump / P-: excluded from pump\n"
            "Red dot: watering / Green: scheduled / Grey: inactive",
            text_color=GREY,
            justify="left",
            wraplength=220,
        ).grid(row=10, column=0, columnspan=4, padx=14, pady=10, sticky="w")
        self.rain_pause_var = tk.BooleanVar(self, self.controller.rain_paused)
        self.rain_pause_switch = ctk.CTkSwitch(
            sidebar, text="RAIN PAUSE", variable=self.rain_pause_var,
            command=self.toggle_rain_pause, progress_color="#87734e",
        )
        self.rain_pause_switch.grid(
            row=11, column=0, columnspan=4, padx=20, pady=(8, 4), sticky="w"
        )
        ctk.CTkLabel(
            sidebar, text="Included zones OFF; R- zones stay active.\nRemembered after restart.",
            text_color=GREY, justify="left",
        ).grid(row=12, column=0, columnspan=4, padx=20, pady=(0, 12), sticky="w")

        panel = ctk.CTkFrame(self, fg_color="transparent")
        panel.grid(row=1, column=1, sticky="nsew", padx=(8, 20), pady=8)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        title_row = ctk.CTkFrame(panel, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        title_row.grid_columnconfigure(1, weight=1)
        self.heading = ctk.CTkLabel(title_row, text="", font=("", 18, "bold"), anchor="w")
        self.heading.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.status = ctk.CTkLabel(
            title_row, text="", anchor="e", text_color=GREY, wraplength=400
        )
        self.status.grid(row=0, column=1, sticky="ew")
        overview_controls = ctk.CTkFrame(panel, fg_color="transparent")
        overview_controls.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.overview_button = ctk.CTkButton(
            overview_controls, text="Daily Schedule", command=self.show_overview, width=150,
        )
        self.overview_button.pack(side="left", padx=(0, 8))
        self.weekly_button = ctk.CTkButton(
            overview_controls, text="Weekly Overview", command=self.show_weekly_overview, width=150,
        )
        self.weekly_button.pack(side="left")
        self.profiles_button = ctk.CTkButton(
            overview_controls, text="Timetables", command=self.show_profiles, width=150,
        )
        self.profiles_button.pack(side="left", padx=(8, 0))
        self.running_button = ctk.CTkButton(
            overview_controls, text="Running: 0", command=self.show_running, width=125,
        )
        self.running_button.pack(side="left", padx=(8, 0))
        self.editor = ctk.CTkScrollableFrame(panel)
        self.editor.grid(row=2, column=0, sticky="nsew")
        self.editor.grid_columnconfigure(0, weight=1)
        # Linux/X11 reports wheel movement as button presses, not MouseWheel.
        self.bind("<Button-4>", self._scroll_editor, add="+")
        self.bind("<Button-5>", self._scroll_editor, add="+")
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        actions.grid_columnconfigure(2, weight=1)
        self.manual_duration = tk.StringVar(self, "10 min")
        self.custom_duration = tk.StringVar(self, "")
        self.manual_button = ctk.CTkButton(
            actions, text="RUN ZONE", height=36, command=self.manual
        )
        self.manual_button.grid(row=0, column=2, sticky="ew")
        self.duration_menu = ctk.CTkOptionMenu(
            actions, variable=self.manual_duration, width=145,
            values=["10 min", "30 min", "1 hour", "Custom", "Until stopped"],
            command=lambda _value: self._refresh(),
        )
        self.duration_menu.grid(row=0, column=0, padx=(0, 8))
        self.duration_entry = ctk.CTkEntry(
            actions, textvariable=self.custom_duration, width=110, placeholder_text="Minutes",
        )
        self.duration_summary = ctk.CTkLabel(
            panel, text="", text_color=GREY, anchor="w", justify="left",
        )
        self.duration_summary.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.custom_duration.trace_add("write", lambda *_args: self._update_duration_summary())

        footer = ctk.CTkFrame(self, corner_radius=0)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.system_status = ctk.CTkLabel(footer, text="System: Ready", anchor="w")
        self.system_status.grid(row=0, column=0, sticky="ew", padx=20, pady=8)
        self.history_button = ctk.CTkButton(
            footer, text="Activity", command=self.show_history, width=90,
        )
        self.history_button.grid(row=0, column=1, padx=4)
        self.backup_button = ctk.CTkButton(
            footer, text="Backup", command=self.show_backup, width=80,
        )
        self.backup_button.grid(row=0, column=4, padx=(0, 8))
        self.stop_all_button = ctk.CTkButton(
            footer, text="STOP ALL", fg_color=RED, hover_color="#92313c",
            command=self.stop_all, width=120,
        )
        self.stop_all_button.grid(row=0, column=2, padx=10)
        self.save_button = ctk.CTkButton(
            footer, text="Save Settings", command=self.save, width=150
        )
        self.save_button.grid(row=0, column=3, padx=(10, 24))
        ctk.CTkLabel(
            footer,
            text="Developed by Wally Horsman \u00a9 2026  |  wrhorsman@gmail.com",
            text_color=GREY,
            font=("", 11),
        ).grid(row=1, column=0, columnspan=4, padx=20, pady=(0, 8), sticky="e")

    def show_backup(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Backup and restore")
        window.geometry("640x460")
        ctk.CTkLabel(
            window, text="Encrypted full sprinkler-system backup\n\n"
            "Includes saved settings, all timetables, Rain Pause, relay connections,\n"
            "Tapo assignments/passwords, the standard phone password, history,\n"
            "service configuration and application version information.\n"
            "Not an Ubuntu/computer image; active runs are never restored.\n\n"
            + system_backup.PASSWORD_HELP,
            justify="left", wraplength=590,
        ).pack(padx=16, pady=16)
        ctk.CTkButton(window, text="Export full backup", command=self.export_backup).pack(pady=5)
        ctk.CTkButton(window, text="Restore full backup", command=self.restore_backup).pack(pady=5)
        ctk.CTkButton(window, text="Resume operation after restore", command=self.resume_operation).pack(pady=5)

    def _backup_password(self, confirm: bool) -> str | None:
        window = ctk.CTkToplevel(self)
        window.title("Choose backup password" if confirm else "Unlock backup")
        window.geometry("580x420")
        window.transient(self)
        result: str | None = None
        ctk.CTkLabel(
            window, text=system_backup.PASSWORD_HELP if confirm else
            "Enter the password chosen when this backup was exported.\n"
            "It is not your Tapo or phone sign-in password.\n"
            "Lost backup passwords cannot be recovered.",
            wraplength=530, justify="left",
        ).pack(padx=20, pady=16)
        password = ctk.CTkEntry(window, width=400, show="*", placeholder_text="Backup password")
        password.pack(pady=6)
        confirmation = ctk.CTkEntry(window, width=400, show="*", placeholder_text="Enter it again")
        if confirm:
            confirmation.pack(pady=6)
        visible = tk.BooleanVar(window, False)

        def show_password() -> None:
            for entry in (password, confirmation):
                entry.configure(show="" if visible.get() else "*")

        ctk.CTkCheckBox(window, text="Show password", variable=visible, command=show_password).pack(pady=6)
        error = ctk.CTkLabel(window, text="", text_color="#ff9999")
        error.pack()

        def accept() -> None:
            nonlocal result
            value = password.get()
            if not value.strip() or len(value) > 1024 or (confirm and len(value) < 12):
                error.configure(text="Use 12 to 1024 characters." if confirm else "Enter the backup password.")
                return
            if confirm and value != confirmation.get():
                error.configure(text="Passwords do not match. Please enter them again.")
                return
            result = value
            window.destroy()

        ctk.CTkButton(window, text="Continue", command=accept).pack(pady=4)
        ctk.CTkButton(window, text="Cancel", command=window.destroy, fg_color="#35465a").pack(pady=4)
        window.bind("<Return>", lambda _event: accept())
        window.bind("<Escape>", lambda _event: window.destroy())
        window.wait_visibility()
        window.grab_set()
        password.focus_set()
        self.wait_window(window)
        return result

    def _backup_client(self) -> ServiceClient:
        if not isinstance(self.controller, ServiceClient) or not self.controller.full_backup_supported:
            raise ValueError("Full backups require the updated background controller service. Restart it first.")
        if self.service_error:
            raise ValueError("Controller connection is unavailable.")
        return self.controller

    def export_backup(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Export encrypted full backup", defaultextension=".sprinkler-backup",
            filetypes=[("Encrypted sprinkler backups", "*.sprinkler-backup")],
        )
        if not path:
            return
        try:
            target = Path(path)
            if target.resolve() == self.settings_path.resolve():
                raise ValueError("Choose a backup file other than the active settings file.")
            client = self._backup_client()
            password = self._backup_password(confirm=True)
            if password is None:
                return
            result = client.backup("backup_export", password=password)
            system_backup.write_private(target, json.dumps(result["archive"]).encode("utf-8"))
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Backup failed", str(error), parent=self)
            return
        messagebox.showinfo(
            "Full backup saved", result["summary"] + "\n\n"
            "Keep a copy on another device. Store the backup password separately.\n"
            "Only saved settings were included; unsaved desktop edits are not backed up.",
            parent=self,
        )

    def restore_backup(self) -> None:
        if self.faulted or self.dirty:
            messagebox.showerror(
                "Restore unavailable", "Resolve controller faults and save or discard desktop edits first.",
                parent=self,
            )
            return
        path = filedialog.askopenfilename(
            parent=self, title="Restore encrypted full backup",
            filetypes=[("Encrypted sprinkler backups", "*.sprinkler-backup")],
        )
        if not path:
            return
        try:
            source = Path(path)
            if source.stat().st_size > system_backup.MAX_TRANSPORT_BYTES:
                raise ValueError("Backup file is too large.")
            archive = json.loads(source.read_text(encoding="utf-8"))
            client = self._backup_client()
            password = self._backup_password(confirm=False)
            if password is None:
                return
            preview = client.backup("backup_preview", archive=archive, password=password)
        except (OSError, UnicodeError, ValueError, RuntimeError) as error:
            messagebox.showerror("Invalid backup", str(error), parent=self)
            return
        if not messagebox.askyesno(
            "Restore the full system?",
            preview["summary"] + "\n\n"
            "Replace this system's saved configuration and history?\n"
            "Outputs will be stopped and held OFF, including Rain Pause-exempt plugs.\n"
            "A recovery backup of the current setup will use this SAME backup password.\n"
            "Restart services afterward, inspect devices/settings, then Resume operation.\n"
            "Service unit copies require path review before installation on another computer.",
            parent=self,
        ):
            return
        try:
            client.backup("backup_restore", archive=archive, password=password, revision=preview["revision"])
        except ValueError as error:
            messagebox.showerror("Restore failed", str(error), parent=self)
            return
        except (OSError, RuntimeError, ExceptionGroup) as error:
            self._fault("Restore failed", error)
            return
        self._refresh()
        messagebox.showinfo(
            "Full backup restored — operation held",
            "All outputs are stopped. Restart the controller and phone services, then reopen this app.\n"
            "Review devices, schedules and restored Rain Pause before selecting Resume operation in Backup.\n"
            f"Recovery backup (same password): {client.recovery_backup}\n"
            f"Service/reference recovery guide: {self.settings_path.name}.restored-service-config/RECOVERY.txt\n"
            "Phone sign-in may now use the password from the restored backup.",
            parent=self,
        )

    def resume_operation(self) -> None:
        try:
            client = self._backup_client()
            if not client.operation_held:
                messagebox.showinfo("Operation", "No restore safety hold is active.", parent=self)
                return
            if not messagebox.askyesno(
                "Resume restored system?",
                "Have you checked device assignments and saved schedules?\n"
                "Resuming can switch equipment ON immediately for a current schedule.\n"
                "The restored Rain Pause state and exemptions will apply.",
                parent=self,
            ):
                return
            client.command("resume_operation")
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot resume operation", str(error), parent=self)
        self._refresh()

    def show_history(self) -> None:
        if not isinstance(self.controller, ServiceClient):
            messagebox.showinfo("Activity history", "Activity history requires the background service.", parent=self)
            return
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            return
        window = ctk.CTkToplevel(self)
        self.history_window = window
        window.title("Activity history")
        window.geometry("760x500")
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        label = ctk.CTkLabel(window, text="", anchor="w")
        label.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        contents = ctk.CTkTextbox(window, wrap="word")
        contents.grid(row=1, column=0, sticky="nsew", padx=12)
        controls = ctk.CTkFrame(window, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        choices = ["All activity", "System", "Faults"] + [
            f"Channel {index + 1}" + (
                f": {self.controller.zones[index].name}" if index < len(self.controller.zones) else ""
            )
            for index in range(MAX_ZONE_COUNT)
        ]
        category = tk.StringVar(window, "All activity")
        filter_menu = ctk.CTkOptionMenu(
            controls, variable=category, values=choices,
            state="normal" if self.controller.history_filters_supported else "disabled",
            command=lambda _value: load(0),
        )
        filter_menu.pack(side="right")
        offset = 0

        def load(value: int) -> None:
            nonlocal offset
            selected = choices.index(category.get())
            category_value = ("all", "system", "faults")[selected] if selected < 3 else str(selected - 3)
            try:
                page = self.controller.history(value) if category_value == "all" else self.controller.history(value, category_value)
            except (OSError, ValueError) as error:
                label.configure(text="History unavailable: " + str(error))
                return
            offset = value
            events = page["events"]
            warning = page["warning"]
            label.configure(
                text=f"Events {min(offset + 1, page['total'])}-{offset + len(events)} of {page['total']}"
                + (" / " + warning if warning else "")
            )
            contents.configure(state="normal")
            contents.delete("1.0", "end")
            for event in events:
                contents.insert("end", format_history_event(event) + "\n")
            if not events:
                contents.insert("end", "No recorded activity.")
            contents.configure(state="disabled")
            contents.yview_moveto(0)
            newer.configure(state="normal" if offset else "disabled")
            older.configure(state="normal" if offset + page["page_size"] < page["total"] else "disabled")

        newer = ctk.CTkButton(controls, text="Newer", command=lambda: load(max(0, offset - 25)))
        newer.pack(side="left", padx=(0, 8))
        older = ctk.CTkButton(controls, text="Older", command=lambda: load(offset + 25))
        older.pack(side="left", padx=(0, 8))
        ctk.CTkButton(controls, text="Refresh", command=lambda: load(0)).pack(side="left")
        load(0)

    def overview_snapshot(self) -> tuple[list[Zone], int | None, bool, bool, bool, int]:
        return (
            [fields.read() for fields in self.fields[:self.controller.channel_count]],
            self.controller.master_channel,
            self.dirty, self.controller.rain_paused, self.faulted,
            8 if self.board_selector.get() == "Channels 9-16" else 0,
        )

    def timetable_status(self) -> dict:
        bank = 8 if self.board_selector.get() == "Channels 9-16" else 0
        if bank >= self.controller.channel_count:
            return {"names": [], "selected": "", "modified": False}
        if isinstance(self.controller, ServiceClient):
            status = (self.controller.timetable_groups or {}).get(
                str(bank), {"names": [], "selected": "", "modified": False},
            )
        else:
            assert self.timetables is not None
            status = self.timetables.status(self.controller.zones, bank)
        if self.dirty and status["selected"]:
            try:
                modified = (
                    timetable([fields.read() for fields in self.fields[bank:bank + 8]])
                    != timetable(self.controller.zones[bank:bank + 8])
                )
            except ValueError:
                modified = True
            status = {**status, "modified": status["modified"] or modified}
        return status

    def show_profiles(self) -> None:
        if self.profile_window is not None and self.profile_window.winfo_exists():
            self.profile_window.deiconify()
            self.profile_window.lift()
            return
        window = ctk.CTkToplevel(self)
        self.profile_window = window
        window.title("Timetable profiles")
        window.geometry("500x300")
        description = ctk.CTkLabel(
            window, text="",
            wraplength=440, justify="left",
        )
        description.pack(padx=16, pady=12)
        choice = tk.StringVar(window, "")
        menu = ctk.CTkOptionMenu(window, variable=choice, values=["No saved profiles"])
        menu.pack(padx=16, pady=8)
        label = ctk.CTkLabel(window, text="", wraplength=440)
        label.pack(padx=16, pady=4)
        last_bank = ""

        def refresh() -> None:
            nonlocal last_bank
            bank = self.board_selector.get()
            window.title(f"Timetable profiles - {bank}")
            description.configure(
                text=f"{bank} timetables only.\n"
                "Save or load schedules for this group; the other group's timetable\n"
                "is unchanged. Hardware, names and exclusions stay unchanged.",
            )
            status = self.timetable_status()
            names = status["names"]
            values = names or ["No saved profiles"]
            if menu.cget("values") != values:
                menu.configure(values=values)
            if bank != last_bank or choice.get() not in names:
                choice.set(status["selected"] or (names[0] if names else "No saved profiles"))
            last_bank = bank
            label.configure(
                text=("Selected: " + status["selected"]
                      + (" (modified)" if status["modified"] else ""))
                if status["selected"] else "No named timetable selected",
            )

        def save_as() -> None:
            name = ctk.CTkInputDialog(text="Timetable name:", title="Save timetable").get_input()
            if name is not None and self.save_profile(name):
                refresh()

        def load() -> None:
            if self.load_profile(choice.get()):
                refresh()

        ctk.CTkButton(window, text="Save current as...", command=save_as).pack(pady=4)
        ctk.CTkButton(window, text="Load selected timetable", command=load).pack(pady=4)
        self.profile_refresh = refresh
        refresh()

    def _refresh_timetable_controls(self) -> dict:
        status = self.timetable_status()
        if (
            self.profile_window is not None and self.profile_window.winfo_exists()
            and self.profile_refresh is not None
        ):
            self.profile_refresh()
        selected = status["selected"]
        text = "Timetables"
        if selected:
            display = selected if len(selected) <= 16 else selected[:13] + "..."
            text = f"Timetable: {display}"
            if status["modified"]:
                text += " (modified)"
        bank = 8 if self.board_selector.get() == "Channels 9-16" else 0
        self.profiles_button.configure(
            text=text, state="disabled" if self.faulted or bank >= self.controller.channel_count else "normal",
        )
        return status

    def save_profile(self, name: str) -> bool:
        bank = 8 if self.board_selector.get() == "Channels 9-16" else 0
        try:
            name = profile_name(name)
            if name == RECOVERY_PROFILE:
                raise ValueError(f'"{RECOVERY_PROFILE}" is reserved for automatic recovery.')
        except ValueError as exc:
            messagebox.showerror("Cannot save timetable", str(exc), parent=self)
            return False
        if name in self.timetable_status()["names"] and not messagebox.askyesno(
            "Replace timetable?", f'Replace "{name}" for channels {bank + 1}-{bank + 8}?', parent=self,
        ):
            return False
        if self.dirty and not self.save(bank=bank):
            return False
        try:
            if isinstance(self.controller, ServiceClient):
                self.controller.revision = self.edit_revision
                self.controller.save_profile(name, bank)
                self.edit_revision = self.controller.revision
            else:
                assert self.timetables is not None
                self.timetables.save(name, self.controller.zones, bank)
        except ValueError as exc:
            messagebox.showerror("Cannot save timetable", str(exc), parent=self)
            return False
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Cannot save timetable", exc)
            return False
        self._refresh()
        return True

    def load_profile(self, name: str) -> bool:
        bank = 8 if self.board_selector.get() == "Channels 9-16" else 0
        if not messagebox.askyesno(
            "Load timetable?",
            f'Load "{name}" for channels {bank + 1}-{bank + 8} only?\n'
            "The other channel group's timetable will not be loaded or replaced.\n\n"
            "Scheduled relays may start or stop immediately. Rain Pause stays unchanged.\n"
            f'This group\'s current timetable will be saved as "{RECOVERY_PROFILE}".',
            parent=self,
        ):
            return False
        if self.dirty and not self.save(bank=bank):
            return False
        try:
            if isinstance(self.controller, ServiceClient):
                self.controller.revision = self.edit_revision
                self.controller.load_profile(name, bank)
                self.edit_revision = self.controller.revision
            else:
                assert self.timetables is not None
                zones = self.timetables.load(name, self.controller.zones, bank)
                save_zones(self.settings_path, zones)
                self.controller.apply(zones, datetime.now())
        except ValueError as exc:
            messagebox.showerror("Cannot load timetable", str(exc), parent=self)
            return False
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Cannot load timetable", exc)
            return False
        self.syncing = True
        try:
            for fields, zone in zip(
                self.fields[bank:bank + 8], self.controller.zones[bank:bank + 8],
            ):
                fields.enabled.set(zone.enabled)
                for variables, row in zip(fields.schedule, zone.schedule):
                    for variable, value in zip(variables, row):
                        variable.set(value)
                for variable, value in zip(fields.paused_windows, zone.paused_windows):
                    variable.set(value)
        finally:
            self.syncing = False
        self._update_window_pause_buttons()
        self._validate()
        self._refresh()
        return True

    def show_weekly_overview(self) -> None:
        if self.weekly_overview is not None and self.weekly_overview.winfo_exists():
            self.weekly_overview.refresh()
            self.weekly_overview.deiconify()
            self.weekly_overview.lift()
            return
        self.weekly_overview = WeeklyOverview(self, self.overview_snapshot, self.select_zone)

    def show_overview(self) -> None:
        if self.overview is not None and self.overview.winfo_exists():
            self.overview.refresh()
            self.overview.deiconify()
            self.overview.lift()
            return
        self.overview = ScheduleOverview(
            self,
            self.overview_snapshot,
            self.select_zone, datetime.now().weekday(),
        )

    def toggle_rain_pause_exemption(self, channel: int) -> None:
        if (channel >= self.controller.channel_count
                or channel == self.controller.master_channel or self.faulted):
            return
        variable = self.fields[channel].rain_pause_exempt
        variable.set(not variable.get())

    def show_board(self, board: str) -> None:
        first = 8 if board == "Channels 9-16" else 0
        self.board_selector.set(board)
        if first < self.controller.channel_count and not first <= self.selected < first + 8:
            self.select_zone(first)
            return
        for channel in range(MAX_ZONE_COUNT):
            for widget in (
                self.zone_buttons[channel], self.zone_dots[channel],
                self.rain_pause_exempt_buttons[channel],
                self.pump_exempt_buttons[channel],
            ):
                if first <= channel < first + 8:
                    widget.grid()
                else:
                    widget.grid_remove()
        if self.overview is not None and self.overview.winfo_exists():
            self.overview.refresh()
        if self.weekly_overview is not None and self.weekly_overview.winfo_exists():
            self.weekly_overview.refresh()
        self._refresh_timetable_controls()

    def toggle_pump_exemption(self, channel: int) -> None:
        master = self.controller.master_channel
        if self.faulted or master is None or channel not in range(master):
            return
        variable = self.fields[channel].pump_exempt
        variable.set(not variable.get())

    def select_zone(self, channel: int) -> None:
        if channel >= self.controller.channel_count:
            return
        board = "Channels 1-8" if channel < 8 else "Channels 9-16"
        self.selected = channel
        self.board_selector.set(board)
        self.show_board(board)
        for widget in self.editor.winfo_children():
            widget.destroy()
        self.channel_summary: ctk.CTkLabel | None = None
        self.pump_setting: ctk.CTkSwitch | None = None
        self.rain_setting: ctk.CTkSwitch | None = None
        self.window_pause_buttons = []
        self.schedule_entries: dict[tuple[int, int], ctk.CTkEntry] = {}
        if channel == self.controller.master_channel:
            ctk.CTkLabel(
                self.editor, text="CHANNEL 8 / MASTER PUMP", font=("", 18, "bold"),
            ).grid(row=0, column=0, padx=20, pady=(20, 10), sticky="w")
            ctk.CTkLabel(
                self.editor,
                text="Automatically ON when a non-excluded zone (1-7) is watering.\n"
                "The valve opens before the pump starts. The pump stops before\n"
                "the last valve closes. No independent schedule or manual watering.\n\n"
                "The previous channel 8 name and schedule are preserved but ignored.",
                justify="left", anchor="w", wraplength=650,
            ).grid(row=1, column=0, padx=20, pady=10, sticky="ew")
            self.validation = ctk.CTkLabel(self.editor, text="", anchor="w")
            self.validation.grid(row=2, column=0, padx=20, pady=10, sticky="ew")
            self._validate()
            self._refresh()
            return
        fields = self.fields[channel]
        ctk.CTkLabel(self.editor, text="RELAY / GARDEN NAME", anchor="w").grid(
            row=0, column=0, padx=12, pady=(4, 0), sticky="w"
        )
        ctk.CTkEntry(
            self.editor,
            textvariable=fields.name,
            placeholder_text="For example: Front Lawn",
        ).grid(
            row=1, column=0, columnspan=2, padx=12, sticky="ew"
        )
        ctk.CTkSwitch(
            self.editor, text="Enable Routine for this Zone", variable=fields.enabled
        ).grid(row=0, column=1, padx=12, pady=(4, 0), sticky="e")
        self.channel_summary = ctk.CTkLabel(self.editor, text="", anchor="w", wraplength=650)
        self.channel_summary.grid(row=2, column=0, columnspan=2, padx=12, sticky="ew")
        options = ctk.CTkFrame(self.editor, fg_color="transparent")
        options.grid(row=8, column=0, columnspan=2, padx=12, pady=(8, 2), sticky="ew")
        options.grid_columnconfigure(0, weight=1)
        master = self.controller.master_channel
        if master is not None and channel < master:
            self.pump_setting = ctk.CTkSwitch(
                options, text="", variable=fields.pump_exempt,
            )
            self.pump_setting.grid(row=1, column=0, sticky="w", pady=(5, 0))
            ctk.CTkLabel(
                options, text="Excluded channels do not start or keep the master pump running.",
                text_color=GREY, anchor="w", wraplength=650,
            ).grid(row=2, column=0, sticky="ew")
        else:
            ctk.CTkLabel(
                options, text="Pump: independent - this channel does not control the master pump.",
                text_color=GREY, anchor="w", wraplength=650,
            ).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.rain_setting = ctk.CTkSwitch(
            options, text="", variable=fields.rain_pause_exempt,
        )
        self.rain_setting.grid(row=3, column=0, sticky="w", pady=(5, 0))
        ctk.CTkLabel(
            options, text="Excluded channels can run on schedule or manually during Rain Pause.",
            text_color=GREY, anchor="w", wraplength=650,
        ).grid(row=4, column=0, sticky="ew")
        maximum_row = ctk.CTkFrame(options, fg_color="transparent")
        maximum_row.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ctk.CTkLabel(maximum_row, text="Maximum manual run (minutes):").pack(side="left", padx=(0, 8))
        self.manual_max_entry = ctk.CTkEntry(
            maximum_row, textvariable=fields.max_manual_minutes, width=100,
            placeholder_text="No limit",
        )
        self.manual_max_entry.pack(side="left")
        ctk.CTkLabel(
            options, text="Blank: no channel limit. Set 1-1440 minutes to disable Until stopped. "
            "Schedules are not limited. Adding or lowering a limit stops an active manual run.",
            text_color=GREY, anchor="w", wraplength=650, justify="left",
        ).grid(row=6, column=0, sticky="ew")
        reminder_row = ctk.CTkFrame(options, fg_color="transparent")
        reminder_row.grid(row=7, column=0, sticky="w", pady=(8, 0))
        ctk.CTkLabel(reminder_row, text="Continuous run reminder (minutes):").pack(side="left", padx=(0, 8))
        self.reminder_entry = ctk.CTkEntry(
            reminder_row, textvariable=fields.continuous_reminder_minutes, width=100,
            placeholder_text="Off",
        )
        self.reminder_entry.pack(side="left")
        self.reminder_help = ctk.CTkLabel(
            options, text="Blank: off. Shows an on-screen reminder after 1-1440 minutes; does not stop the channel.",
            text_color=GREY, anchor="w", wraplength=650,
        )
        self.reminder_help.grid(row=8, column=0, sticky="ew")
        if isinstance(self.controller, ServiceClient) and self.controller.tapo is not None and channel >= 8:
            plug = next(item for item in self.controller.tapo if item["channel"] == channel)
            details = (
                f"Tapo P110M / {plug['host']} / MAC {plug['mac']}"
                if plug["assigned"] else "Tapo P110M: unassigned - no switching or schedules will run."
            )
            ctk.CTkLabel(options, text=details, text_color="#ffcb70", anchor="w", wraplength=650).grid(
                row=9, column=0, sticky="ew", pady=(8, 0),
            )
            ctk.CTkLabel(
                options, text="Pair in Tapo first. Run tapo_plugs.py credentials, discover and assign\n"
                "on this computer to connect a plug. Keep its LAN IP reserved in your router.",
                text_color=GREY, anchor="w", justify="left", wraplength=650,
            ).grid(row=10, column=0, sticky="ew")
        ctk.CTkLabel(self.editor, text="DAILY SCHEDULE - UP TO FOUR WINDOWS", anchor="w").grid(
            row=3, column=0, columnspan=2, padx=12, pady=(6, 0), sticky="ew"
        )
        copy_controls = ctk.CTkFrame(self.editor, fg_color="transparent")
        copy_controls.grid(row=4, column=0, columnspan=2, padx=12, pady=(4, 0), sticky="w")
        self.schedule_day_menu = ctk.CTkOptionMenu(
            copy_controls, variable=self.schedule_day, values=list(DAY_NAMES), width=85,
            command=lambda _day: self._update_schedule_view(),
        )
        self.schedule_day_menu.pack(side="left", padx=(0, 12))
        self.schedule_view_button = ctk.CTkButton(
            copy_controls, text="See week view", width=140,
            command=self._toggle_schedule_view,
        )
        self.schedule_view_button.pack(side="left", padx=(0, 12))
        copy_var = tk.BooleanVar(self, False)

        def copy_selected_day() -> None:
            if not copy_var.get():
                return
            index = DAY_NAMES.index(self.schedule_day.get())
            current_schedule = tuple(
                tuple(variable.get().strip() for variable in day)
                for day in fields.schedule
            )
            try:
                copied_schedule = copy_day_times(current_schedule, index)
            except ValueError as exc:
                messagebox.showerror("Cannot copy schedule", str(exc), parent=self)
            else:
                for day_index, day in enumerate(fields.schedule):
                    for slot, variable in enumerate(day):
                        variable.set(copied_schedule[day_index][slot])
            finally:
                copy_var.set(False)

        self.copy_day_button = ctk.CTkCheckBox(
            copy_controls,
            text="Copy selected day's times to all days",
            variable=copy_var,
            command=copy_selected_day,
        )
        self.copy_day_button.pack(side="left")
        times = ctk.CTkFrame(self.editor, fg_color="transparent")
        times.grid(row=5, column=0, columnspan=2, padx=12, pady=(4, 4), sticky="ew")
        times.grid_columnconfigure(0, weight=0)
        for column in range(1, WINDOW_COUNT * 2 + 1):
            times.grid_columnconfigure(column, weight=0)
        ctk.CTkLabel(times, text="Day", anchor="w").grid(
            row=0, column=0, padx=(0, 8), pady=(0, 5), sticky="w"
        )
        for window in range(WINDOW_COUNT):
            start_column = window * 2 + 1
            background, border = WINDOW_COLORS[window]
            ctk.CTkLabel(
                times, text=f"Start {window + 1}", width=60, height=18, font=("", 11),
                fg_color=background, corner_radius=5,
            ).grid(row=0, column=start_column, padx=4, pady=(0, 0), sticky="ew")
            pause_button = ctk.CTkButton(
                times, text="Not Paused", width=60, height=20, font=("", 10),
                command=lambda index=window: self._toggle_window_pause(index),
            )
            pause_button.grid(
                row=1, column=start_column, padx=4, pady=(0, 1), sticky="ew"
            )
            self.window_pause_buttons.append(pause_button)
            ctk.CTkLabel(
                times, text=f"Stop {window + 1}", width=60, fg_color=background,
                text_color="#edf0f4", corner_radius=5,
            ).grid(
                row=0, column=start_column + 1, rowspan=2, padx=4, pady=(0, 1),
                sticky="nsew",
            )
        for day_index, day_name in enumerate(DAY_NAMES):
            ctk.CTkLabel(times, text=day_name, anchor="w").grid(
                row=day_index + 2, column=0, padx=(0, 8), pady=1, sticky="w"
            )
            for slot, variable in enumerate(fields.schedule[day_index], start=1):
                background, border = WINDOW_COLORS[(slot - 1) // 2]
                entry = ctk.CTkEntry(
                    times, textvariable=variable, width=60, height=26,
                    fg_color=background, border_color=border, text_color="#edf0f4",
                )
                entry.grid(
                    row=day_index + 2, column=slot, padx=4, pady=0, sticky="ew"
                )
                entry.bind(
                    "<FocusOut>",
                    lambda _event, time_variable=variable: self._format_time_field(time_variable),
                )
                if (slot - 1) % 2:
                    click_handler = (
                        lambda _event, day=day_index, window=(slot - 1) // 2:
                        self._overlap_stop_clicked(day, window)
                    )
                    entry.bind(
                        "<Button-1>",
                        click_handler,
                        add="+",
                    )
                    entry._canvas.bind("<Button-1>", click_handler, add="+")
                self.schedule_entries[day_index, slot - 1] = entry
        self.schedule_rows = [
            [widget for widget in times.winfo_children()
             if int(widget.grid_info()["row"]) == day + 2]
            for day in range(len(DAY_NAMES))
        ]
        self._update_schedule_view()
        self._update_window_pause_buttons()
        self.validation = ctk.CTkLabel(self.editor, text="", anchor="w", wraplength=700)
        self.validation.grid(row=6, column=0, columnspan=2, padx=12, pady=(0, 4), sticky="ew")
        ctk.CTkLabel(
            self.editor,
            text="Enter 815, 0815, or 08:15 (24-hour time). Leave a pair blank to skip it. "
            "Windows may be entered in any order but cannot overlap. Use the Not Paused / Paused "
            "buttons below Start to pause/resume that group across all weekdays; changes "
            "save automatically about one second after you stop editing. "
            "Red borders identify overlapping groups. Click a conflicting Stop field for details. "
            "Pump and Rain Pause settings are below.",
            text_color=GREY,
            anchor="w",
            wraplength=700,
        ).grid(row=7, column=0, columnspan=2, padx=12, pady=(0, 4), sticky="ew")
        self._validate()
        self._refresh()

    def _toggle_schedule_view(self) -> None:
        self.schedule_week_view = not self.schedule_week_view
        self._update_schedule_view()

    def _update_schedule_view(self) -> None:
        selected_day = DAY_NAMES.index(self.schedule_day.get())
        for day, widgets in enumerate(self.schedule_rows):
            for widget in widgets:
                if self.schedule_week_view or day == selected_day:
                    widget.grid()
                else:
                    widget.grid_remove()
        self.schedule_view_button.configure(
            text="See one day view" if self.schedule_week_view else "See week view",
        )

    def _edited(self, *_args: str) -> None:
        if self.syncing:
            return
        self.dirty = True
        self.save_status = "Unsaved changes"
        self._update_window_pause_buttons()
        self._validate()
        self._schedule_autosave()
        self._refresh()

    def _schedule_autosave(self) -> None:
        if self.autosave_timer is not None:
            self.after_cancel(self.autosave_timer)
            self.autosave_timer = None
        if self.dirty and not self.faulted:
            try:
                validate_zones([fields.read() for fields in self.fields])
            except ValueError:
                return
            self.autosave_timer = self.after(
                self.AUTOSAVE_DELAY_MS, self._autosave
            )

    def _autosave(self) -> None:
        self.autosave_timer = None
        if self.dirty and not self.faulted:
            self.save(automatic=True)

    def _toggle_window_pause(self, window: int) -> None:
        paused = self.fields[self.selected].paused_windows[window]
        paused.set(not paused.get())

    def _format_time_field(self, variable: tk.StringVar) -> None:
        value = variable.get().strip()
        if not value:
            variable.set("")
            return
        try:
            formatted = display_time(value)
        except ValueError:
            return
        variable.set(formatted)

    def _update_window_pause_buttons(self) -> None:
        if self.selected == self.controller.master_channel:
            return
        for button, variable in zip(
            self.window_pause_buttons, self.fields[self.selected].paused_windows
        ):
            button.configure(
                text="Paused" if variable.get() else "Not Paused",
                fg_color=RED if variable.get() else "#35465a",
                hover_color="#92313c" if variable.get() else "#445b73",
                state="disabled" if self.faulted else "normal",
            )

    def _validate(self) -> None:
        self._update_overlap_highlights()
        try:
            self.fields[self.selected].read().validate()
        except ValueError as exc:
            self.validation.configure(text=str(exc), text_color="#ff9999")
        else:
            self.validation.configure(
                text="Master pump / Automatic control" if self.selected == self.controller.master_channel
                else "Daily windows are valid.", text_color=GREEN,
            )
        try:
            validate_zones([fields.read() for fields in self.fields])
        except ValueError:
            self.save_button.configure(state="disabled")
        else:
            self.save_button.configure(state="disabled" if self.faulted else "normal")

    def _update_overlap_highlights(self) -> None:
        conflicting: set[tuple[int, int]] = set()
        active_conflicts: set[tuple[int, int, int, int]] = set()
        for day_index, day in enumerate(self.fields[self.selected].schedule):
            row = tuple(variable.get().strip() for variable in day)
            for first, second in overlap_notices(row):
                active_conflicts.add((self.selected, day_index, first, second))
            for first, second in overlapping_windows(row):
                conflicting.add((day_index, first))
                conflicting.add((day_index, second))
        self.acknowledged_overlaps.intersection_update(active_conflicts)
        for (day_index, field_index), entry in self.schedule_entries.items():
            slot = field_index // 2
            _, border = WINDOW_COLORS[slot]
            is_conflicting = (day_index, slot) in conflicting
            entry.configure(
                border_color="#ff5555" if is_conflicting else border,
                border_width=2 if is_conflicting else 1,
            )

    def _overlap_stop_clicked(self, day: int, window: int) -> None:
        row = tuple(variable.get().strip() for variable in self.fields[self.selected].schedule[day])
        notices = overlap_notices(row)
        conflicts = [
            (first, second, notice)
            for (first, second), notice in notices.items()
            if window in (first, second)
            and (self.selected, day, first, second) not in self.acknowledged_overlaps
        ]
        if not conflicts:
            return
        self.acknowledged_overlaps.update(
            (self.selected, day, first, second) for first, second, _ in conflicts
        )
        messages = [
            f"{DAY_NAMES[day]} {notice}"
            for _, _, notice in conflicts
        ]
        messagebox.showwarning(
            "Overlapping schedule times",
            "These windows overlap:\n\n" + "\n".join(messages)
            + "\n\nAdjust the times before saving.",
            parent=self,
        )

    def show_running(self) -> None:
        if self.running_window is not None and self.running_window.winfo_exists():
            self.running_window.lift()
            return
        window = ctk.CTkToplevel(self)
        window.title("Currently running")
        window.geometry("600x300")
        self.running_window = window
        self.running_summary = ctk.CTkTextbox(window, wrap="word")
        self.running_summary.pack(expand=True, fill="both", padx=12, pady=12)
        self.running_summary.insert("1.0", self.running_text)
        self.running_summary.configure(state="disabled")
        ctk.CTkLabel(
            window,
            text=("The service keeps running when this window closes."
                  if self.remote else "Schedules run only while the app is open.")
            + "\nSTOP ALL suppresses current schedule windows only.",
            text_color=GREY, justify="left",
        ).pack(padx=12, pady=(0, 12))

    def _update_duration_summary(self) -> None:
        choice = self.manual_duration.get()
        if self.selected == self.controller.master_channel:
            text = "The master pump is automatic."
        else:
            name = self.fields[self.selected].name.get().strip() or "(Unnamed)"
            if choice == "Custom":
                value = self.custom_duration.get().strip()
                valid = value.isascii() and value.isdigit() and len(value) <= 4
                duration = (
                    f"{value} minutes" if valid and 1 <= int(value) <= MAX_MANUAL_MINUTES
                    else "the custom minutes entered"
                )
            else:
                duration = {"10 min": "10 minutes", "30 min": "30 minutes",
                            "1 hour": "1 hour", "Until stopped": "until stopped"}.get(choice, choice)
            text = f"If started: Channel {self.selected + 1}, {name} - {duration}."
            text += " Choose a duration, then press RUN ZONE."
        self.duration_summary.configure(text=text)

    def _refresh(self) -> None:
        self._update_window_pause_buttons()
        operation_held = isinstance(self.controller, ServiceClient) and self.controller.operation_held
        now = datetime.now()
        if self.faulted:
            running_text = "Output states unknown - controller or connection unavailable."
        else:
            running = []
            reminders = []
            for channel, run in sorted(self.controller.runs.items()):
                name = self.controller.zones[channel].name
                if run.deadline is None:
                    end = "until stopped"
                else:
                    seconds = max(0, math.ceil(run.deadline - self.controller.clock()))
                    end = "until about " + (now + timedelta(seconds=seconds)).strftime("%a %H:%M")
                running.append(f"Channel {channel + 1}, {name} - {run.source} - {end}")
                threshold = self.controller.zones[channel].continuous_reminder_minutes
                if run.source == "Continuous" and threshold is not None and run.started is not None:
                    if self.controller.clock() - run.started >= threshold * 60:
                        reminders.append(f"Channel {channel + 1}, {name}: continuous run reached {threshold} minutes.")
            if self.controller.pump_on:
                running.append("Master pump - automatic - end depends on pump-using channels")
            running_text = "\n".join(running) if running else "No channels currently reported running."
            if reminders:
                running_text = "REMINDERS (informational; channel still running)\n" + "\n".join(reminders) + "\n\n" + running_text
        self.running_text = running_text
        self.running_button.configure(
            text="Running: unknown" if self.faulted
            else f"Reminder: {len(reminders)}" if reminders
            else f"Running: {len(self.controller.runs) + int(self.controller.pump_on)}",
            fg_color=RED if self.faulted or reminders else "#1f6aa5",
        )
        if (
            self.running_window is not None and self.running_window.winfo_exists()
            and self.running_summary.get("1.0", "end-1c") != running_text
        ):
            self.running_summary.configure(state="normal")
            self.running_summary.delete("1.0", "end")
            self.running_summary.insert("1.0", running_text)
            self.running_summary.configure(state="disabled")
        profile_status = self._refresh_timetable_controls()
        selected_profile = profile_status["selected"]
        for channel in range(MAX_ZONE_COUNT):
            if channel >= self.controller.channel_count:
                self.zone_buttons[channel].configure(
                    text=f"{channel + 1}  Board 2 not configured", state="disabled",
                    fg_color="transparent",
                )
                self.zone_dots[channel].configure(text_color=GREY)
                self.rain_pause_exempt_buttons[channel].configure(text="-", state="disabled")
                self.pump_exempt_buttons[channel].configure(text="-", state="disabled")
                continue
            zone = self.controller.zones[channel]
            available = channel in self.controller.available_channels
            master = channel == self.controller.master_channel
            active = self.controller.pump_on if master else channel in self.controller.runs
            color = RUNNING_RED if active else (
                SCHEDULED_GREEN
                if available and not master and (not self.controller.rain_paused or zone.rain_pause_exempt)
                and zone.scheduled(now) else GREY
            )
            self.zone_dots[channel].configure(text_color=color)
            name = "Master Pump" if master else self.fields[channel].name.get().strip() or "(Unnamed)"
            if not available:
                name = "(Unassigned) " + name
            display_name = name if len(name) <= 21 else name[:20] + "..."
            self.zone_buttons[channel].configure(
                text=f"{channel + 1}  {display_name}",
                state="normal",
                fg_color="#254e73" if channel == self.selected else "transparent",
            )
            exempt = self.fields[channel].rain_pause_exempt.get() and not master
            active_exemption = zone.rain_pause_exempt and not master
            pump_option = (
                self.controller.master_channel is not None
                and channel < self.controller.master_channel
            )
            self.pump_exempt_buttons[channel].configure(
                text=("P-" if self.fields[channel].pump_exempt.get() else "P")
                if pump_option else "-",
                text_color=RUNNING_RED if zone.pump_exempt and pump_option else GREY,
                state="normal" if pump_option and not self.faulted else "disabled",
            )
            self.rain_pause_exempt_buttons[channel].configure(
                text="-" if master else "R-" if exempt else "R",
                text_color=RUNNING_RED
                if self.controller.rain_paused and active_exemption else GREY,
                state="disabled" if master or self.faulted else "normal",
            )
        self.heading.configure(text=f"ZONE {self.selected + 1} CONFIGURATION")
        run = self.controller.runs.get(self.selected)
        master_selected = self.selected == self.controller.master_channel
        available = self.selected in self.controller.available_channels
        if not master_selected:
            fields = self.fields[self.selected]
            zone = self.controller.zones[self.selected]
            pump_option = (
                self.controller.master_channel is not None
                and self.selected < self.controller.master_channel
            )
            pump_text = (
                "excluded" if fields.pump_exempt.get() else "uses pump"
            ) if pump_option else "independent"
            rain_text = "continues" if fields.rain_pause_exempt.get() else "stops"
            pending = (
                fields.rain_pause_exempt.get() != zone.rain_pause_exempt
                or (pump_option and fields.pump_exempt.get() != zone.pump_exempt)
            )
            assert self.channel_summary is not None and self.rain_setting is not None
            self.channel_summary.configure(
                text=f"{fields.name.get().strip() or '(Unnamed)'} / Pump: {pump_text}"
                f" / Rain Pause: {rain_text}"
                + (" / Unsaved - not yet applied" if pending else ""),
            )
            self.rain_setting.configure(
                text="Rain Pause: keeps working" if fields.rain_pause_exempt.get()
                else "Rain Pause: stops this channel",
                state="disabled" if self.faulted else "normal",
            )
            if self.pump_setting is not None:
                self.pump_setting.configure(
                    text="Master pump: does not use pump" if fields.pump_exempt.get()
                    else "Master pump: uses pump",
                    state="disabled" if self.faulted else "normal",
                )
        if run is not None:
            if run.deadline is None:
                text = "RELAY ON / Continuous / Until stopped"
            else:
                seconds = max(0, math.ceil(run.deadline - self.controller.clock()))
                ends = (now + timedelta(seconds=seconds)).strftime("%a %H:%M")
                text = f"RELAY ON / {run.source} / until {ends} / {seconds // 60:02}:{seconds % 60:02} left"
        else:
            zone = self.controller.zones[self.selected]
            text = "RELAY OFF / Routine " + ("enabled" if zone.enabled else "disabled")
        if master_selected:
            text = "MASTER PUMP / " + ("ON" if self.controller.pump_on else "OFF") + " / Automatic"
        if self.controller.rain_paused and run is None and not master_selected:
            text = "RELAY OFF / RAIN PAUSE / Exempt zones may run"
        elif self.controller.rain_paused and master_selected:
            text += " / RAIN PAUSE"
        if self.faulted:
            text = ("SERVICE UNAVAILABLE / status unknown" if self.service_error else
                    "FAULT / output state unknown - check device")
            if isinstance(self.controller, ServiceClient) and self.controller.fault and not self.service_error:
                text = "SERVICE FAULT / " + self.controller.fault
        elif not available:
            text = "UNASSIGNED TAPO PLUG / output unavailable\nSchedules blocked until a device is assigned."
        elif not master_selected:
            next_start = next_scheduled_start(self.controller.zones[self.selected], now)
            next_text = next_start.strftime("%a %d %b %H:%M") if next_start else "none"
            text += "\nNext scheduled start: " + next_text
            if self.controller.rain_paused and not self.controller.zones[self.selected].rain_pause_exempt:
                text += " (blocked while Rain Pause is on)"
        if operation_held and not self.faulted:
            text = "RESTORE SAFETY HOLD / ALL OUTPUTS STOPPED\n"
            text += "Restart services and reopen app." if self.controller.restore_restart_required else "Review settings, then Backup > Resume operation."
        self.status.configure(
            text=text, text_color="#ff9999" if self.faulted else RUNNING_RED
            if run or (master_selected and self.controller.pump_on) else GREY
        )
        if self.faulted:
            for dot in self.zone_dots:
                dot.configure(text_color="#ff9999")
        self.manual_button.configure(
            text="STOP THIS ZONE" if run else "RUN ZONE",
            fg_color=RED if run else "#1f6aa5",
            hover_color="#92313c" if run else "#144870",
            state="disabled" if self.faulted or operation_held or master_selected or not available
            or (self.controller.rain_paused and not run
                and not self.controller.zones[self.selected].rain_pause_exempt)
            else "normal",
        )
        duration_disabled = self.faulted or operation_held or master_selected or run is not None or not available
        choices = ["10 min", "30 min", "1 hour", "Custom"]
        if (
            not master_selected
            and (self.selected >= 8 or self.controller.zones[self.selected].pump_exempt)
            and self.controller.zones[self.selected].max_manual_minutes is None
            and not self.fields[self.selected].max_manual_minutes.get().strip()
        ):
            choices.append("Until stopped")
        if self.duration_menu.cget("values") != choices:
            self.duration_menu.configure(values=choices)
        if self.manual_duration.get() not in choices:
            self.manual_duration.set("10 min")
        self.duration_menu.configure(state="disabled" if duration_disabled else "normal")
        if self.manual_duration.get() == "Custom" and not master_selected:
            self.duration_entry.grid(row=0, column=1, padx=(0, 8))
        else:
            self.duration_entry.grid_remove()
        self.duration_entry.configure(
            state="normal" if not duration_disabled and self.manual_duration.get() == "Custom"
            else "disabled",
        )
        self._update_duration_summary()
        if not master_selected:
            self.manual_max_entry.configure(state="disabled" if self.faulted else "normal")
            reminder_unavailable = isinstance(self.controller, ServiceClient) and not self.controller.continuous_reminders_supported
            self.reminder_entry.configure(state="disabled" if self.faulted or reminder_unavailable else "normal")
            self.reminder_help.configure(
                text="Restart the updated controller service to enable reminders and save updated settings."
                if reminder_unavailable else
                "Blank: off. Shows an on-screen reminder after 1-1440 minutes; does not stop the channel."
            )
        count = len(self.controller.runs) + int(self.controller.pump_on)
        if self.dirty:
            try:
                validate_zones([fields.read() for fields in self.fields])
            except ValueError:
                save_status = "Unsaved changes / invalid settings not saved"
            else:
                save_status = "Unsaved changes / auto-saving shortly" if self.autosave_timer else "Unsaved changes"
        else:
            save_status = self.save_status
        if selected_profile:
            modified = profile_status["modified"]
            save_status += f" / {selected_profile}" + (" (modified)" if modified else "")
        self.system_status.configure(
            text=("System: SERVICE UNAVAILABLE - status unknown" if self.service_error
                  else "System: FAULT - output state unknown") if self.faulted else (
                ("RAIN PAUSE / Exempt zones may water" if self.controller.rain_paused
                 else f"RELAY / {count} relays on")
                + " / " + save_status
            )
        )
        if self.faulted:
            self.connection_label.configure(
                text="CONNECTION LOST - OUTPUT STATES UNKNOWN" if self.service_error else
                "CONTROLLER FAULT - OUTPUT STATES UNKNOWN",
                text_color="#ff7777",
            )
        else:
            endpoints = (
                " / ".join(f"{board['host']}:{board['port']}" for board in self.controller.boards)
                if isinstance(self.controller, ServiceClient) else f"{self.relay.host}:{self.relay.port}"
            )
            self.connection_label.configure(
                text=f"{'SERVICE' if self.remote else 'WAVESHARE RELAY'} - {endpoints}",
                text_color="#ffcb70",
            )
        self.connection_button.configure(
            state="disabled" if self.faulted else "normal",
        )
        self.rain_pause_var.set(self.controller.rain_paused)
        self.rain_pause_switch.configure(state="disabled" if self.faulted else "normal")
        self.history_button.configure(state="disabled" if self.service_error else "normal")
        if isinstance(self.controller, ServiceClient) and self.controller.history_warning:
            self.system_status.configure(
                text=self.system_status.cget("text") + " / HISTORY WARNING: " + self.controller.history_warning
            )
        if self.overview is not None and self.overview.winfo_exists():
            self.overview.refresh()
        if self.weekly_overview is not None and self.weekly_overview.winfo_exists():
            self.weekly_overview.refresh()

    def toggle_rain_pause(self) -> None:
        paused = self.rain_pause_var.get()
        if paused and self.dirty and not self.save():
            self.rain_pause_var.set(False)
            self._refresh()
            return
        if not paused and not messagebox.askyesno(
            "Resume watering?",
            "Turn off Rain Pause and resume normal scheduling?\n\n"
            "Watering may start immediately if a saved schedule is active.",
            parent=self,
        ):
            self.rain_pause_var.set(True)
            return
        if paused and not self.remote:
            self.controller.rain_paused = True
        try:
            if not self.remote:
                save_rain_pause(self.settings_path, paused)
            self.controller.set_rain_pause(paused, datetime.now())
        except ValueError as exc:
            messagebox.showerror("Cannot change Rain Pause", str(exc), parent=self)
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Cannot change Rain Pause", exc)
        self._refresh()

    def read_relay_status(self) -> None:
        try:
            states = self.relay.read_states()
        except ValueError as exc:
            messagebox.showerror("Cannot read relay status", str(exc), parent=self)
            return
        except (OSError, RuntimeError) as exc:
            self._fault("Relay status read failed", exc)
            return
        active = [str(channel + 1) for channel, state in enumerate(states) if state is True]
        unknown = [str(channel + 1) for channel, state in enumerate(states) if state is None]
        messagebox.showinfo(
            "Relay status",
            f"Connected to {self.relay.host}:"
            f"{self.relay.port} (slave {self.relay.slave_id}).\n\n"
            f"Relay outputs currently ON: {', '.join(active) if active else 'none'}"
            f"\nUnassigned/unknown: {', '.join(unknown) if unknown else 'none'}",
            parent=self,
        )

    def save(self, *, automatic: bool = False, bank: int | None = None) -> bool:
        if self.autosave_timer is not None:
            self.after_cancel(self.autosave_timer)
            self.autosave_timer = None
        try:
            if bank is None:
                zones = [fields.read() for fields in self.fields]
            else:
                zones = list(self.controller.zones)
                if bank not in (0, 8) or bank + 8 > len(zones):
                    raise ValueError("This timetable group is not configured.")
                zones[bank:bank + 8] = [fields.read() for fields in self.fields[bank:bank + 8]]
            if isinstance(self.controller, ServiceClient):
                self.controller.revision = self.edit_revision
            else:
                save_zones(self.settings_path, zones)
            self.controller.apply(zones, datetime.now())
        except ValueError as exc:
            self.save_status = "Save failed"
            messagebox.showerror("Cannot save/apply settings", str(exc), parent=self)
            self._refresh()
            return False
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Cannot save/apply settings", exc)
            return False
        try:
            self.dirty = [fields.read() for fields in self.fields] != self.controller.zones
        except ValueError:
            self.dirty = True
        self.save_status = "Unsaved changes" if self.dirty else "Auto-saved" if automatic else "Saved"
        if isinstance(self.controller, ServiceClient):
            self.edit_revision = self.controller.revision
        if bank is not None and self.dirty:
            self._schedule_autosave()
        self._refresh()
        return True

    def manual(self) -> None:
        try:
            if self.selected in self.controller.runs:
                self.controller.stop(self.selected, datetime.now())
            else:
                fields = self.fields[self.selected]
                maximum = fields.manual_maximum()
                if maximum != self.controller.zones[self.selected].max_manual_minutes:
                    raise ValueError("Save the manual maximum before starting a run.")
                choice = self.manual_duration.get()
                continuous = choice == "Until stopped"
                if continuous:
                    zone = self.controller.zones[self.selected]
                    if self.selected < 8 and not zone.pump_exempt:
                        raise ValueError("Until stopped requires a saved master pump exclusion.")
                    if maximum is not None:
                        raise ValueError("Until stopped is unavailable while a manual maximum is set.")
                    if not messagebox.askyesno(
                        "Run until stopped?",
                        "This channel has no automatic time limit. Closing the app will not stop it.",
                        parent=self,
                    ):
                        return
                if choice == "Custom":
                    value = self.custom_duration.get().strip()
                    if not value.isascii() or not value.isdecimal():
                        raise ValueError("Enter a custom duration of 1 to 1440 minutes.")
                    duration = int(value)
                else:
                    duration = {"10 min": 10, "30 min": 30, "1 hour": 60, "Until stopped": 10}[choice]
                if not 1 <= duration <= MAX_MANUAL_MINUTES:
                    raise ValueError("Enter a custom duration of 1 to 1440 minutes.")
                self.controller.start_manual(
                    self.selected, datetime.now(), duration_minutes=duration, continuous=continuous,
                )
        except ValueError as exc:
            messagebox.showerror("Manual operation rejected", str(exc), parent=self)
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Manual operation failed", exc)
        self._refresh()

    @staticmethod
    def _error_details(error: Exception) -> str:
        if isinstance(error, ExceptionGroup):
            return "\n".join(SprinklerApp._error_details(item) for item in error.exceptions)
        return str(error)

    def _fault(self, title: str, error: Exception) -> None:
        logging.error("%s: %s", title, self._error_details(error))
        if self.remote:
            self.faulted = True
            self.service_error = self._error_details(error)
            self._validate()
            self._refresh()
            messagebox.showerror(
                title,
                f"{self.service_error}\n\nThe desktop cannot confirm current output states. "
                "The background service may still be running. Do not assume watering "
                "has stopped. Check the service/device; this window will reconnect.",
                parent=self,
            )
            return
        self.faulted = True
        self.controller.closed = True
        details = self._error_details(error)
        try:
            self.controller.stop_all(datetime.now())
        except ExceptionGroup as shutdown_error:
            details += "\n\nOFF failures:\n" + self._error_details(shutdown_error)
        self._validate()
        self._refresh()
        messagebox.showerror(
            title,
            f"{details}\n\nScheduling is stopped. All outputs were sent OFF commands, "
            "but physical states may be unknown. Check the device and disconnect "
            "valve power if necessary. Restart only after fixing the fault.",
            parent=self,
        )

    def stop_all(self) -> None:
        try:
            self.controller.stop_all(datetime.now())
        except (OSError, RuntimeError, ValueError, ExceptionGroup) as exc:
            self._fault("Relay shutdown failed", exc)
        self._refresh()

    def _tick(self) -> None:
        if isinstance(self.controller, ServiceClient):
            try:
                self.controller.sync()
            except (OSError, RuntimeError, ValueError) as exc:
                if not self.service_error:
                    logging.error("Service connection lost: %s", exc)
                self.service_error = str(exc)
                self.faulted = True
            else:
                self.service_error = ""
                self.faulted = bool(self.controller.fault)
                if not self.dirty:
                    self.syncing = True
                    try:
                        for fields, zone in zip(self.fields, self.controller.zones):
                            if fields.name.get() != zone.name:
                                fields.name.set(zone.name)
                            if fields.enabled.get() != zone.enabled:
                                fields.enabled.set(zone.enabled)
                            if fields.rain_pause_exempt.get() != zone.rain_pause_exempt:
                                fields.rain_pause_exempt.set(zone.rain_pause_exempt)
                            if fields.pump_exempt.get() != zone.pump_exempt:
                                fields.pump_exempt.set(zone.pump_exempt)
                            maximum = str(zone.max_manual_minutes) if zone.max_manual_minutes else ""
                            if fields.max_manual_minutes.get() != maximum:
                                fields.max_manual_minutes.set(maximum)
                            reminder = str(zone.continuous_reminder_minutes or "")
                            if fields.continuous_reminder_minutes.get() != reminder:
                                fields.continuous_reminder_minutes.set(reminder)
                            for variables, row in zip(fields.schedule, zone.schedule):
                                for variable, value in zip(variables, row):
                                    if variable.get() != value:
                                        variable.set(value)
                            for variable, value in zip(fields.paused_windows, zone.paused_windows):
                                if variable.get() != value:
                                    variable.set(value)
                        self.edit_revision = self.controller.revision
                    finally:
                        self.syncing = False
                    self._update_window_pause_buttons()
            self._validate()
            self._refresh()
            self.timer = self.after(1000, self._tick)
            return
        try:
            self.controller.tick(datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Scheduler stopped", exc)
        self._refresh()
        if not self.faulted:
            self.timer = self.after(250, self._tick)

    def on_close(self) -> None:
        if self.autosave_timer is not None:
            self.after_cancel(self.autosave_timer)
            self.autosave_timer = None
        if self.dirty and not self.faulted:
            choice = messagebox.askyesnocancel(
                "Unsaved changes", "Save settings before closing?", parent=self
            )
            if choice is None or (choice and not self.save()):
                return
        if self.timer is not None:
            self.after_cancel(self.timer)
        try:
            self.controller.close(datetime.now())
        except ExceptionGroup as exc:
            messagebox.showerror(
                "Relay shutdown failed",
                f"{self._error_details(exc)}\nDisconnect relay power and check the hardware.",
                parent=self,
            )
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=default_socket(),
                        help="Local background service socket")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    try:
        backend = ServiceClient(args.socket.expanduser())
    except (OSError, ValueError) as exc:
        logging.error("Cannot connect to background controller: %s", exc)
        root = ctk.CTk()
        root.withdraw()
        messagebox.showerror(
            "Background service unavailable",
            f"{exc}\n\nStart/check the service:\n"
            "systemctl --user status garden-sprinkler.service\n\n"
            "This app will not connect directly to the relay board.",
            parent=root,
        )
        root.destroy()
        raise SystemExit(1) from exc
    app = SprinklerApp(DEFAULT_CONFIG, backend)
    try:
        app.mainloop()
    finally:
        if not app.controller.closed:
            app.controller.close(datetime.now())


if __name__ == "__main__":
    main()
