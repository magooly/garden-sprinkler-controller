"""Dark-mode desktop interface for the garden sprinkler controller."""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from controller import (
    DAY_NAMES,
    WINDOW_COUNT,
    Controller,
    Zone,
    copy_day_times,
    load_zones,
    load_rain_pause,
    save_rain_pause,
    save_zones,
    validate_zones,
)
from waveshare_relay import DEFAULT_HOST, DEFAULT_PORT, WaveshareRelays

GREEN = "#37cc87"
RUNNING_RED = "#ff2020"
BLUE = "#55aaff"
GREY = "#8b98ab"
RED = "#b63e4b"
WINDOW_COLORS = (
    ("#283c50", "#526e89"),
    ("#2e433b", "#587b69"),
    ("#49402e", "#87734e"),
    ("#40344c", "#78618b"),
)


@dataclass
class ZoneFields:
    name: tk.StringVar
    enabled: tk.BooleanVar
    schedule: list[list[tk.StringVar]]

    def read(self) -> Zone:
        return Zone(
            self.name.get().strip(),
            self.enabled.get(),
            tuple(tuple(time.get().strip() for time in day) for day in self.schedule),
        )


class SprinklerApp(ctk.CTk):
    def __init__(
        self,
        settings_path: Path,
        backend: WaveshareRelays,
    ) -> None:
        super().__init__()
        self.withdraw()
        self.title("Garden Sprinkler Controller")
        self.geometry("1180x760")
        self.minsize(1100, 620)
        self.settings_path = settings_path
        self.selected = 0
        self.timer: str | None = None
        self.dirty = False
        self.faulted = False
        self.relay = backend
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            zones = load_zones(settings_path)
            rain_paused = load_rain_pause(settings_path)
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
                    [tk.StringVar(self, value) for value in day]
                    for day in zone.schedule
                ],
            )
            for zone in zones
        ]
        self._build_layout()
        for fields in self.fields:
            for variable in (
                fields.name,
                fields.enabled,
                *(time for day in fields.schedule for time in day),
            ):
                variable.trace_add("write", self._edited)
        self.select_zone(0)
        self.deiconify()
        self._tick()

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="#172638", corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(
            header, text="GARDEN / Sprinkler Controller", font=("", 20, "bold")
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

        sidebar = ctk.CTkFrame(self, width=235, corner_radius=0)
        sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        sidebar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(sidebar, text="RELAY ZONES", font=("", 14, "bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(10, 6), sticky="w"
        )
        self.zone_buttons: list[ctk.CTkButton] = []
        self.zone_dots: list[ctk.CTkLabel] = []
        for channel in range(8):
            dot = ctk.CTkLabel(
                sidebar,
                text="\u25cf",
                text_color=GREY,
                width=24,
                font=("", 22, "bold"),
            )
            dot.grid(row=channel + 1, column=0, padx=(15, 0))
            button = ctk.CTkButton(
                sidebar,
                text="",
                anchor="w",
                width=200,
                height=35,
                command=lambda index=channel: self.select_zone(index),
            )
            button.grid(row=channel + 1, column=1, padx=(8, 15), pady=3, sticky="ew")
            self.zone_buttons.append(button)
            self.zone_dots.append(dot)
        ctk.CTkLabel(
            sidebar,
            text="Red: watering\nBlue: scheduled now\nGrey: inactive",
            text_color=GREY,
            justify="left",
        ).grid(row=9, column=0, columnspan=2, padx=20, pady=10, sticky="w")
        self.rain_pause_var = tk.BooleanVar(self, self.controller.rain_paused)
        self.rain_pause_switch = ctk.CTkSwitch(
            sidebar, text="RAIN PAUSE", variable=self.rain_pause_var,
            command=self.toggle_rain_pause, progress_color="#87734e",
        )
        self.rain_pause_switch.grid(
            row=10, column=0, columnspan=2, padx=20, pady=(8, 4), sticky="w"
        )
        ctk.CTkLabel(
            sidebar, text="All watering OFF while enabled.\nRemembered after restart.",
            text_color=GREY, justify="left",
        ).grid(row=11, column=0, columnspan=2, padx=20, pady=(0, 12), sticky="w")

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
        self.editor = ctk.CTkScrollableFrame(panel)
        self.editor.grid(row=2, column=0, sticky="nsew")
        self.editor.grid_columnconfigure(0, weight=1)
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        actions.grid_columnconfigure((0, 1), weight=1, uniform="actions")
        self.manual_button = ctk.CTkButton(actions, text="", height=36, command=self.manual)
        self.manual_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.test_button = ctk.CTkButton(
            actions, text="Test Zone - ON (10 min max)", height=36, command=self.test
        )
        self.test_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        ctk.CTkLabel(
            panel,
            text="Schedules run only while this app is open. Changes apply after saving.\n"
            "Manual watering stops after 10 minutes. Stop All stops current windows only.",
            text_color=GREY,
            justify="left",
            anchor="w",
        ).grid(row=5, column=0, sticky="ew", pady=(6, 0))

        footer = ctk.CTkFrame(self, corner_radius=0)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.system_status = ctk.CTkLabel(footer, text="System: Ready", anchor="w")
        self.system_status.grid(row=0, column=0, sticky="ew", padx=20, pady=8)
        self.stop_all_button = ctk.CTkButton(
            footer, text="STOP ALL", fg_color=RED, hover_color="#92313c",
            command=self.stop_all, width=120,
        )
        self.stop_all_button.grid(row=0, column=1, padx=10)
        self.save_button = ctk.CTkButton(
            footer, text="Save Settings", command=self.save, width=150
        )
        self.save_button.grid(row=0, column=2, padx=(10, 24))
        ctk.CTkLabel(
            footer,
            text="Developed by Wally Horsman \u00a9 2026",
            text_color=GREY,
            font=("", 11),
        ).grid(row=1, column=0, columnspan=3, padx=20, pady=(0, 8), sticky="e")

    def select_zone(self, channel: int) -> None:
        self.selected = channel
        for widget in self.editor.winfo_children():
            widget.destroy()
        if channel == self.controller.master_channel:
            ctk.CTkLabel(
                self.editor, text="CHANNEL 8 / MASTER PUMP", font=("", 18, "bold"),
            ).grid(row=0, column=0, padx=20, pady=(20, 10), sticky="w")
            ctk.CTkLabel(
                self.editor,
                text="Automatically ON whenever a garden zone (1-7) is watering.\n"
                "The valve opens before the pump starts. The pump stops before\n"
                "the last valve closes. No independent schedule or manual test.\n\n"
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
        ctk.CTkLabel(self.editor, text="DAILY SCHEDULE - UP TO FOUR WINDOWS", anchor="w").grid(
            row=3, column=0, columnspan=2, padx=12, pady=(6, 0), sticky="ew"
        )
        copy_controls = ctk.CTkFrame(self.editor, fg_color="transparent")
        copy_controls.grid(row=4, column=0, columnspan=2, padx=12, pady=(4, 0), sticky="w")
        source_day = tk.StringVar(self, DAY_NAMES[0])
        ctk.CTkOptionMenu(
            copy_controls, variable=source_day, values=list(DAY_NAMES), width=115
        ).pack(side="left", padx=(0, 12))
        copy_var = tk.BooleanVar(self, False)

        def copy_selected_day() -> None:
            if not copy_var.get():
                return
            index = DAY_NAMES.index(source_day.get())
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

        ctk.CTkCheckBox(
            copy_controls,
            text="Copy selected day's times to all days",
            variable=copy_var,
            command=copy_selected_day,
        ).pack(side="left")
        times = ctk.CTkFrame(self.editor, fg_color="transparent")
        times.grid(row=5, column=0, columnspan=2, padx=12, pady=6, sticky="ew")
        times.grid_columnconfigure(0, weight=1)
        for column in range(1, WINDOW_COUNT * 2 + 1):
            times.grid_columnconfigure(column, weight=2)
        ctk.CTkLabel(times, text="Day", anchor="w").grid(
            row=0, column=0, padx=(0, 8), pady=(0, 5), sticky="w"
        )
        for column, label in enumerate(
            (f"{label} {window}" for window in range(1, WINDOW_COUNT + 1)
             for label in ("Start", "Stop")),
            start=1,
        ):
            background, _ = WINDOW_COLORS[(column - 1) // 2]
            ctk.CTkLabel(
                times, text=label, fg_color=background, text_color="#edf0f4",
                corner_radius=5,
            ).grid(
                row=0, column=column, padx=4, pady=(0, 5), sticky="ew"
            )
        for day_index, day_name in enumerate(DAY_NAMES):
            ctk.CTkLabel(times, text=day_name, anchor="w").grid(
                row=day_index + 1, column=0, padx=(0, 8), pady=4, sticky="w"
            )
            for slot, variable in enumerate(fields.schedule[day_index], start=1):
                background, border = WINDOW_COLORS[(slot - 1) // 2]
                ctk.CTkEntry(
                    times, textvariable=variable, width=70, height=30,
                    fg_color=background, border_color=border, text_color="#edf0f4",
                ).grid(
                    row=day_index + 1, column=slot, padx=4, pady=2, sticky="ew"
                )
        ctk.CTkLabel(
            self.editor,
            text="Enter 1620 or 16:20 (24-hour time). Leave a pair blank to skip it. "
            "Keep windows in time order without overlaps.",
            text_color=GREY,
            anchor="w",
            wraplength=700,
        ).grid(row=6, column=0, columnspan=2, padx=12, pady=(0, 4), sticky="ew")
        self.validation = ctk.CTkLabel(self.editor, text="", anchor="w", wraplength=700)
        self.validation.grid(row=7, column=0, columnspan=2, padx=12, pady=(0, 4), sticky="ew")
        self._validate()
        self._refresh()

    def _edited(self, *_args: str) -> None:
        self.dirty = True
        self._validate()
        self._refresh()

    def _validate(self) -> None:
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

    def _refresh(self) -> None:
        now = datetime.now()
        for channel, zone in enumerate(self.controller.zones):
            master = channel == self.controller.master_channel
            active = self.controller.pump_on if master else channel in self.controller.runs
            color = RUNNING_RED if active else (
                BLUE if not master and not self.controller.rain_paused and zone.scheduled(now) else GREY
            )
            self.zone_dots[channel].configure(text_color=color)
            name = "Master Pump" if master else self.fields[channel].name.get().strip() or "(Unnamed)"
            display_name = name if len(name) <= 21 else name[:20] + "..."
            self.zone_buttons[channel].configure(
                text=f"{channel + 1}  {display_name}",
                fg_color="#254e73" if channel == self.selected else "transparent",
            )
        self.heading.configure(text=f"ZONE {self.selected + 1} CONFIGURATION")
        run = self.controller.runs.get(self.selected)
        master_selected = self.selected == self.controller.master_channel
        if run is not None:
            seconds = max(0, math.ceil(run.deadline - self.controller.clock()))
            text = f"RELAY ON / {run.source} / {seconds // 60:02}:{seconds % 60:02} left"
        else:
            zone = self.controller.zones[self.selected]
            text = "Idle / Routine " + ("enabled" if zone.enabled else "disabled")
        if master_selected:
            text = "MASTER PUMP / " + ("ON" if self.controller.pump_on else "OFF") + " / Automatic"
        if self.controller.rain_paused:
            text = "RAIN PAUSE / All watering OFF"
        if self.faulted:
            text = "FAULT / output state unknown - check device"
        self.status.configure(
            text=text, text_color="#ff9999" if self.faulted else RUNNING_RED
            if run or (master_selected and self.controller.pump_on) else GREY
        )
        if self.faulted:
            for dot in self.zone_dots:
                dot.configure(text_color="#ff9999")
        self.manual_button.configure(
            text="STOP THIS ZONE" if run else "Manual Override: TURN ON NOW (10 min)",
            fg_color=RED if run else "#1f6aa5",
            hover_color="#92313c" if run else "#144870",
            state="disabled" if self.faulted or self.controller.rain_paused or master_selected else "normal",
        )
        testing = run is not None and run.source == "Test"
        self.test_button.configure(
            text="Stop Test - OFF" if testing else "Test Zone - ON (10 min max)",
            fg_color=RED if testing else "#1f6aa5",
            hover_color="#92313c" if testing else "#144870",
            state="disabled" if self.faulted or self.controller.rain_paused or master_selected or (run is not None and not testing) else "normal",
        )
        count = len(self.controller.runs) + int(self.controller.pump_on)
        self.system_status.configure(
            text="System: FAULT - output state unknown" if self.faulted else (
                ("RAIN PAUSE / All watering OFF" if self.controller.rain_paused
                 else f"RELAY / {count} relays on")
                + (" / Unsaved changes" if self.dirty else " / Ready")
            )
        )
        if self.faulted:
            self.connection_label.configure(text="HARDWARE FAULT - output state unknown")
        else:
            self.connection_label.configure(
                text=f"WAVESHARE RELAY - {self.relay.host}:{self.relay.port}"
            )
        self.connection_button.configure(
            state="disabled" if self.faulted else "normal",
        )
        self.rain_pause_var.set(self.controller.rain_paused)
        self.rain_pause_switch.configure(state="disabled" if self.faulted else "normal")

    def toggle_rain_pause(self) -> None:
        paused = self.rain_pause_var.get()
        if not paused and not messagebox.askyesno(
            "Resume watering?",
            "Turn off Rain Pause and resume normal scheduling?\n\n"
            "Watering may start immediately if a saved schedule is active.",
            parent=self,
        ):
            self.rain_pause_var.set(True)
            return
        if paused:
            self.controller.rain_paused = True
        try:
            save_rain_pause(self.settings_path, paused)
            self.controller.set_rain_pause(paused, datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Cannot change Rain Pause", exc)
        self._refresh()

    def read_relay_status(self) -> None:
        try:
            states = self.relay.read_states()
        except (OSError, RuntimeError) as exc:
            self._fault("Relay status read failed", exc)
            return
        active = [str(channel + 1) for channel, state in enumerate(states) if state]
        messagebox.showinfo(
            "Relay status",
            f"Connected to {self.relay.host}:"
            f"{self.relay.port} (slave {self.relay.slave_id}).\n\n"
            f"Relay outputs currently ON: {', '.join(active) if active else 'none'}",
            parent=self,
        )

    def save(self) -> bool:
        try:
            zones = [fields.read() for fields in self.fields]
            save_zones(self.settings_path, zones)
            self.controller.apply(zones, datetime.now())
        except ValueError as exc:
            messagebox.showerror("Cannot save/apply settings", str(exc), parent=self)
            return False
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Cannot save/apply settings", exc)
            return False
        self.dirty = False
        self._refresh()
        return True

    def manual(self) -> None:
        try:
            if self.selected in self.controller.runs:
                self.controller.stop(self.selected, datetime.now())
            else:
                self.controller.start_manual(self.selected, datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Manual operation failed", exc)
        self._refresh()

    def test(self) -> None:
        try:
            self.controller.toggle_test(self.selected, datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Zone test failed", exc)
        self._refresh()

    @staticmethod
    def _error_details(error: Exception) -> str:
        if isinstance(error, ExceptionGroup):
            return "\n".join(SprinklerApp._error_details(item) for item in error.exceptions)
        return str(error)

    def _fault(self, title: str, error: Exception) -> None:
        logging.error("%s: %s", title, self._error_details(error))
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
        except ExceptionGroup as exc:
            self._fault("Relay shutdown failed", exc)
        self._refresh()

    def _tick(self) -> None:
        try:
            self.controller.tick(datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as exc:
            self._fault("Scheduler stopped", exc)
        self._refresh()
        if not self.faulted:
            self.timer = self.after(250, self._tick)

    def on_close(self) -> None:
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
    parser.add_argument(
        "--config", type=Path,
        default=Path.home() / ".config" / "garden-sprinkler-controller" / "settings.json",
        help="Settings JSON file path",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Relay IP address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="RTU-over-TCP port")
    parser.add_argument("--slave-id", type=int, default=1, help="Numeric Modbus slave ID")
    parser.add_argument("--timeout", type=float, default=1.0, help="Transaction timeout in seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    try:
        backend = WaveshareRelays(args.host, args.port, args.slave_id, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    app = SprinklerApp(args.config.expanduser(), backend)
    try:
        app.mainloop()
    finally:
        if not app.controller.closed:
            app.controller.close(datetime.now())


if __name__ == "__main__":
    main()
