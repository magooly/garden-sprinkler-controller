"""On-demand read-only plug telemetry, independent of the switching loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
import re
from decimal import Decimal, ROUND_HALF_UP, localcontext
import threading
import time
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import energy_ledger

from kasa import Credentials, Discover, KasaException, Module
from kasa.smart.smartdevice import SmartDevice

from tapo_plugs import Plug, normalize_mac

SAMPLE_INTERVAL_SECONDS = 300
OFF_PEAK_SAMPLE_INTERVAL_SECONDS = 60
RETENTION_SECONDS = 8 * 86400
HISTORY_HOURS = (6, 24, 168)


def parse_electricity_rate(value: object) -> str | None:
    if not isinstance(value, str):
        raise ValueError("Enter cents per kWh as text, or leave blank to hide costs.")
    text = value.strip()
    if not text:
        return None
    if len(text) > 16 or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]{1,4})?|\.[0-9]{1,4})", text):
        raise ValueError("Use a non-negative cents/kWh number with up to four decimal places (maximum 16 characters).")
    return format(Decimal(text).normalize(), "f")


def dollars_to_rate(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if len(text) > 14 or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]{1,6})?|\.[0-9]{1,6})", text):
        raise ValueError("Enter dollars/kWh, e.g. 0.26268, with up to six decimal places (maximum 14 characters).")
    return parse_electricity_rate(format((Decimal(text) * 100).normalize(), "f"))


def rate_to_dollars(rate: str | None) -> str:
    return "" if rate is None else format((Decimal(rate) / 100).normalize(), "f")


def estimated_costs(sample: dict | None, rate: str | None) -> dict[str, str | None]:
    costs: dict[str, str | None] = {"today_kwh": None, "month_kwh": None}
    if rate is None or sample is None or sample.get("error"):
        return costs
    with localcontext() as context:
        context.prec = 350
        for key in costs:
            value = sample.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                costs[key] = format((Decimal(str(value)) * Decimal(rate) / 100)
                                    .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    return costs


def validate_electricity_rates(rates: object) -> None:
    if not isinstance(rates, dict):
        raise ValueError("Invalid saved electricity rates.")
    for mac, rate in rates.items():
        if (not isinstance(mac, str) or normalize_mac(mac) != mac or not isinstance(rate, str)
                or parse_electricity_rate(rate) != rate):
            raise ValueError("Invalid saved electricity rate or plug identity.")


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
        self.last_sample_buckets: dict[int, int] = {}
        self.failures: dict[int, tuple[str, int]] = {}

    def sample_due(self) -> None:
        now = int(time.time())
        with self.lock:
            if self.store is None or self.reading:
                return
            due = []
            for index, plug in enumerate(self.plugs):
                if plug is None:
                    continue
                interval = OFF_PEAK_SAMPLE_INTERVAL_SECONDS if plug.off_peak else SAMPLE_INTERVAL_SECONDS
                bucket = now // interval * interval
                if self.last_sample_buckets.get(index) != bucket:
                    due.append(index)
            if not due:
                return
            for index in due:
                plug = self.plugs[index]
                interval = OFF_PEAK_SAMPLE_INTERVAL_SECONDS if plug and plug.off_peak else SAMPLE_INTERVAL_SECONDS
                self.last_sample_buckets[index] = now // interval * interval
            self.reading = True
            self.last_started = time.monotonic()
            threading.Thread(target=self._sample, args=(now, tuple(due)), daemon=True).start()

    def _sample(self, bucket: int, indices: tuple[int, ...]) -> None:
        try:
            asyncio.run(self._read(bucket, indices))
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

    async def _read(self, bucket: int | None = None, indices: tuple[int, ...] | None = None) -> None:
        for index, plug in enumerate(self.plugs):
            if plug is None or (indices is not None and index not in indices):
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
                interval = OFF_PEAK_SAMPLE_INTERVAL_SECONDS if plug.off_peak else SAMPLE_INTERVAL_SECONDS
                self.store.save(plug.mac, bucket // interval * interval,
                                {**row, "sample_interval_seconds": interval})


class TapoSamples:
    """Keep eight days of samples at each plug's cadence, separated by identity."""

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
            connection.execute("CREATE TABLE IF NOT EXISTS electricity_rates "
                               "(mac TEXT PRIMARY KEY, cents_per_kwh TEXT NOT NULL)")
            with connection:
                energy_ledger.initialize(connection)
        except (sqlite3.Error, ValueError):
            connection.close()
            raise
        return connection

    def set_rate(self, mac: str, value: object, expected: object) -> str | None:
        rate = parse_electricity_rate(value)
        if expected is not None and (not isinstance(expected, str)
                                     or parse_electricity_rate(expected) != expected):
            raise ValueError("Refresh the graph before saving its electricity rate.")
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute("SELECT cents_per_kwh FROM electricity_rates WHERE mac=?", (mac,)).fetchone()
                    current = row[0] if row else None
                    if current != expected:
                        raise ValueError("The electricity rate changed in another window. Refresh history and try again.")
                    if rate != current:
                        energy_ledger.rate_changed(connection, mac)
                    if rate is None:
                        connection.execute("DELETE FROM electricity_rates WHERE mac=?", (mac,))
                    else:
                        connection.execute("INSERT OR REPLACE INTO electricity_rates VALUES (?,?)", (mac, rate))
        except (OSError, sqlite3.Error) as error:
            logging.error("Electricity rate could not be saved: %s", error)
            raise OSError("Electricity rate could not be saved. No rate change was applied.") from error
        return rate

    def rates(self) -> dict[str, str]:
        try:
            with closing(self._connect()) as connection:
                rates = dict(connection.execute("SELECT mac,cents_per_kwh FROM electricity_rates"))
            validate_electricity_rates(rates)
            return rates
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.error("Electricity rates could not be read: %s", error)
            raise OSError("Saved electricity rates are unavailable.") from error

    def restore_rates(self, rates: dict[str, str]) -> None:
        validate_electricity_rates(rates)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("DELETE FROM electricity_rates")
                    connection.executemany("INSERT INTO electricity_rates VALUES (?,?)", rates.items())
        except (OSError, sqlite3.Error) as error:
            logging.error("Electricity rates could not be restored: %s", error)
            raise OSError("Electricity rates could not be restored.") from error

    def yearly_backup(self) -> dict:
        try:
            with closing(self._connect()) as connection:
                value = energy_ledger.export(connection)
            energy_ledger.validate(value)
            return value
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.error("Yearly electricity tally could not be read: %s", error)
            raise OSError("Yearly electricity tally is unavailable.") from error

    def restore_electricity(self, rates: dict[str, str], yearly: dict) -> None:
        validate_electricity_rates(rates)
        energy_ledger.validate(yearly)
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM electricity_rates")
                    connection.executemany("INSERT INTO electricity_rates VALUES (?,?)", rates.items())
                    energy_ledger.restore(connection, yearly)
                    connection.execute("DELETE FROM samples")
        except (OSError, sqlite3.Error) as error:
            logging.error("Electricity tally and rates could not be restored: %s", error)
            raise OSError("Electricity tally and rates could not be restored.") from error

    def save(self, mac: str, bucket: int, row: dict[str, object]) -> None:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    rate = connection.execute(
                        "SELECT cents_per_kwh FROM electricity_rates WHERE mac=?", (mac,)
                    ).fetchone()
                    energy_ledger.record(connection, mac, bucket, row, rate[0] if rate else None)
                    connection.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?)",
                                       (mac, bucket, json.dumps(row, allow_nan=False)))
                    connection.execute("DELETE FROM samples WHERE bucket < ?", (bucket - RETENTION_SECONDS,))
            self.error = ""
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.error = "Some telemetry samples could not be saved."
            logging.error("Tapo samples could not be saved: %s", exc)

    def history(self, mac: str, end: datetime, hours: int = 24,
                interval: int = SAMPLE_INTERVAL_SECONDS) -> dict[str, object]:
        if type(hours) is not int or hours not in HISTORY_HOURS:
            raise ValueError("Choose 6, 24 or 168 hours of history.")
        if type(interval) is not int or interval not in (60, 300):
            raise ValueError("Choose a supported telemetry interval.")
        end_seconds = int(end.timestamp())
        start_seconds = end_seconds - hours * 3600
        try:
            with closing(self._connect()) as connection:
                records = connection.execute(
                    "SELECT bucket,data FROM samples WHERE mac=? AND bucket>? AND bucket<=? ORDER BY bucket",
                    (mac, start_seconds, end_seconds),
                ).fetchall()
                rate_row = connection.execute("SELECT cents_per_kwh FROM electricity_rates WHERE mac=?", (mac,)).fetchone()
                rate = rate_row[0] if rate_row else None
                if rate is not None and parse_electricity_rate(rate) != rate:
                    raise ValueError("Invalid stored electricity rate.")
                yearly = energy_ledger.years(connection, mac)
            rows = {bucket: json.loads(data) for bucket, data in records}
        except (OSError, sqlite3.Error, ValueError) as exc:
            logging.error("Tapo samples could not be read: %s", exc)
            raise OSError("Recorded Tapo readings are unavailable.") from None
        samples = []
        first = start_seconds // interval * interval + interval
        covered_until = first
        for bucket in range(first, end_seconds + 1, interval):
            if bucket not in rows and bucket < covered_until:
                continue
            row = rows.get(bucket, {})
            cadence = row.get("sample_interval_seconds", SAMPLE_INTERVAL_SECONDS) if row else interval
            samples.append({
                "timestamp": datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
                "state": None, "power_w": None, "today_kwh": None, "month_kwh": None,
                "voltage_v": None, "current_a": None, "updated": None,
                "error": "No recorded reading.", **row, "sample_interval_seconds": cadence,
            })
            covered_until = bucket + cadence
        return {"samples": samples, "samples_warning": self.error,
                "electricity_rate_supported": True, "rate_cents_per_kwh": rate,
                "estimated_costs": estimated_costs(samples[-1] if samples else None, rate),
                "yearly_energy": yearly,
                "sample_interval_seconds": interval,
                "voltage_periods": voltage_periods(samples, end - timedelta(hours=hours), end),
                "daily_energy": daily_energy(samples, end)}


def voltage_periods(samples: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Bound each observation by its cadence; never bridge an unrecorded gap."""
    periods: list[dict] = []

    def append(first: datetime, last: datetime, state: str) -> None:
        if last <= first:
            return
        if periods and periods[-1]["state"] == state and periods[-1]["end"] == first.isoformat():
            periods[-1]["end"] = last.isoformat()
            periods[-1]["seconds"] += (last - first).total_seconds()
        else:
            periods.append({"start": first.isoformat(), "end": last.isoformat(),
                            "state": state, "seconds": (last - first).total_seconds()})

    cursor = start
    ordered = sorted(samples, key=lambda row: row["timestamp"])
    for index, row in enumerate(ordered):
        stamp = datetime.fromisoformat(row["timestamp"])
        if stamp >= end:
            break
        until = min(end, stamp + timedelta(seconds=row.get("sample_interval_seconds", SAMPLE_INTERVAL_SECONDS)))
        if index + 1 < len(ordered):
            until = min(until, datetime.fromisoformat(ordered[index + 1]["timestamp"]))
        if until <= cursor:
            continue
        append(cursor, min(end, max(cursor, stamp)), "Unknown")
        value = row.get("voltage_v")
        state = ("Unknown" if row.get("error") or type(value) not in (int, float)
                 or not math.isfinite(value) or value < 0 else "Zero" if value == 0 else "Voltage")
        append(max(cursor, stamp), until, state)
        cursor = until
    append(cursor, end, "Unknown")
    return periods


def voltage_interruptions(periods: list[dict]) -> list[dict]:
    """Group consecutive zero/unknown periods until voltage is observed again."""
    interruptions: list[dict] = []
    current: dict | None = None
    for index, period in enumerate(periods):
        if period["state"] == "Voltage":
            if current is not None:
                current["return"] = period["start"]
                interruptions.append(current)
                current = None
        else:
            if current is None:
                current = {"start": period["start"], "return": None,
                           "at_range_start": index == 0, "zero": False, "unknown": False}
            current["zero"] |= period["state"] == "Zero"
            current["unknown"] |= period["state"] == "Unknown"
    if current is not None:
        interruptions.append(current)
    return interruptions


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
            and all(0 < (last[0] - previous[0]).total_seconds()
                    <= previous[1].get("sample_interval_seconds", SAMPLE_INTERVAL_SECONDS)
                    for previous, last in zip(relevant, relevant[1:]))
            and all(last >= previous for previous, last in zip(values, values[1:])
                    if type(last) in (int, float) and type(previous) in (int, float))
        )
        result.append({"date": date, "kwh": values[-1] - values[0] if complete else None,
                       "status": "Counter-derived total" if complete else
                       "Incomplete: exact day-boundary readings, continuous samples and a non-reset counter required."})
    return result
