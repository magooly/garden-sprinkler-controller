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
        if not required.issubset(data) or data["protocol"] != 1:
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
        self.boards = data.get("boards", [
            {"host": data["host"], "port": data["port"], "slave_id": data["slave_id"]}
        ])
        self.revision = data["revision"]
        self.rain_paused = data["rain_paused"]
        self.master_channel = data["master_channel"]
        self.pump_on = data["pump_on"]
        self.fault = data["fault"]
        self.timetables = data.get("timetables")
        self.host = data["host"]
        self.port = data["port"]
        self.slave_id = data["slave_id"]
        self.runs = {
            item["channel"]: Run(item["source"], self.clock() + item["remaining"])
            for item in data["runs"]
        }

    def sync(self) -> None:
        self._update(exchange(self.path, {"command": "status"}))

    def command(self, command: str, **arguments: object) -> None:
        self._update(exchange(self.path, {"command": command, **arguments}))
        if self.fault:
            raise RuntimeError("Service fault: " + self.fault)

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

    def save_profile(self, name: str) -> None:
        if self.timetables is None:
            raise ValueError("Restart the background service to enable timetable profiles.")
        self.command("profile_save", name=name, revision=self.revision)

    def load_profile(self, name: str) -> None:
        if self.timetables is None:
            raise ValueError("Restart the background service to enable timetable profiles.")
        self.command("profile_load", name=name, revision=self.revision)

    def stop_all(self, now: datetime) -> None:
        self.command("stop_all")

    def start_manual(self, channel: int, now: datetime) -> None:
        self.command("manual", channel=channel)

    def toggle_test(self, channel: int, now: datetime) -> None:
        self.command("test", channel=channel)

    def set_rain_pause(self, paused: bool, now: datetime) -> None:
        self.command("rain_pause", paused=paused)

    def read_states(self) -> list[bool]:
        result = exchange(self.path, {"command": "read_states"})
        if "states" not in result:
            self._update(result)
            raise RuntimeError("Service fault: " + self.fault)
        return result["states"]

    def close(self, now: datetime) -> None:
        # Detaching the desktop must not send OFF or stop the headless controller.
        self.closed = True
