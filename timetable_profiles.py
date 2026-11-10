"""Named schedule-only profiles stored separately from relay settings."""

import json
from dataclasses import dataclass, replace
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


@dataclass
class ProfileGroup:
    profiles: dict[str, list[dict[str, object]]]
    selected: str = ""


def channel_bank(zones: list[Zone], bank: object) -> int:
    if type(bank) is not int or bank not in (0, 8) or bank + 8 > len(zones):
        raise ValueError("Choose a configured timetable group: channels 1-8 or 9-16.")
    return bank


class TimetableProfiles:
    def __init__(self, settings_path: Path) -> None:
        self.path = settings_path.with_name(settings_path.name + ".timetables.json")
        self.groups = {bank: ProfileGroup({}) for bank in (0, 8)}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int:
                raise ValueError(f"Invalid timetable profile file: {self.path}")
            if data["version"] == 1:
                self._read_legacy(data)
            elif data["version"] == 2 and set(data) == {"version", "groups"}:
                groups = data["groups"]
                if not isinstance(groups, dict) or set(groups) != {"0", "8"}:
                    raise ValueError("Timetable file must contain both channel groups.")
                for bank in (0, 8):
                    self.groups[bank] = self._read_group(groups[str(bank)], (8,))
            else:
                raise ValueError(f"Invalid timetable profile file: {self.path}")

    @staticmethod
    def _read_group(data: object, counts: tuple[int, ...]) -> ProfileGroup:
        if (
            not isinstance(data, dict) or set(data) != {"selected", "profiles"}
            or not isinstance(data["profiles"], dict) or not isinstance(data["selected"], str)
        ):
            raise ValueError("Invalid timetable group.")
        group = ProfileGroup({})
        for name, records in data["profiles"].items():
            if profile_name(name) != name or not isinstance(records, list) or len(records) not in counts:
                raise ValueError(f"Invalid timetable profile: {name}")
            validated = apply_timetable(records, [Zone(f"Channel {i + 1}") for i in range(len(records))])
            group.profiles[name] = timetable(validated)
        group.selected = data["selected"]
        if group.selected and group.selected not in group.profiles:
            raise ValueError("Selected timetable is missing from the profile file.")
        return group

    def _read_legacy(self, data: dict) -> None:
        if set(data) != {"version", "selected", "profiles"}:
            raise ValueError(f"Invalid timetable profile file: {self.path}")
        legacy = self._read_group({"selected": data["selected"], "profiles": data["profiles"]}, (8, 16))
        for bank in (0, 8):
            profiles = {
                name: records[bank:bank + 8] for name, records in legacy.profiles.items()
                if len(records) >= bank + 8
            }
            self.groups[bank] = ProfileGroup(
                profiles, legacy.selected if legacy.selected in profiles else "",
            )

    def _write(self, bank: int, profiles: dict[str, list[dict[str, object]]], selected: str) -> None:
        groups = {**self.groups, bank: ProfileGroup(profiles, selected)}
        _save_json(self.path, {
            "version": 2,
            "groups": {
                str(index): {"selected": group.selected, "profiles": group.profiles}
                for index, group in groups.items()
            },
        })
        self.groups = groups

    def save(self, name: object, zones: list[Zone], bank: int = 0) -> None:
        bank = channel_bank(zones, bank)
        name = profile_name(name)
        if name == RECOVERY_PROFILE:
            raise ValueError(f'"{RECOVERY_PROFILE}" is reserved for automatic recovery.')
        self._write(bank, {**self.groups[bank].profiles, name: timetable(zones[bank:bank + 8])}, name)

    def load(self, name: object, zones: list[Zone], bank: int = 0) -> list[Zone]:
        bank = channel_bank(zones, bank)
        name = profile_name(name)
        group = self.groups[bank]
        if name not in group.profiles:
            raise ValueError("Choose an existing timetable profile in this channel group.")
        result = list(zones)
        result[bank:bank + 8] = apply_timetable(group.profiles[name], zones[bank:bank + 8])
        # Loading recovery swaps only this bank, allowing independent undo.
        self._write(
            bank, {**group.profiles, RECOVERY_PROFILE: timetable(zones[bank:bank + 8])},
            "" if name == RECOVERY_PROFILE else name,
        )
        return result

    def status(self, zones: list[Zone], bank: int = 0) -> dict[str, object]:
        bank = channel_bank(zones, bank)
        group = self.groups[bank]
        return {
            "names": sorted(group.profiles),
            "selected": group.selected,
            "modified": bool(group.selected and group.profiles[group.selected] != timetable(zones[bank:bank + 8])),
        }
