"""Read-only schedule table and cross-relay conflict warnings."""

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk
from typing import Callable

import customtkinter as ctk

from controller import DAY_NAMES, Zone, parse_time


@dataclass
class ScheduleRow:
    channel: int
    name: str
    period: int
    start: str
    stop: str
    enabled: bool
    start_minute: int | None
    stop_minute: int | None
    warning: str = ""
    paused: bool = False


def overview_rows(
    zones: list[Zone], day: int, master_channel: int | None, hide_disabled: bool,
) -> list[ScheduleRow]:
    if type(day) is not int or day not in range(7):
        raise ValueError("Choose a weekday from Monday through Sunday.")
    rows = []
    for channel, zone in enumerate(zones):
        if channel == master_channel or (hide_disabled and not zone.enabled):
            continue
        for period, (start, stop) in enumerate(
            zip(zone.schedule[day][::2], zone.schedule[day][1::2]), start=1
        ):
            if not start and not stop:
                continue
            begin = end = None
            warning = ""
            try:
                begin = parse_time(start)
                end = parse_time(stop)
                if end <= begin:
                    raise ValueError("Stop must be later than start.")
            except ValueError:
                warning = "Invalid/incomplete time pair"
                end = None
            rows.append(ScheduleRow(
                channel, zone.name or "(Unnamed)", period, start, stop,
                zone.enabled, begin, end, warning, zone.paused_windows[period - 1],
            ))
    warnings: dict[int, str] = {}
    for index, row in enumerate(rows):
        if not row.enabled or row.paused or row.warning:
            continue
        peers = [
            other for other in rows
            if other.channel != row.channel and other.enabled and not other.paused and not other.warning
        ]
        # Collect warnings separately so marking one row cannot hide it from peers.
        duplicate = any(other.start_minute == row.start_minute for other in peers)
        overlap = any(
            row.start_minute is not None and row.stop_minute is not None
            and other.start_minute is not None and other.stop_minute is not None
            and row.start_minute < other.stop_minute
            and other.start_minute < row.stop_minute
            for other in peers
        )
        row_notes = []
        if duplicate:
            row_notes.append("Same start")
        if overlap:
            row_notes.append("Overlap")
        # Do not mutate warnings until all rows have been compared.
        warnings[index] = " / ".join(row_notes)
    for index, warning in warnings.items():
        rows[index].warning = warning
    return sorted(rows, key=lambda row: (
        row.start_minute if row.start_minute is not None else 1440,
        row.channel, row.period,
    ))


class ScheduleOverview(ctk.CTkToplevel):
    def __init__(
        self, master: ctk.CTk, snapshot: Callable[[], tuple[list[Zone], int | None, bool, bool, bool, int]],
        select_zone: Callable[[int], None], day: int,
    ) -> None:
        super().__init__(master)
        self.title("Daily Schedule")
        self.geometry("960x520")
        self.minsize(820, 360)
        self.snapshot = snapshot
        self.select_zone = select_zone
        self.last_snapshot = None
        self.rows: list[ScheduleRow] = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        self.day = tk.StringVar(self, DAY_NAMES[day])
        ctk.CTkOptionMenu(
            controls, values=list(DAY_NAMES), variable=self.day,
            command=lambda _value: self.refresh(),
        ).pack(side="left", padx=(0, 20))
        self.hide_disabled = tk.BooleanVar(self, False)
        ctk.CTkCheckBox(
            controls, text="Hide disabled routines", variable=self.hide_disabled,
            command=self.refresh,
        ).pack(side="left")
        self.summary = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=880)
        self.summary.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        container = ctk.CTkFrame(self)
        container.grid(row=2, column=0, sticky="nsew", padx=12)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure(
            "Schedule.Treeview", background="#242b34", foreground="#edf0f4",
            fieldbackground="#242b34", rowheight=30,
        )
        style.configure("Schedule.Treeview.Heading", font=("", 10, "bold"))
        self.table = ttk.Treeview(
            container, columns=("start", "stop", "relay", "garden", "period", "state", "warning"),
            show="headings", selectmode="browse", style="Schedule.Treeview",
        )
        for key, title, width in (
            ("start", "Start", 70), ("stop", "Stop", 70), ("relay", "Relay", 55),
            ("garden", "Garden", 210), ("period", "Period", 55),
            ("state", "Routine", 80), ("warning", "Warning", 220),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=45, stretch=key in ("garden", "warning"))
        self.table.tag_configure("warning", background="#51432d", foreground="#ffe0a0")
        self.table.tag_configure("invalid", background="#503238", foreground="#ffbdc4")
        self.table.tag_configure("disabled", foreground="#9ba5b3")
        self.table.tag_configure("paused", foreground="#9ba5b3")
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<<TreeviewSelect>>", self.open_selected)
        ctk.CTkLabel(
            self, text="Select a row to open its relay settings. Amber warnings do not block saving.\n"
            "Warnings compare enabled garden relays only; touching end/start times do not overlap.",
            anchor="w", justify="left",
        ).grid(row=3, column=0, sticky="ew", padx=12, pady=10)
        self.refresh()

    def open_selected(self, _event: tk.Event | None = None) -> None:
        selected = self.table.selection()
        if selected:
            self.select_zone(self.rows[int(selected[0])].channel)
            self.master.lift()

    def refresh(self) -> None:
        zones, master, dirty, paused, unavailable, first = self.snapshot()
        signature = (zones, master, dirty, paused, unavailable, first, self.day.get(), self.hide_disabled.get())
        if signature == self.last_snapshot:
            return
        self.last_snapshot = signature
        self.rows = [
            row for row in overview_rows(zones, DAY_NAMES.index(self.day.get()), master, self.hide_disabled.get())
            if first <= row.channel < first + 8
        ]
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self.rows):
            invalid = row.stop_minute is None
            tag = (
                "invalid" if invalid else "paused" if row.paused
                else "disabled" if not row.enabled else "warning" if row.warning else ""
            )
            self.table.insert("", "end", iid=str(index), values=(
                row.start, row.stop, row.channel + 1, row.name, row.period,
                "Paused" if row.paused else "Enabled" if row.enabled else "Disabled",
                row.warning,
            ), tags=(tag,))
        text = "UNSAVED EDITS - not yet applied" if dirty else "Saved schedules"
        text += f" / Channels {first + 1}-{first + 8}"
        text += f" / {len(self.rows)} periods / {sum(bool(row.warning) for row in self.rows)} rows with warnings"
        if not self.rows:
            text += " / No configured periods for this day/filter"
        if any(row.stop_minute is None for row in self.rows):
            text += "\nInvalid/incomplete pairs are shown but cannot be fully checked for conflicts."
        if paused:
            text += "\nRAIN PAUSE is on: this table shows configured times, not running outputs."
        if unavailable:
            text += "\nService unavailable or faulted: saved status may be stale."
        self.summary.configure(text=text)


class WeeklyOverview(ctk.CTkToplevel):
    def __init__(
        self, master: ctk.CTk,
        snapshot: Callable[[], tuple[list[Zone], int | None, bool, bool, bool, int]],
        select_zone: Callable[[int], None],
    ) -> None:
        super().__init__(master)
        self.title("Weekly Overview")
        self.geometry("1180x740")
        self.minsize(1000, 480)
        self.snapshot = snapshot
        self.select_zone = select_zone
        self.last_snapshot = None
        self.cells: dict[tuple[int, int], ctk.CTkLabel] = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.hide_disabled = tk.BooleanVar(self, False)
        ctk.CTkCheckBox(
            self, text="Hide disabled routines", variable=self.hide_disabled,
            command=self.refresh,
        ).grid(row=0, column=0, padx=12, pady=10, sticky="w")
        self.summary = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=960)
        self.summary.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.table = ctk.CTkScrollableFrame(self)
        self.table.grid(row=2, column=0, padx=12, sticky="nsew")
        self.table.grid_columnconfigure(0, weight=2, minsize=140)
        for column in range(1, 8):
            self.table.grid_columnconfigure(column, weight=1, uniform="days", minsize=110)
        ctk.CTkLabel(
            self, text="Click a garden or day cell to open its relay. Each line is a numbered start-stop period.\n"
            "Bright red text: same start/overlap. Red background: invalid pair. Disabled routines do not cause conflicts.\n"
            "Warnings compare enabled relays on the same day only and do not block saving.",
            anchor="w", justify="left",
        ).grid(row=3, column=0, padx=12, pady=10, sticky="ew")
        self.refresh()

    def open_channel(self, channel: int) -> None:
        self.select_zone(channel)
        self.master.lift()

    def refresh(self) -> None:
        zones, master, dirty, paused, unavailable, first = self.snapshot()
        signature = (zones, master, dirty, paused, unavailable, first, self.hide_disabled.get())
        if signature == self.last_snapshot:
            return
        self.last_snapshot = signature
        for widget in self.table.winfo_children():
            widget.destroy()
        self.cells.clear()
        for column, name in enumerate(("Relay / Garden", *DAY_NAMES)):
            ctk.CTkLabel(self.table, text=name, font=("", 13, "bold")).grid(
                row=0, column=column, padx=3, pady=4, sticky="ew",
            )
        days = [overview_rows(zones, day, master, self.hide_disabled.get()) for day in range(7)]
        channels = [
            channel for channel, zone in enumerate(zones)
            if first <= channel < first + 8
            and channel != master and (zone.enabled or not self.hide_disabled.get())
        ]
        for row_index, channel in enumerate(channels, start=1):
            zone = zones[channel]
            ctk.CTkButton(
                self.table, text=f"{channel + 1}  {zone.name or '(Unnamed)'}\n"
                + ("Enabled" if zone.enabled else "Disabled"),
                width=140, height=68, anchor="w",
                command=lambda index=channel: self.open_channel(index),
            ).grid(row=row_index, column=0, padx=3, pady=3, sticky="nsew")
            for day, rows in enumerate(days):
                periods = {row.period: row for row in rows if row.channel == channel}
                lines = []
                for period in range(1, 5):
                    item = periods.get(period)
                    text = f"{period}: {item.start or '?'}-{item.stop or '?'}" if item else f"{period}: --"
                    if item and item.paused:
                        text += " (Paused)"
                    if item and item.warning:
                        text += " !"
                    lines.append(text)
                invalid = any(item.stop_minute is None for item in periods.values())
                conflict = any(item.warning for item in periods.values())
                cell = ctk.CTkLabel(
                    self.table, text="\n".join(lines), anchor="w", justify="left",
                    fg_color="#503238" if invalid else "#51432d" if conflict else "#242b34",
                    text_color="#ffbdc4" if invalid else "#9ba5b3" if not zone.enabled
                    else "#ff2020" if conflict else "#edf0f4",
                    corner_radius=5, height=68,
                )
                cell.grid(row=row_index, column=day + 1, padx=3, pady=3, sticky="nsew")
                cell.bind("<Button-1>", lambda _event, index=channel: self.open_channel(index))
                self.cells[channel, day] = cell
        text = "UNSAVED EDITS - not yet applied" if dirty else "Saved schedules"
        text += f" / Channels {first + 1}-{first + 8}"
        text += f" / All seven days / {len(channels)} garden relays"
        if not channels:
            text += " / No relays match this filter"
        if paused:
            text += "\nRAIN PAUSE is on; configured times are shown, not running outputs."
        if unavailable:
            text += "\nService unavailable or faulted: saved status may be stale."
        if any(row.stop_minute is None for rows in days for row in rows if row.channel in channels):
            text += "\nInvalid/incomplete pairs (!) cannot be fully checked for conflicts."
        self.summary.configure(text=text)
