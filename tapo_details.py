"""On-demand read-only plug telemetry, independent of the switching loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import threading
import time
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3

from kasa import Credentials, Discover, KasaException, Module
from kasa.smart.smartdevice import SmartDevice

from tapo_plugs import Plug, normalize_mac

SAMPLE_INTERVAL_SECONDS = 300
RETENTION_SECONDS = 8 * 86400
HISTORY_HOURS = (6, 24, 168)


async def read_details(plug: Plug, credentials: Credentials) -> dict[str, object]:
    device = None
    try:
        async with asyncio.timeout(5):
            device = await Discover.discover_single(
                plug.host, credentials=credentials, discovery_timeout=1, timeout=1,
            )
            if not isinstance(device, SmartDevice) or normalize_mac(device.mac) != plug.mac:
                raise OSError("Plug identity does not match the assignment.")
            await device.update()
            if device.model.split("(", 1)[0].strip() != "P110M":
                raise OSError("Assigned device is not a P110M.")
            energy = device.modules.get(Module.Energy)
            return {
                "state": device.is_on,
                "power_w": energy.current_consumption if energy else None,
                "today_kwh": energy.consumption_today if energy else None,
                "month_kwh": energy.consumption_this_month if energy else None,
                "voltage_v": energy.voltage if energy else None,
                "current_a": energy.current if energy else None,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": "",
            }
    except (KasaException, TimeoutError, ValueError):
        raise OSError("Plug details unavailable; check connection, credentials and firmware.") from None
    finally:
        if device is not None:
            try:
                async with asyncio.timeout(0.5):
                    await device.disconnect()
            except (KasaException, TimeoutError):
                raise OSError("Plug details connection could not be closed.") from None


class TapoDetails:
    def __init__(self, plugs: tuple[Plug | None, ...], credentials: Credentials | None,
                 config: Path | None = None) -> None:
        self.plugs = plugs
        self.credentials = credentials
        self.lock = threading.Lock()
        self.reading = False
        self.last_started = float("-inf")
        self.rows: dict[int, dict[str, object]] = {}
        self.store = TapoSamples(config) if config is not None else None
        self.last_sample_bucket: int | None = None
        self.failures: dict[int, tuple[str, int]] = {}

    def sample_due(self) -> None:
        bucket = int(time.time()) // SAMPLE_INTERVAL_SECONDS * SAMPLE_INTERVAL_SECONDS
        with self.lock:
            if self.store is None or not any(self.plugs) or self.reading or self.last_sample_bucket == bucket:
                return
            self.reading = True
            self.last_sample_bucket = bucket
            self.last_started = time.monotonic()
            threading.Thread(target=self._sample, args=(bucket,), daemon=True).start()

    def _sample(self, bucket: int) -> None:
        try:
            asyncio.run(self._read(bucket))
        finally:
            with self.lock:
                self.reading = False

    def snapshot(self, names: list[str], refresh: bool = False) -> dict[str, object]:
        with self.lock:
            if refresh and not self.reading and time.monotonic() - self.last_started >= 10:
                self.reading = True
                self.last_started = time.monotonic()
                threading.Thread(target=self._refresh, daemon=True).start()
            return {
                "reading": self.reading,
                "rows": [
                    {
                        "channel": index + 8, "name": names[index + 8],
                        "state": None, "power_w": None, "today_kwh": None,
                        "month_kwh": None, "voltage_v": None, "current_a": None,
                        "updated": None, "error": "",
                        **self.rows.get(index, {}),
                    }
                    for index, plug in enumerate(self.plugs) if plug is not None
                ],
            }

    def _refresh(self) -> None:
        try:
            asyncio.run(self._read())
        finally:
            with self.lock:
                self.reading = False

    async def _read(self, bucket: int | None = None) -> None:
        for index, plug in enumerate(self.plugs):
            if plug is None:
                continue
            try:
                if self.credentials is None:
                    raise OSError("Tapo credentials are unavailable.")
                row = await read_details(plug, self.credentials)
            except OSError as exc:
                previous_error, repeats = self.failures.get(index, ("", 0))
                if previous_error != str(exc) or repeats % 12 == 0:
                    logging.warning("Tapo details for channel %s: %s (repeated failures: %s)",
                                    index + 9, exc, repeats if previous_error == str(exc) else 0)
                self.failures[index] = (str(exc), repeats + 1 if previous_error == str(exc) else 1)
                with self.lock:
                    self.rows[index] = {
                        **self.rows.get(index, {}),
                        "state": None, "power_w": None, "today_kwh": None,
                        "month_kwh": None, "voltage_v": None, "current_a": None,
                        "error": str(exc),
                    }
                row = {"error": str(exc), "updated": None}
            else:
                if index in self.failures:
                    logging.info("Tapo details for channel %s recovered.", index + 9)
                    del self.failures[index]
                with self.lock:
                    self.rows[index] = row
            if bucket is not None and self.store is not None:
                self.store.save(plug.mac, bucket, row)


class TapoSamples:
    """Keep eight days of five-minute samples, separated by plug identity."""

    def __init__(self, config: Path) -> None:
        self.path = config.with_name(config.name + ".tapo-samples.sqlite3")
        self.error = ""

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=0.25)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS samples "
                               "(mac TEXT NOT NULL, bucket INTEGER NOT NULL, data TEXT NOT NULL, "
                               "PRIMARY KEY(mac, bucket))")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def save(self, mac: str, bucket: int, row: dict[str, object]) -> None:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?)",
                                       (mac, bucket, json.dumps(row, allow_nan=False)))
                    connection.execute("DELETE FROM samples WHERE bucket < ?", (bucket - RETENTION_SECONDS,))
            self.error = ""
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.error = "Some telemetry samples could not be saved."
            logging.error("Tapo samples could not be saved: %s", exc)

    def history(self, mac: str, end: datetime, hours: int = 24) -> dict[str, object]:
        if type(hours) is not int or hours not in HISTORY_HOURS:
            raise ValueError("Choose 6, 24 or 168 hours of history.")
        end_seconds = int(end.timestamp())
        start_seconds = end_seconds - hours * 3600
        try:
            with closing(self._connect()) as connection:
                records = connection.execute(
                    "SELECT bucket,data FROM samples WHERE mac=? AND bucket>? AND bucket<=? ORDER BY bucket",
                    (mac, start_seconds, end_seconds),
                ).fetchall()
            rows = {bucket: json.loads(data) for bucket, data in records}
        except (OSError, sqlite3.Error, ValueError) as exc:
            logging.error("Tapo samples could not be read: %s", exc)
            raise OSError("Recorded Tapo readings are unavailable.") from None
        samples = []
        first = start_seconds // SAMPLE_INTERVAL_SECONDS * SAMPLE_INTERVAL_SECONDS + SAMPLE_INTERVAL_SECONDS
        for bucket in range(first, end_seconds + 1, SAMPLE_INTERVAL_SECONDS):
            samples.append({
                "timestamp": datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
                "state": None, "power_w": None, "today_kwh": None, "month_kwh": None,
                "voltage_v": None, "current_a": None, "updated": None,
                "error": "No recorded reading.", **rows.get(bucket, {}),
            })
        return {"samples": samples, "samples_warning": self.error,
                "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
                "daily_energy": daily_energy(samples, end)}


def daily_energy(samples: list[dict], end: datetime) -> list[dict]:
    """Only total fully bracketed local days with uninterrupted, monotonic counters."""
    grouped: dict[str, list[tuple[datetime, dict]]] = {}
    for row in samples:
        stamp = datetime.fromisoformat(row["timestamp"]).astimezone()
        grouped.setdefault(stamp.date().isoformat(), []).append((stamp, row))
    result = []
    for date, rows in sorted(grouped.items()):
        first = rows[0][0].replace(hour=0, minute=0, second=0, microsecond=0)
        following = datetime.combine(first.date() + timedelta(days=1), datetime.min.time()).astimezone()
        relevant = [(datetime.fromisoformat(row["updated"]).astimezone(), row)
                    for row in samples if row.get("updated") and
                    first <= datetime.fromisoformat(row["updated"]).astimezone() <= following]
        relevant.sort(key=lambda pair: pair[0])
        values = [row.get("month_kwh") for _, row in relevant]
        complete = (
            following <= end.astimezone() and len(relevant) >= 2
            and relevant[0][0] == first and relevant[-1][0] == following
            and first.month == following.month
            and all(not row.get("error") and type(row.get("month_kwh")) in (int, float)
                    for _, row in relevant)
            and all(0 < (last[0] - previous[0]).total_seconds() <= SAMPLE_INTERVAL_SECONDS
                    for previous, last in zip(relevant, relevant[1:]))
            and all(last >= previous for previous, last in zip(values, values[1:])
                    if type(last) in (int, float) and type(previous) in (int, float))
        )
        result.append({"date": date, "kwh": values[-1] - values[0] if complete else None,
                       "status": "Counter-derived total" if complete else
                       "Incomplete: exact day-boundary readings, continuous samples and a non-reset counter required."})
    return result
