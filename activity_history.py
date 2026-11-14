"""Bounded, persistent history of controller-reported activity."""

from datetime import datetime, timedelta, timezone
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3

HISTORY_LIMIT = 1000
HISTORY_PAGE_SIZE = 25


class ActivityHistory:
    def __init__(self, config: Path) -> None:
        self.path = config.with_name(config.name + ".activity.sqlite3")
        self.error = ""

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=0.25)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, channel INTEGER, "
                "name TEXT NOT NULL, event TEXT NOT NULL, reason TEXT NOT NULL)"
            )
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def record(self, channel: int | None, name: str, event: str, reason: str) -> None:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO events (timestamp, channel, name, event, reason) VALUES (?, ?, ?, ?, ?)",
                        (datetime.now().astimezone().isoformat(timespec="seconds"),
                         channel, name, event, reason[:300]),
                    )
                    connection.execute(
                        "DELETE FROM events WHERE id NOT IN "
                        "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (HISTORY_LIMIT,),
                    )
        except (OSError, sqlite3.Error) as error:
            self.error = "Some events may be missing: " + str(error)
            logging.error("Activity history could not be saved: %s", error)

    def page(self, offset: int, category: str = "all") -> dict[str, object]:
        if type(offset) is not int or not 0 <= offset < HISTORY_LIMIT or offset % HISTORY_PAGE_SIZE:
            raise ValueError("Invalid history page.")
        conditions = {"all": ("", ()), "system": (" WHERE channel IS NULL", ()),
                      "faults": (" WHERE event IN (?, ?, ?)", ("FAULT", "GARDEN FAULT", "STATION FAULT"))}
        if not isinstance(category, str):
            raise ValueError("Invalid history filter.")
        if category not in conditions:
            if not category.isascii() or not category.isdecimal() or len(category) > 2:
                raise ValueError("Invalid history filter.")
            channel = int(category)
            if channel not in range(16):
                raise ValueError("Invalid history filter.")
            conditions[category] = (" WHERE channel = ?", (channel,))
        where, params = conditions[category]
        try:
            with closing(self._connect()) as connection:
                count = connection.execute("SELECT COUNT(*) FROM events" + where, params).fetchone()[0]
                rows = connection.execute(
                    "SELECT timestamp, channel, name, event, reason FROM events"
                    + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*params, HISTORY_PAGE_SIZE, offset),
                ).fetchall()
        except (OSError, sqlite3.Error) as error:
            logging.error("Activity history could not be read: %s", error)
            raise OSError("Activity history is unavailable: " + str(error)) from error
        return {
            "events": [
                {"timestamp": timestamp, "channel": channel, "name": name,
                 "event": event, "reason": reason}
                for timestamp, channel, name, event, reason in rows
            ],
            "offset": offset, "total": count, "page_size": HISTORY_PAGE_SIZE,
            "warning": self.error,
        }

    def timeline(self, channel: int, now: datetime | None = None) -> dict[str, object]:
        if type(channel) is not int or channel not in range(8, 16):
            raise ValueError("Choose a Tapo channel.")
        end = (now or datetime.now().astimezone()).astimezone(timezone.utc)
        start = end - timedelta(hours=24)
        try:
            with closing(self._connect()) as connection:
                records = connection.execute(
                    "SELECT timestamp, channel, event, reason FROM events "
                    "WHERE channel = ? OR channel IS NULL ORDER BY id", (channel,),
                ).fetchall()
            events = sorted(
                ((datetime.fromisoformat(stamp).astimezone(timezone.utc), owner, event, reason)
                 for stamp, owner, event, reason in records),
                key=lambda item: item[0],
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.error("Tapo switching history could not be read: %s", error)
            raise OSError("Tapo switching history is unavailable.") from error
        state = "Unknown"
        cursor = start
        segments = []
        visible = []
        for stamp, owner, event, reason in events:
            if stamp > end:
                continue
            next_state = (
                event if owner == channel and event in ("ON", "OFF")
                else "Unknown" if owner == channel and event in ("UNAVAILABLE", "STATION FAULT")
                else "Unknown" if owner is None and event in ("STARTED", "STOPPED", "FAULT", "RESTORED")
                else None
            )
            if next_state is None:
                continue
            if stamp >= start:
                visible.append({"timestamp": stamp.isoformat(), "event": next_state, "reason": reason})
                if stamp > cursor:
                    segments.append({
                        "start": cursor.isoformat(), "end": stamp.isoformat(),
                        "state": state, "seconds": (stamp - cursor).total_seconds(),
                    })
                cursor = stamp
            state = next_state
        requested_segments = []
        requested_state = "Unknown"
        requested_cursor = start
        for stamp, owner, event, reason in events:
            if stamp > end:
                continue
            requested = (
                event.removeprefix("REQUESTED ") if owner == channel and event in ("REQUESTED ON", "REQUESTED OFF")
                else "OFF" if owner is None and event in ("STARTED", "STOPPED", "FAULT", "RESTORED")
                else None
            )
            if requested is None:
                continue
            if stamp >= start:
                if owner == channel:
                    visible.append({"timestamp": stamp.isoformat(), "event": event, "reason": reason})
                if stamp > requested_cursor:
                    requested_segments.append({
                        "start": requested_cursor.isoformat(), "end": stamp.isoformat(),
                        "state": requested_state, "seconds": (stamp - requested_cursor).total_seconds(),
                    })
                requested_cursor = stamp
            requested_state = requested
        requested_segments.append({
            "start": requested_cursor.isoformat(), "end": end.isoformat(),
            "state": requested_state, "seconds": (end - requested_cursor).total_seconds(),
        })
        visible.sort(key=lambda row: row["timestamp"])
        if cursor < end:
            segments.append({
                "start": cursor.isoformat(), "end": end.isoformat(),
                "state": state, "seconds": (end - cursor).total_seconds(),
            })
        if self.error:
            segments = [{"start": start.isoformat(), "end": end.isoformat(),
                         "state": "Unknown", "seconds": (end - start).total_seconds()}]
            requested_segments = [{"start": start.isoformat(), "end": end.isoformat(),
                                   "state": "Unknown", "seconds": (end - start).total_seconds()}]
        return {
            "channel": channel, "start": start.isoformat(), "end": end.isoformat(),
            "segments": segments, "events": visible,
            "requested_segments": requested_segments,
            "on_seconds": sum(row["seconds"] for row in segments if row["state"] == "ON"),
            "warning": self.error,
            "description": "Controller switching history only. Physical-button and Tapo-app changes "
                           "are not tracked. Unknown includes missing history and controller restarts.",
        }
