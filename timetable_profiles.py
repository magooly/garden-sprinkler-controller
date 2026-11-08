"""Named schedule-only profiles stored separately from relay settings."""

import json
from dataclasses import replace
from pathlib import Path

from controller import Zone, _save_json, validate_zones

RECOVERY_PROFILE = "Before last load"


def timetable(zones: list[Zone]) -> list[dict[str, object]]:
    validate_zones(zones)
    return [
        {
            "enabled": zone.enabled,
            "schedule": [list(row) for row in zone.schedule],
            "paused_windows": list(zone.paused_windows),
        }
        for zone in zones
    ]


def apply_timetable(data: object, zones: list[Zone]) -> list[Zone]:
    if not isinstance(data, list) or len(data) != len(zones):
        raise ValueError("Timetable must have the same channel count as the current settings.")
    result = []
    for record, zone in zip(data, zones):
        if not isinstance(record, dict) or set(record) != {"enabled", "schedule", "paused_windows"}:
            raise ValueError("Timetable entries must contain only enabled, schedule, and paused_windows.")
        schedule = record["schedule"]
        paused = record["paused_windows"]
        if not isinstance(schedule, list) or any(not isinstance(row, list) for row in schedule):
            raise ValueError("Timetable schedule must contain weekday lists.")
        if not isinstance(paused, list):
            raise ValueError("Timetable paused_windows must be a list.")
        result.append(replace(
            zone, enabled=record["enabled"],
            schedule=tuple(tuple(row) for row in schedule), paused_windows=tuple(paused),
        ))
    validate_zones(result)
    return result


def profile_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValueError("Enter a timetable name of 1-40 characters.")
    return name.strip()


class TimetableProfiles:
    def __init__(self, settings_path: Path) -> None:
        self.path = settings_path.with_name(settings_path.name + ".timetables.json")
        self.profiles: dict[str, list[dict[str, object]]] = {}
        self.selected = ""
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict) or set(data) != {"version", "selected", "profiles"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["profiles"], dict) or not isinstance(data["selected"], str)
            ):
                raise ValueError(f"Invalid timetable profile file: {self.path}")
            for name, records in data["profiles"].items():
                if profile_name(name) != name or not isinstance(records, list) or len(records) not in (8, 16):
                    raise ValueError(f"Invalid timetable profile: {name}")
                validated = apply_timetable(records, [Zone(f"Channel {i + 1}") for i in range(len(records))])
                self.profiles[name] = timetable(validated)
            self.selected = data["selected"]
            if self.selected and self.selected not in self.profiles:
                raise ValueError("Selected timetable is missing from the profile file.")

    def _write(self, profiles: dict[str, list[dict[str, object]]], selected: str) -> None:
        _save_json(self.path, {"version": 1, "selected": selected, "profiles": profiles})
        self.profiles = profiles
        self.selected = selected

    def save(self, name: object, zones: list[Zone]) -> None:
        name = profile_name(name)
        if name == RECOVERY_PROFILE:
            raise ValueError(f'"{RECOVERY_PROFILE}" is reserved for automatic recovery.')
        self._write({**self.profiles, name: timetable(zones)}, name)

    def load(self, name: object, zones: list[Zone]) -> list[Zone]:
        name = profile_name(name)
        if name not in self.profiles:
            raise ValueError("Choose an existing timetable profile.")
        result = apply_timetable(self.profiles[name], zones)
        profiles = dict(self.profiles)
        # Loading recovery swaps it with the current timetable, allowing undo.
        if name == RECOVERY_PROFILE:
            self._write({**profiles, RECOVERY_PROFILE: timetable(zones)}, "")
        else:
            self._write({**profiles, RECOVERY_PROFILE: timetable(zones)}, name)
        return result

    def status(self, zones: list[Zone]) -> dict[str, object]:
        return {
            "names": sorted(self.profiles),
            "selected": self.selected,
            "modified": bool(self.selected and self.profiles[self.selected] != timetable(zones)),
        }
