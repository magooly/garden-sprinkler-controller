"""Scheduling, persistence, and relay control independent of the GUI."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Mapping, Protocol

ZONE_COUNT = 8
# Set to None to restore the original eight independent zones, including the
# preserved channel 8 name/schedule. Channel numbers here are zero-based.
MASTER_PUMP_CHANNEL: int | None = 7
MANUAL_SECONDS = 10 * 60
TEST_SECONDS = 10 * 60
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WINDOW_COUNT = 4


def empty_schedule() -> tuple[tuple[str, ...], ...]:
    return (("",) * (WINDOW_COUNT * 2),) * 7


def legacy_schedule(
    days: tuple[int, ...], start: str, stop: str
) -> tuple[tuple[str, ...], ...]:
    schedule = list(empty_schedule())
    for day in days:
        if type(day) is int and day in range(7):
            schedule[day] = (start, stop) + ("",) * 6
    return tuple(schedule)


def copy_day_times(
    schedule: tuple[tuple[str, ...], ...],
    source_day: int,
) -> tuple[tuple[str, ...], ...]:
    if len(schedule) != len(DAY_NAMES) or type(source_day) is not int or source_day not in range(7):
        raise ValueError("Choose a valid weekday from a seven-day schedule.")
    source = schedule[source_day]
    if not isinstance(source, tuple) or len(source) != WINDOW_COUNT * 2:
        raise ValueError("The selected day must contain four start/stop pairs.")
    return tuple(source for _ in DAY_NAMES)


def parse_time(value: str) -> int:
    """Convert a 24-hour HHMM or HH:MM value into minutes after midnight."""
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):?[0-5][0-9]", value):
        raise ValueError("Enter a 24-hour time such as 1620 or 16:20 (four digits).")
    digits = value.replace(":", "")
    return int(digits[:2]) * 60 + int(digits[2:])


@dataclass(frozen=True)
class Zone:
    name: str
    enabled: bool = False
    schedule: tuple[tuple[str, ...], ...] = field(
        default_factory=empty_schedule
    )

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Each zone needs a name.")
        if len(self.name) > 40:
            raise ValueError("Zone names must be 40 characters or fewer.")
        if type(self.enabled) is not bool:
            raise ValueError("Routine enabled must be true or false.")
        if not isinstance(self.schedule, tuple) or len(self.schedule) != 7:
            raise ValueError("Schedule must contain exactly one row for each weekday.")
        has_window = False
        for day, row in enumerate(self.schedule):
            if not isinstance(row, tuple) or len(row) != WINDOW_COUNT * 2:
                raise ValueError(f"{DAY_NAMES[day]} must have four start/stop pairs.")
            previous_stop = -1
            for slot in range(WINDOW_COUNT):
                start, stop = row[slot * 2 : slot * 2 + 2]
                if not isinstance(start, str) or not isinstance(stop, str):
                    raise ValueError(f"{DAY_NAMES[day]} times must be strings.")
                if not start and not stop:
                    continue
                if not start or not stop:
                    raise ValueError(
                        f"{DAY_NAMES[day]} window {slot + 1}: enter both start and stop times."
                    )
                start_minute, stop_minute = parse_time(start), parse_time(stop)
                if stop_minute <= start_minute:
                    raise ValueError(
                        f"{DAY_NAMES[day]} window {slot + 1}: stop must be later than start."
                    )
                if start_minute < previous_stop:
                    raise ValueError(
                        f"{DAY_NAMES[day]} window {slot + 1} must start at or after "
                        "the previous window ends."
                    )
                previous_stop = stop_minute
                has_window = True
        if self.enabled and not has_window:
            raise ValueError("Set at least one daily time window for an enabled routine.")

    def scheduled(self, now: datetime) -> bool:
        return bool(self.scheduled_windows(now))

    def scheduled_windows(self, now: datetime) -> tuple[tuple[str, str], ...]:
        if not self.enabled:
            return ()
        minute = now.hour * 60 + now.minute
        row = self.schedule[now.weekday()]
        return tuple(
            (start, stop)
            for start, stop in zip(row[::2], row[1::2])
            if start and parse_time(start) <= minute < parse_time(stop)
        )


def default_zones() -> list[Zone]:
    names = ("Front Lawn", "Back Yard", "Veggies", "Flower Bed")
    return [
        Zone(name=names[index] if index < len(names) else f"Zone {index + 1} (Unused)")
        for index in range(ZONE_COUNT)
    ]


def validate_zones(zones: list[Zone]) -> None:
    if len(zones) != ZONE_COUNT:
        raise ValueError(f"Exactly {ZONE_COUNT} zones are required.")
    for index, zone in enumerate(zones):
        try:
            zone.validate()
        except ValueError as exc:
            raise ValueError(f"Zone {index + 1}: {exc}") from exc


def load_zones(path: Path) -> list[Zone]:
    if not path.exists():
        return default_zones()
    return decode_zones(json.loads(path.read_text(encoding="utf-8")))


def decode_zones(data: object) -> list[Zone]:
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2, 3):
        raise ValueError("Unsupported settings format; expected version 1, 2, or 3.")
    records = data.get("zones")
    if not isinstance(records, list):
        raise ValueError("Settings must contain a list of zones.")
    zones = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Each zone setting must be an object.")
        if data["version"] == 1:
            if set(record) != {"name", "enabled", "days", "start", "stop"}:
                raise ValueError("Version 1 zones must contain name, enabled, days, start, and stop.")
            if not isinstance(record["days"], list):
                raise ValueError("Version 1 zone days must be a list.")
            if any(type(day) is not int or day not in range(7) for day in record["days"]):
                raise ValueError("Version 1 zone days must be weekday numbers from 0 through 6.")
            if len(set(record["days"])) != len(record["days"]):
                raise ValueError("Version 1 zone weekdays cannot be duplicated.")
            if not isinstance(record["start"], str) or not isinstance(record["stop"], str):
                raise ValueError("Version 1 zone start and stop times must be strings.")
            if parse_time(record["stop"]) <= parse_time(record["start"]):
                raise ValueError("Version 1 zone stop time must be later than its start time.")
            schedule = legacy_schedule(tuple(record["days"]), record["start"], record["stop"])
        else:
            if set(record) != {"name", "enabled", "schedule"}:
                raise ValueError("Zones must contain name, enabled, and schedule.")
            if not isinstance(record["schedule"], list):
                raise ValueError("Zone schedule must be a list.")
            expected_times = 4 if data["version"] == 2 else WINDOW_COUNT * 2
            if any(
                not isinstance(row, list) or len(row) != expected_times
                for row in record["schedule"]
            ):
                raise ValueError(f"Each daily schedule must be a list of {expected_times} times.")
            padding = ("",) * (WINDOW_COUNT * 2 - expected_times)
            schedule = tuple(tuple(row) + padding for row in record["schedule"])
        zones.append(Zone(name=record["name"], enabled=record["enabled"], schedule=schedule))
    validate_zones(zones)
    return zones


def save_zones(path: Path, zones: list[Zone]) -> None:
    """Validate before replacing the existing file atomically."""
    _save_json(path, encode_zones(zones))


def encode_zones(zones: list[Zone]) -> dict[str, object]:
    validate_zones(zones)
    return {
        "version": 3,
        "zones": [
            {
                "name": zone.name.strip(),
                "enabled": zone.enabled,
                "schedule": [list(day) for day in zone.schedule],
            }
            for zone in zones
        ],
    }
def _save_json(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def rain_pause_path(settings_path: Path) -> Path:
    return settings_path.with_name(settings_path.name + ".rain-pause.json")


def load_rain_pause(settings_path: Path) -> bool:
    path = rain_pause_path(settings_path)
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or set(data) != {"rain_paused"}
        or type(data["rain_paused"]) is not bool
    ):
        raise ValueError(f"Invalid rain pause state in {path}; expected a rain_paused boolean.")
    return data["rain_paused"]


def save_rain_pause(settings_path: Path, paused: bool) -> None:
    if type(paused) is not bool:
        raise ValueError("Rain pause must be true or false.")
    _save_json(rain_pause_path(settings_path), {"rain_paused": paused})


class RelayBackend(Protocol):
    """The controller initializes outputs OFF; adapters raise on failure."""

    def set_relay(self, channel: int, energized: bool) -> None:
        """Set a zero-based channel; return only when the command succeeds."""
        ...


@dataclass(frozen=True)
class Run:
    source: str
    deadline: float
    window_key: tuple[date, str, str] | None = None


class Controller:
    def __init__(
        self,
        zones: list[Zone],
        backend: RelayBackend,
        clock: Callable[[], float] = time.monotonic,
        *,
        rain_paused: bool = False,
    ) -> None:
        validate_zones(zones)
        self.zones = list(zones)
        self.backend = backend
        self.clock = clock
        self.runs: dict[int, Run] = {}
        self.blocked: dict[int, set[tuple[date, str, str]]] = {}
        self.closed = False
        self.rain_paused = rain_paused
        self.master_channel = MASTER_PUMP_CHANNEL
        self.pump_on = False
        errors: list[Exception] = []
        for channel in self._shutdown_channels():
            try:
                self.backend.set_relay(channel, False)
            except (OSError, RuntimeError) as exc:
                errors.append(exc)
        if errors:
            self.closed = True
            raise ExceptionGroup("Some relays could not be initialized OFF", errors)

    def _current_windows(self, channel: int, now: datetime) -> tuple[tuple[date, str, str], ...]:
        if channel == self.master_channel:
            return ()
        return tuple(
            (now.date(), start, stop)
            for start, stop in self.zones[channel].scheduled_windows(now)
        )

    @staticmethod
    def _block(channel: int, window: tuple[date, str, str] | None,
               blocked: dict[int, set[tuple[date, str, str]]]) -> None:
        if window is not None:
            blocked.setdefault(channel, set()).add(window)

    def _off(self, channel: int) -> None:
        if channel in self.runs:
            if self.master_channel is not None and self.pump_on and len(self.runs) == 1:
                self.backend.set_relay(self.master_channel, False)
                self.pump_on = False
            self.backend.set_relay(channel, False)
            del self.runs[channel]

    def _shutdown_channels(self) -> tuple[int, ...]:
        channels = tuple(channel for channel in range(ZONE_COUNT) if channel != self.master_channel)
        return channels if self.master_channel is None else (self.master_channel, *channels)

    def _on(self, channel: int, run: Run, now: datetime) -> None:
        try:
            self.backend.set_relay(channel, True)
            self.runs[channel] = run
            if self.master_channel is not None and not self.pump_on:
                self.backend.set_relay(self.master_channel, True)
                self.pump_on = True
        except (OSError, RuntimeError) as exc:
            # An uncertain valve/pump ON must never leave scheduling running.
            self.closed = True
            try:
                self.stop_all(now, suppress=False)
            except ExceptionGroup as shutdown_error:
                raise ExceptionGroup(
                    "Watering start failed; shutdown also failed",
                    [exc, *shutdown_error.exceptions],
                ) from exc
            raise

    def stop(self, channel: int, now: datetime) -> None:
        """Stop immediately and suppress only currently active schedule windows."""
        if channel == self.master_channel:
            raise RuntimeError("The master pump is controlled automatically by garden zones.")
        run = self.runs.get(channel)
        self._off(channel)
        if run is not None and run.source == "Routine":
            self._block(channel, run.window_key, self.blocked)
        for window in self._current_windows(channel, now):
            self._block(channel, window, self.blocked)

    def start_manual(self, channel: int, now: datetime, *, test: bool = False) -> None:
        if self.closed:
            raise RuntimeError("Controller is closed.")
        if self.rain_paused:
            raise RuntimeError("Rain Pause is enabled. All watering is disabled.")
        if channel == self.master_channel:
            raise RuntimeError("The master pump is controlled automatically by garden zones.")
        duration = TEST_SECONDS if test else MANUAL_SECONDS
        active_windows = self._current_windows(channel, now)
        self._on(channel, Run("Test" if test else "Manual", self.clock() + duration), now)
        for window in active_windows:
            self._block(channel, window, self.blocked)

    def toggle_test(self, channel: int, now: datetime) -> None:
        if self.closed:
            raise RuntimeError("Controller is closed.")
        run = self.runs.get(channel)
        if run is not None:
            if run.source != "Test":
                raise RuntimeError("Stop the current watering run before starting a test.")
            self.stop(channel, now)
        else:
            self.start_manual(channel, now, test=True)

    def apply(self, zones: list[Zone], now: datetime) -> None:
        validate_zones(zones)
        # A changed routine cancels its scheduled run; manual/test runs are independent.
        for channel, zone in enumerate(zones):
            if replace(zone, name=self.zones[channel].name) != self.zones[channel]:
                run = self.runs.get(channel)
                if run is not None and run.source == "Routine":
                    self._off(channel)
        self.zones = list(zones)
        self.tick(now)

    def tick(self, now: datetime) -> None:
        if self.closed or self.rain_paused:
            return
        monotonic_now = self.clock()
        for channel, zone in enumerate(self.zones):
            if channel in self.blocked:
                self.blocked[channel] = {
                    window for window in self.blocked[channel] if window[0] == now.date()
                }
                if not self.blocked[channel]:
                    del self.blocked[channel]
            windows = self._current_windows(channel, now)
            run = self.runs.get(channel)
            if run is not None:
                if monotonic_now >= run.deadline or (
                    run.source == "Routine" and run.window_key not in windows
                ):
                    if run.source == "Routine":
                        self._block(channel, run.window_key, self.blocked)
                    self._off(channel)
                continue
            next_window = next(
                (window for window in windows if window not in self.blocked.get(channel, set())),
                None,
            )
            if next_window is not None:
                _, start, stop = next_window
                end = now.replace(
                    hour=parse_time(stop) // 60,
                    minute=parse_time(stop) % 60,
                    second=0,
                    microsecond=0,
                )
                self._on(
                    channel,
                    Run("Routine", monotonic_now + (end - now).total_seconds(), next_window),
                    now,
                )

    def set_rain_pause(self, paused: bool, now: datetime) -> None:
        if self.closed:
            raise RuntimeError("Controller is closed.")
        self.rain_paused = paused
        if paused:
            self.stop_all(now, suppress=False)
        else:
            self.tick(now)

    def stop_all(self, now: datetime, *, suppress: bool = True) -> None:
        errors: list[Exception] = []
        for channel in self._shutdown_channels():
            try:
                # Send OFF even when local state already says OFF.
                self.backend.set_relay(channel, False)
                if channel == self.master_channel:
                    self.pump_on = False
                run = self.runs.get(channel)
                if suppress and run is not None and run.source == "Routine":
                    self._block(channel, run.window_key, self.blocked)
                if suppress:
                    for window in self._current_windows(channel, now):
                        self._block(channel, window, self.blocked)
                self.runs.pop(channel, None)
            except (OSError, RuntimeError) as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Some relays could not be switched off", errors)

    def close(self, now: datetime) -> None:
        self.closed = True
        self.stop_all(now)
