"""Desktop-side snapshot and commands; this module never drives hardware."""

from datetime import datetime
from pathlib import Path
import time

from controller import Run, decode_zones, encode_zones, Zone
from service import exchange


class ServiceClient:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False
        self.sync()

    @staticmethod
    def clock() -> float:
        return time.monotonic()

    def _update(self, data: dict) -> None:
        required = {
            "protocol", "settings", "revision", "rain_paused", "master_channel",
            "pump_on", "fault", "host", "port", "slave_id", "runs",
        }
        if not required.issubset(data) or data["protocol"] not in (1, 2):
            raise OSError("Incompatible service reply. Update/restart the service and app together.")
        zones = decode_zones(data["settings"])
        channel_count = data.get("channel_count", 8)
        if type(channel_count) is not int or channel_count not in (8, 16) or channel_count > len(zones):
            raise OSError("Invalid configured channel count in service reply.")
        if hasattr(self, "zones") and (
            len(zones) != len(self.zones) or channel_count != self.channel_count
        ):
            raise OSError("Board configuration changed. Close and reopen this window.")
        self.zones = zones
        self.channel_count = channel_count
        available = data.get("available_channels", list(range(channel_count)))
        if (
            not isinstance(available, list) or any(type(channel) is not int or channel not in range(channel_count) for channel in available)
            or not set(range(8)).issubset(available)
        ):
            raise OSError("Invalid available channel list in service reply.")
        self.available_channels = frozenset(available)
        self.tapo = data.get("tapo")
        self.boards = data.get("boards", [
            {"host": data["host"], "port": data["port"], "slave_id": data["slave_id"]}
        ])
        self.revision = data["revision"]
        self.rain_paused = data["rain_paused"]
        self.master_channel = data["master_channel"]
        self.pump_on = data["pump_on"]
        self.fault = data["fault"]
        self.group_controls_supported = data.get("group_controls_supported") is True
        self.garden_fault = data.get("garden_fault", "")
        self.channel_faults = {int(channel): error for channel, error in data.get("channel_faults", {}).items()}
        self.full_backup_supported = data.get("full_backup_supported") is True
        self.operation_held = data.get("operation_held") is True
        self.restore_restart_required = data.get("restore_restart_required") is True
        self.recovery_backup = data.get("recovery_backup", "")
        self.manual_timers_supported = data.get("manual_timers_supported") is True
        self.history_supported = data.get("history_supported") is True
        self.history_filters_supported = data.get("history_filters_supported") is True
        self.continuous_reminders_supported = data.get("continuous_reminders_supported") is True
        self.history_warning = data.get("history_warning", "")
        self.timetables = data.get("timetables")
        self.timetable_groups = data.get("timetable_groups")
        self.host = data["host"]
        self.port = data["port"]
        self.slave_id = data["slave_id"]
        self.runs = {
            item["channel"]: Run(
                item["source"],
                None if item["remaining"] is None else self.clock() + item["remaining"],
                started=None if item.get("elapsed") is None else self.clock() - item["elapsed"],
            )
            for item in data["runs"]
        }

    def sync(self) -> None:
        self._update(exchange(self.path, {"command": "status"}))

    def tapo_details(self, refresh: bool = False) -> dict:
        return exchange(self.path, {"command": "tapo_details", "refresh": refresh})

    def tapo_timeline(self, channel: int) -> dict:
        return exchange(self.path, {"command": "tapo_timeline", "channel": channel})

    def history(self, offset: int, category: str = "all") -> dict:
        if not self.history_supported:
            raise ValueError("Restart the updated controller service to view activity history.")
        if category != "all" and not self.history_filters_supported:
            raise ValueError("Restart the updated controller service to filter history.")
        request = {"command": "history", "offset": offset}
        if category != "all":
            request["filter"] = category
        return exchange(self.path, request)

    def backup(self, command: str, **arguments: object) -> dict:
        if not self.full_backup_supported:
            raise ValueError("Restart the updated controller service to enable encrypted full backups.")
        result = exchange(self.path, {"command": command, **arguments})
        if result.get("fault"):
            raise RuntimeError("Service fault: " + result["fault"])
        if command == "backup_restore":
            self._update(result)
        return result

    def command(self, command: str, **arguments: object) -> None:
        self._update(exchange(self.path, {"command": command, **arguments}))
        if self.fault:
            raise RuntimeError("Service fault: " + self.fault)
        channel = arguments.get("channel")
        if isinstance(channel, int) and channel in self.channel_faults:
            raise ValueError(f"Station {channel + 1} fault; other stations remain enabled: "
                             + self.channel_faults[channel])
        if command in ("manual", "continuous", "test") and isinstance(channel, int) and channel < 8 and self.garden_fault:
            raise ValueError("Garden group fault; appliances remain enabled: " + self.garden_fault)

    def tick(self, now: datetime) -> None:
        self.sync()

    def apply(self, zones: list[Zone], now: datetime) -> None:
        try:
            self.command("save", settings=encode_zones(zones), revision=self.revision)
        except ValueError as exc:
            if str(exc).startswith("Unsupported settings format"):
                raise ValueError(
                    "The background service is older than this desktop app. "
                    "Restart garden-sprinkler.service, then close and reopen the desktop "
                    "before saving again. Your edits have not been applied."
                ) from exc
            raise

    def stop(self, channel: int, now: datetime) -> None:
        self.command("stop", channel=channel)

    def save_profile(self, name: str, bank: int = 0) -> None:
        if self.timetable_groups is None:
            raise ValueError("Restart the background service to enable separate timetable groups.")
        self.command("profile_save", name=name, bank=bank, revision=self.revision)

    def load_profile(self, name: str, bank: int = 0) -> None:
        if self.timetable_groups is None:
            raise ValueError("Restart the background service to enable separate timetable groups.")
        self.command("profile_load", name=name, bank=bank, revision=self.revision)

    def stop_all(self, now: datetime) -> None:
        self.command("stop_all")

    def stop_group(self, bank: int, now: datetime) -> None:
        if bank not in (0, 8) or not self.group_controls_supported:
            raise ValueError("Group controls unavailable; restart the updated controller.")
        self.command("stop_garden" if bank == 0 else "stop_appliances")

    def retry_station(self, channel: int, now: datetime) -> None:
        self.command("retry_station", channel=channel)

    def start_manual(
        self, channel: int, now: datetime, *, duration_minutes: int = 10,
        continuous: bool = False,
    ) -> None:
        if not self.manual_timers_supported:
            raise ValueError("Restart the updated background service before using manual timers.")
        if continuous:
            self.command("continuous", channel=channel)
        else:
            self.command("manual", channel=channel, duration_minutes=duration_minutes)

    def toggle_test(self, channel: int, now: datetime) -> None:
        self.command("test", channel=channel)

    def set_rain_pause(self, paused: bool, now: datetime) -> None:
        self.command("rain_pause", paused=paused)

    def read_states(self) -> list[bool | None]:
        result = exchange(self.path, {"command": "read_states"})
        if "states" not in result:
            self._update(result)
            if self.garden_fault:
                raise ValueError("Garden read failed; appliances remain enabled: " + self.garden_fault)
            raise RuntimeError("Service fault: " + self.fault)
        self.sync()
        return result["states"]

    def close(self, now: datetime) -> None:
        # Detaching the desktop must not send OFF or stop the headless controller.
        self.closed = True
