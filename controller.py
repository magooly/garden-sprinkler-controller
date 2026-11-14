"""Scheduling, persistence, and relay control independent of the GUI."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Protocol

ZONE_COUNT = 8
MAX_ZONE_COUNT = 16
# Set to None to restore the original eight independent zones, including the
# preserved channel 8 name/schedule. Channel numbers here are zero-based.
MASTER_PUMP_CHANNEL: int | None = 7
MANUAL_SECONDS = 10 * 60
TEST_SECONDS = 10 * 60
MAX_MANUAL_MINUTES = 1440
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WINDOW_COUNT = 4


def parse_manual_duration(value: str) -> int:
    value = value.strip()
    match = re.fullmatch(r"([0-9]+)(?:\.([0-9]+))?\s*([hH]?)", value)
    if not match:
        raise ValueError("Enter minutes, such as 30, or hours, such as 2.5h.")
    whole, fraction, hours = match.groups()
    fraction = fraction or ""
    scale = 10 ** len(fraction)
    amount = int(whole + fraction) * (60 if hours else 1)
    minutes, remainder = divmod(amount, scale)
    if remainder:
        raise ValueError("Duration must convert to whole minutes; no rounding is applied.")
    if not 1 <= minutes <= MAX_MANUAL_MINUTES:
        raise ValueError("Duration must be 1 to 1440 minutes (up to 24h).")
    return minutes


def format_manual_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, remainder = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    if remainder:
        text += f" {remainder} minute{'s' if remainder != 1 else ''}"
    return f"{text} ({minutes} minutes)"


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
    """Convert a 24-hour HMM, HHMM, or HH:MM value into minutes after midnight."""
    if ":" in value:
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Enter a 24-hour time such as 815, 0815, or 08:15.")
        hour, minute = value.split(":")
    else:
        if not re.fullmatch(r"[0-9]{3,4}", value):
            raise ValueError("Enter a 24-hour time such as 815, 0815, or 08:15.")
        hour, minute = value[:-2], value[-2:]
    hours, minutes = int(hour), int(minute)
    if hours > 23 or minutes > 59:
        raise ValueError("Enter a 24-hour time such as 815, 0815, or 08:15.")
    return hours * 60 + minutes


def overlapping_windows(row: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    windows = []
    for slot, (start, stop) in enumerate(zip(row[::2], row[1::2])):
        if not start or not stop:
            continue
        try:
            begin, end = parse_time(start), parse_time(stop)
        except ValueError:
            continue
        if end > begin:
            windows.append((begin, end, slot))
    windows.sort(key=lambda window: window[0])
    conflicts = []
    for index, first in enumerate(windows):
        for second in windows[index + 1 :]:
            if second[0] >= first[1]:
                break
            conflicts.append((first[2], second[2]))
    return tuple(conflicts)


def next_scheduled_start(zone: Zone, now: datetime) -> datetime | None:
    if not zone.enabled:
        return None
    candidates = []
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        row = zone.schedule[day.weekday()]
        for slot, start in enumerate(row[::2]):
            if start and not zone.paused_windows[slot]:
                minute = parse_time(start)
                candidate = datetime.combine(day, datetime.min.time()).replace(
                    hour=minute // 60, minute=minute % 60,
                )
                if candidate > now:
                    candidates.append(candidate)
    return min(candidates) if candidates else None


@dataclass(frozen=True)
class Zone:
    name: str
    enabled: bool = False
    schedule: tuple[tuple[str, ...], ...] = field(
        default_factory=empty_schedule
    )
    paused_windows: tuple[bool, ...] = (False,) * WINDOW_COUNT
    rain_pause_exempt: bool = False
    pump_exempt: bool = False
    max_manual_minutes: int | None = None
    continuous_reminder_minutes: int | None = None

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Each zone needs a name.")
        if len(self.name) > 40:
            raise ValueError("Zone names must be 40 characters or fewer.")
        if type(self.enabled) is not bool:
            raise ValueError("Routine enabled must be true or false.")
        if type(self.rain_pause_exempt) is not bool:
            raise ValueError("Rain Pause exemption must be true or false.")
        if type(self.pump_exempt) is not bool:
            raise ValueError("Master pump exemption must be true or false.")
        if self.max_manual_minutes is not None and (
            type(self.max_manual_minutes) is not int
            or not 1 <= self.max_manual_minutes <= MAX_MANUAL_MINUTES
        ):
            raise ValueError("Manual maximum must be blank or 1 to 1440 minutes.")
        if self.continuous_reminder_minutes is not None and (
            type(self.continuous_reminder_minutes) is not int
            or not 1 <= self.continuous_reminder_minutes <= MAX_MANUAL_MINUTES
        ):
            raise ValueError("Continuous reminder must be blank or 1 to 1440 minutes.")
        if (
            not isinstance(self.paused_windows, tuple)
            or len(self.paused_windows) != WINDOW_COUNT
            or any(type(paused) is not bool for paused in self.paused_windows)
        ):
            raise ValueError("Each of the four schedule windows must have a true/false pause state.")
        if not isinstance(self.schedule, tuple) or len(self.schedule) != 7:
            raise ValueError("Schedule must contain exactly one row for each weekday.")
        has_window = False
        for day, row in enumerate(self.schedule):
            if not isinstance(row, tuple) or len(row) != WINDOW_COUNT * 2:
                raise ValueError(f"{DAY_NAMES[day]} must have four start/stop pairs.")
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
                has_window = True
            conflicts = overlapping_windows(row)
            if conflicts:
                first, second = conflicts[0]
                raise ValueError(
                    f"{DAY_NAMES[day]} Start/Stop group {first + 1} overlaps "
                    f"Start/Stop group {second + 1}."
                )
        if self.enabled and not has_window:
            raise ValueError("Set at least one daily time window for an enabled routine.")

    def scheduled(self, now: datetime) -> bool:
        return bool(self.scheduled_windows(now))

    def scheduled_windows(self, now: datetime) -> tuple[tuple[str, str], ...]:
        if not self.enabled:
            return ()
        minute = now.hour * 60 + now.minute
        row = self.schedule[now.weekday()]
        return tuple(sorted(
            [
                (start, stop)
                for slot, (start, stop) in enumerate(zip(row[::2], row[1::2]))
                if not self.paused_windows[slot]
                and start and parse_time(start) <= minute < parse_time(stop)
            ],
            key=lambda window: parse_time(window[0]),
        ))


def default_zones(count: int = ZONE_COUNT) -> list[Zone]:
    names = ("Front Lawn", "Back Yard", "Veggies", "Flower Bed")
    return [
        Zone(name=names[index] if index < len(names) else f"Zone {index + 1} (Unused)")
        for index in range(count)
    ]


def validate_zones(zones: list[Zone]) -> None:
    if len(zones) not in (ZONE_COUNT, MAX_ZONE_COUNT):
        raise ValueError("Exactly 8 or 16 zones are required.")
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
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2, 3, 4, 5, 6, 7, 8, 9):
        raise ValueError("Unsupported settings format; expected version 1 through 9.")
    records = data.get("zones")
    if not isinstance(records, list):
        raise ValueError("Settings must contain a list of zones.")
    if data["version"] < 6 and len(records) != ZONE_COUNT:
        raise ValueError("Legacy settings must contain exactly 8 zones.")
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
            expected_fields = (
                {"name", "enabled", "schedule", "paused_windows", "rain_pause_exempt"}
                if data["version"] >= 5 else (
                    {"name", "enabled", "schedule", "paused_windows"}
                    if data["version"] == 4 else {"name", "enabled", "schedule"}
                )
            )
            if data["version"] >= 7:
                expected_fields.add("pump_exempt")
            if data["version"] >= 8:
                expected_fields.add("max_manual_minutes")
            if data["version"] >= 9:
                expected_fields.add("continuous_reminder_minutes")
            if set(record) != expected_fields:
                raise ValueError("Zone settings contain unexpected or missing fields.")
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
        paused_windows = (False,) * WINDOW_COUNT
        if data["version"] >= 4:
            values = record["paused_windows"]
            if (
                not isinstance(values, list) or len(values) != WINDOW_COUNT
                or any(type(paused) is not bool for paused in values)
            ):
                raise ValueError("Each zone paused_windows value must contain four booleans.")
            paused_windows = tuple(values)
        rain_pause_exempt = False
        if data["version"] >= 5:
            rain_pause_exempt = record["rain_pause_exempt"]
            if type(rain_pause_exempt) is not bool:
                raise ValueError("Each zone rain_pause_exempt value must be true or false.")
        zones.append(Zone(
            name=record["name"], enabled=record["enabled"], schedule=schedule,
            paused_windows=paused_windows,
            rain_pause_exempt=rain_pause_exempt,
            pump_exempt=record["pump_exempt"] if data["version"] >= 7 else False,
            max_manual_minutes=record["max_manual_minutes"] if data["version"] >= 8 else None,
            continuous_reminder_minutes=record["continuous_reminder_minutes"] if data["version"] >= 9 else None,
        ))
    validate_zones(zones)
    return zones


def save_zones(path: Path, zones: list[Zone]) -> None:
    """Validate before replacing the existing file atomically."""
    _save_json(path, encode_zones(zones))


def encode_zones(zones: list[Zone]) -> dict[str, object]:
    validate_zones(zones)
    return {
        "version": 9,
        "zones": [
            {
                "name": zone.name.strip(),
                "enabled": zone.enabled,
                "schedule": [list(day) for day in zone.schedule],
                "paused_windows": list(zone.paused_windows),
                "rain_pause_exempt": zone.rain_pause_exempt,
                "pump_exempt": zone.pump_exempt,
                "max_manual_minutes": zone.max_manual_minutes,
                "continuous_reminder_minutes": zone.continuous_reminder_minutes,
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
        """Set a channel; requested_channels explicitly opt into queued semantics."""
        ...


@dataclass(frozen=True)
class Run:
    source: str
    deadline: float | None
    window_key: tuple[date, str, str] | None = None
    started: float | None = None


class Controller:
    def __init__(
        self,
        zones: list[Zone],
        backend: RelayBackend,
        clock: Callable[[], float] = time.monotonic,
        *,
        rain_paused: bool = False,
        channel_count: int | None = None,
        activity: Callable[[int | None, str, str, str], None] | None = None,
        available_channels: frozenset[int] | None = None,
    ) -> None:
        validate_zones(zones)
        self.channel_count = len(zones) if channel_count is None else channel_count
        if self.channel_count not in (8, 16) or self.channel_count > len(zones):
            raise ValueError("Configured channel count must be 8 or 16 and fit the zone settings.")
        self.available_channels = (
            frozenset(range(self.channel_count)) if available_channels is None else available_channels
        )
        if not self.available_channels.issubset(range(self.channel_count)) or not frozenset(range(8)).issubset(self.available_channels):
            raise ValueError("Available channels must include the primary board and fit the configured count.")
        self.zones = list(zones)
        self.backend = backend
        self.clock = clock
        self.runs: dict[int, Run] = {}
        self.blocked: dict[int, set[tuple[date, str, str]]] = {}
        self.closed = False
        self.operation_held = False
        self.rain_paused = rain_paused
        self.master_channel = MASTER_PUMP_CHANNEL
        self.pump_on = False
        self.activity = activity
        self.split_groups = self.channel_count == 16
        self.garden_fault = ""
        self.channel_faults: dict[int, str] = {}
        errors: list[Exception] = []
        for channel in self._shutdown_channels():
            if channel < 8 and self.garden_fault:
                continue
            try:
                self._write(channel, False)
            except (OSError, RuntimeError) as exc:
                errors.append(exc)
        if errors:
            self.closed = True
            raise ExceptionGroup("Some relays could not be initialized OFF", errors)

    def fail_station(self, channel: int, error: Exception) -> None:
        self.channel_faults[channel] = str(error)
        self.runs.pop(channel, None)
        self._record(channel, "STATION FAULT", str(error))
        logging.error("Station %s fault; other stations remain enabled: %s", channel + 1, error)

    def _write(self, channel: int, state: bool) -> bool:
        try:
            self.backend.set_relay(channel, state)
        except (OSError, RuntimeError) as error:
            if not self.split_groups:
                raise
            if channel >= 8:
                self.fail_station(channel, error)
                if state:
                    try:
                        self.backend.set_relay(channel, False)
                    except (OSError, RuntimeError) as shutdown:
                        logging.error("Station %s safety OFF not confirmed: %s", channel + 1, shutdown)
                    else:
                        self._record(channel, "OFF", "Safety OFF after failed start; explicit retry required")
            else:
                self.garden_fault = str(error)
                logging.error("Garden group stopped; appliances remain enabled: %s", error)
                self._record(None, "GARDEN FAULT", str(error))
                self.stop_group(0, datetime.now(), reason="Garden fault")
            return False
        return True

    def stop_group(self, bank: int, now: datetime, *, reason: str = "Group stop") -> None:
        if type(bank) is not int or bank not in (0, 8) or bank >= self.channel_count:
            raise ValueError("Choose garden (0) or appliances (8).")
        if not self.split_groups:
            self.stop_all(now, reason=reason)
            return
        for channel in self._shutdown_channels():
            if channel // 8 != bank // 8:
                continue
            try:
                self.backend.set_relay(channel, False)
            except (OSError, RuntimeError) as error:
                if channel >= 8:
                    self.fail_station(channel, error)
                else:
                    if not self.garden_fault:
                        self._record(None, "GARDEN FAULT", str(error))
                    self.garden_fault = str(error)
                    logging.error("Garden OFF not confirmed on station %s: %s", channel + 1, error)
                    self._record(channel, "UNAVAILABLE", str(error))
            else:
                self._record(channel, "OFF", reason)
            self.runs.pop(channel, None)
            if channel == self.master_channel:
                self.pump_on = False
            for window in self._current_windows(channel, now):
                self._block(channel, window, self.blocked)

    def retry_station(self, channel: int, now: datetime) -> None:
        if type(channel) is not int or channel not in self.channel_faults or channel < 8:
            raise ValueError("Choose a faulted appliance station.")
        if self.operation_held or self.closed:
            raise ValueError("Operation is held or controller is closed.")
        if not self._write(channel, False):
            return
        del self.channel_faults[channel]
        self._record(channel, "OFF", "Explicit retry confirmed OFF; select Run to start")
        for window in self._current_windows(channel, now):
            self._block(channel, window, self.blocked)

    def _current_windows(self, channel: int, now: datetime) -> tuple[tuple[date, str, str], ...]:
        if channel == self.master_channel or channel not in self.available_channels:
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

    def _record(self, channel: int | None, event: str, reason: str) -> None:
        if channel in getattr(self.backend, "requested_channels", ()) and event in ("ON", "OFF", "RUN UPDATED"):
            event = "REQUESTED OFF" if event == "OFF" else "REQUESTED ON"
        if self.activity is not None:
            name = "Controller" if channel is None else (
                "Master Pump" if channel == self.master_channel else self.zones[channel].name
            )
            self.activity(channel, name, event, reason)

    def _off(self, channel: int, reason: str) -> None:
        if channel in self.runs:
            pump_stopped = False
            if self._uses_pump(channel) and self.pump_on and not any(
                other != channel and self._uses_pump(other) for other in self.runs
            ):
                if not self._write(self.master_channel, False):
                    return
                self.pump_on = False
                pump_stopped = True
            try:
                if not self._write(channel, False):
                    return
            finally:
                if pump_stopped:
                    self._record(self.master_channel, "OFF", reason)
            del self.runs[channel]
            self._record(channel, "OFF", reason)
        elif channel in getattr(self.backend, "requested_channels", ()):
            self._write(channel, False)
            self._record(channel, "OFF", reason)
        elif self.split_groups and channel >= 8:
            if self._write(channel, False):
                self._record(channel, "OFF", reason)

    def _shutdown_channels(self) -> tuple[int, ...]:
        channels = tuple(channel for channel in sorted(self.available_channels) if channel != self.master_channel)
        return channels if self.master_channel is None else (self.master_channel, *channels)

    def _uses_pump(self, channel: int) -> bool:
        return (
            self.master_channel is not None and channel < self.master_channel
            and not self.zones[channel].pump_exempt
        )

    def _on(self, channel: int, run: Run, now: datetime) -> None:
        try:
            if not self._write(channel, True):
                return
            previous = self.runs.get(channel)
            self.runs[channel] = run
            pump_started = False
            if self._uses_pump(channel) and self.master_channel is not None and not self.pump_on:
                if not self._write(self.master_channel, True):
                    return
                self.pump_on = True
                pump_started = True
            self._record(channel, "ON" if previous is None else "RUN UPDATED", run.source)
            if pump_started:
                self._record(self.master_channel, "ON", "Automatic pump demand")
        except (OSError, RuntimeError) as exc:
            # An uncertain valve/pump ON must never leave scheduling running.
            self.closed = True
            try:
                self.stop_all(now, suppress=False, reason="Start failure")
            except ExceptionGroup as shutdown_error:
                raise ExceptionGroup(
                    "Watering start failed; shutdown also failed",
                    [exc, *shutdown_error.exceptions],
                ) from exc
            raise

    def stop(self, channel: int, now: datetime, *, reason: str = "Stop channel") -> None:
        """Stop immediately and suppress only currently active schedule windows."""
        if channel == self.master_channel:
            raise RuntimeError("The master pump is controlled automatically by garden zones.")
        run = self.runs.get(channel)
        self._off(channel, reason)
        if run is not None and run.source == "Routine":
            self._block(channel, run.window_key, self.blocked)
        for window in self._current_windows(channel, now):
            self._block(channel, window, self.blocked)

    def start_manual(
        self, channel: int, now: datetime, *, test: bool = False, continuous: bool = False,
        duration_minutes: int = 10,
    ) -> None:
        if test and continuous:
            raise ValueError("A test run cannot be continuous.")
        if type(duration_minutes) is not int or not 1 <= duration_minutes <= MAX_MANUAL_MINUTES:
            raise ValueError("Manual duration must be 1 to 1440 minutes.")
        if self.closed:
            raise RuntimeError("Controller is closed.")
        if self.operation_held:
            raise ValueError("Operation is held after restore. Review devices and explicitly resume operation first.")
        if type(channel) is not int or channel not in range(self.channel_count):
            raise ValueError("Relay channel is not configured.")
        if channel not in self.available_channels:
            raise ValueError("This channel has no assigned output device.")
        if channel in self.channel_faults:
            raise ValueError("This appliance station is faulted. Use Retry station to confirm OFF before starting.")
        if channel < 8 and self.garden_fault:
            raise ValueError("Garden group is faulted. Check hardware and restart the controller.")
        if channel == self.master_channel:
            raise RuntimeError("The master pump is controlled automatically by garden zones.")
        if self.rain_paused and not self.zones[channel].rain_pause_exempt:
            raise RuntimeError("Rain Pause is enabled. All watering is disabled.")
        if continuous and self._uses_pump(channel):
            raise ValueError("Continuous runs require the saved master pump exclusion to be enabled.")
        maximum = self.zones[channel].max_manual_minutes
        if continuous and maximum is not None:
            raise ValueError("Until stopped is unavailable while a manual maximum is set.")
        duration = TEST_SECONDS if test else duration_minutes * 60
        if maximum is not None and not continuous and duration > maximum * 60:
            raise ValueError(f"Manual duration exceeds this channel's {maximum}-minute maximum.")
        active_windows = self._current_windows(channel, now)
        self._on(channel, Run(
            "Continuous" if continuous else "Test" if test else "Manual",
            None if continuous else self.clock() + duration,
            started=self.clock() if continuous else None,
        ), now)
        for window in active_windows:
            self._block(channel, window, self.blocked)

    def toggle_test(self, channel: int, now: datetime) -> None:
        if self.closed:
            raise RuntimeError("Controller is closed.")
        run = self.runs.get(channel)
        if run is not None:
            if run.source != "Test":
                raise RuntimeError("Stop the current watering run before starting a test.")
            self.stop(channel, now, reason="Manual maximum changed")
        else:
            self.start_manual(channel, now, test=True)

    def apply(self, zones: list[Zone], now: datetime) -> None:
        validate_zones(zones)
        if len(zones) != len(self.zones):
            raise ValueError("Zone count changed; reopen the desktop before editing.")
        # A changed routine cancels its scheduled run; manual runs are independent.
        for channel, zone in enumerate(zones):
            previous = self.zones[channel]
            for slot, (was_paused, paused) in enumerate(zip(previous.paused_windows, zone.paused_windows)):
                if was_paused and not paused:
                    start, stop = zone.schedule[now.weekday()][slot * 2:slot * 2 + 2]
                    if start and stop and parse_time(start) <= now.hour * 60 + now.minute < parse_time(stop):
                        self._block(channel, (now.date(), start, stop), self.blocked)
            run = self.runs.get(channel)
            if (
                run is not None and run.source in ("Manual", "Test", "Continuous")
                and zone.max_manual_minutes is not None
                and (previous.max_manual_minutes is None
                     or zone.max_manual_minutes < previous.max_manual_minutes)
            ):
                self.stop(channel, now, reason="Pump exclusion removed")
                continue
            if run is not None and run.source == "Continuous" and channel < 8 and not zone.pump_exempt:
                self.stop(channel, now)
                continue
            if replace(
                zone, name=previous.name, pump_exempt=previous.pump_exempt,
                max_manual_minutes=previous.max_manual_minutes,
                continuous_reminder_minutes=previous.continuous_reminder_minutes,
                paused_windows=previous.paused_windows,
            ) != previous:
                run = self.runs.get(channel)
                if run is not None and (
                    run.source == "Routine"
                    or (self.rain_paused and previous.rain_pause_exempt
                        and not zone.rain_pause_exempt)
                ):
                    self._off(channel, "Settings changed")
            elif (
                run is not None and run.source == "Routine" and run.window_key is not None
                and previous.paused_windows != zone.paused_windows
                and run.window_key[1:] not in zone.scheduled_windows(now)
            ):
                self._off(channel, "Schedule group paused")
        self.zones = list(zones)
        if self.master_channel is not None:
            pump_needed = any(self._uses_pump(channel) for channel in self.runs)
            if pump_needed != self.pump_on:
                try:
                    if self._write(self.master_channel, pump_needed):
                        self.pump_on = pump_needed
                        self._record(self.master_channel, "ON" if pump_needed else "OFF", "Pump settings changed")
                except (OSError, RuntimeError) as exc:
                    self.closed = True
                    try:
                        self.stop_all(now, suppress=False, reason="Pump failure")
                    except ExceptionGroup as shutdown:
                        raise ExceptionGroup(
                            "Pump update failed; shutdown also failed",
                            [exc, *shutdown.exceptions],
                        ) from exc
                    raise
        self.tick(now)

    def tick(self, now: datetime) -> None:
        if self.closed or self.operation_held:
            return
        monotonic_now = self.clock()
        for channel, zone in enumerate(self.zones):
            if channel in self.channel_faults or (channel < 8 and self.garden_fault):
                continue
            if self.rain_paused and not zone.rain_pause_exempt:
                continue
            if channel in self.blocked:
                self.blocked[channel] = {
                    window for window in self.blocked[channel] if window[0] == now.date()
                }
                if not self.blocked[channel]:
                    del self.blocked[channel]
            windows = self._current_windows(channel, now)
            run = self.runs.get(channel)
            if run is not None:
                if (run.deadline is not None and monotonic_now >= run.deadline) or (
                    run.source == "Routine" and run.window_key not in windows
                ):
                    if run.source == "Routine":
                        self._block(channel, run.window_key, self.blocked)
                    self._off(channel, "Schedule ended" if run.source == "Routine" else "Manual timer ended")
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
            errors: list[Exception] = []
            stopped: list[int] = []
            requested = getattr(self.backend, "requested_channels", ())
            for channel in requested:
                if not self.zones[channel].rain_pause_exempt:
                    if not self._write(channel, False):
                        continue
                    self._record(channel, "OFF", "Rain Pause")
            exempt_run_active = any(
                self._uses_pump(channel) and zone.rain_pause_exempt
                and channel in self.runs
                for channel, zone in enumerate(self.zones)
            )
            for channel in self._shutdown_channels():
                if channel == self.master_channel:
                    if exempt_run_active:
                        continue
                elif self.zones[channel].rain_pause_exempt:
                    continue
                try:
                    if channel not in requested:
                        if not self._write(channel, False):
                            continue
                    if channel == self.master_channel:
                        if self.pump_on:
                            stopped.append(channel)
                        self.pump_on = False
                    else:
                        if channel in self.runs and channel not in requested:
                            stopped.append(channel)
                        self.runs.pop(channel, None)
                except (OSError, RuntimeError) as exc:
                    errors.append(exc)
            for channel in stopped:
                self._record(channel, "OFF", "Rain Pause")
            if errors:
                raise ExceptionGroup("Some relays could not be switched off", errors)
            self.tick(now)
        else:
            self.tick(now)

    def stop_all(self, now: datetime, *, suppress: bool = True, reason: str = "Stop all") -> None:
        if self.split_groups:
            for channel in getattr(self.backend, "requested_channels", ()):
                self.backend.set_relay(channel, False)
            self.stop_group(0, now, reason=reason)
            self.stop_group(8, now, reason=reason)
            return
        errors: list[Exception] = []
        stopped: list[int] = []
        requested = getattr(self.backend, "requested_channels", ())
        for channel in requested:
            self.backend.set_relay(channel, False)
            self._record(channel, "OFF", reason)
        for channel in self._shutdown_channels():
            try:
                # Send OFF even when local state already says OFF.
                if channel not in requested:
                    self.backend.set_relay(channel, False)
                if channel == self.master_channel:
                    if self.pump_on:
                        stopped.append(channel)
                    self.pump_on = False
                run = self.runs.get(channel)
                if run is not None and channel not in requested:
                    stopped.append(channel)
                if suppress and run is not None and run.source == "Routine":
                    self._block(channel, run.window_key, self.blocked)
                if suppress:
                    for window in self._current_windows(channel, now):
                        self._block(channel, window, self.blocked)
                self.runs.pop(channel, None)
            except (OSError, RuntimeError) as exc:
                errors.append(exc)
        for channel in stopped:
            self._record(channel, "OFF", reason)
        if errors:
            raise ExceptionGroup("Some relays could not be switched off", errors)

    def close(self, now: datetime) -> None:
        self.closed = True
        self.stop_all(now, reason="Controller shutdown")
