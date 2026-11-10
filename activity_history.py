"""Bounded, persistent history of controller-reported activity."""

from datetime import datetime
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
                      "faults": (" WHERE event = ?", ("FAULT",))}
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
