"""Permanent counter-derived yearly energy totals; raw telemetry remains disposable."""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
import json
import math
import re
import sqlite3

from tapo_plugs import normalize_mac


def _number(value: object) -> str | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return format(Decimal(str(value)), "f")


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS energy_years "
                       "(mac TEXT, year INTEGER, data TEXT NOT NULL, PRIMARY KEY(mac, year))")
    connection.execute("CREATE TABLE IF NOT EXISTS energy_cursors "
                       "(mac TEXT PRIMARY KEY, data TEXT NOT NULL)")
    # Historical tariffs were not recorded. Import retained counters as unpriced usage.
    missing = connection.execute(
        "SELECT DISTINCT mac FROM samples WHERE mac NOT IN (SELECT mac FROM energy_cursors)"
    ).fetchall()
    if missing:
        connection.execute("BEGIN IMMEDIATE")
        missing = connection.execute(
            "SELECT DISTINCT mac FROM samples WHERE mac NOT IN (SELECT mac FROM energy_cursors)"
        ).fetchall()
    for (mac,) in missing:
        for bucket, data in connection.execute(
            "SELECT bucket,data FROM samples WHERE mac=? ORDER BY bucket", (mac,)
        ).fetchall():
            record(connection, mac, bucket, json.loads(data), None)


def rate_changed(connection: sqlite3.Connection, mac: str) -> None:
    row = connection.execute("SELECT data FROM energy_cursors WHERE mac=?", (mac,)).fetchone()
    if row:
        cursor = json.loads(row[0])
        cursor["rate_changed"] = True
        connection.execute("UPDATE energy_cursors SET data=? WHERE mac=?", (json.dumps(cursor), mac))


def record(connection: sqlite3.Connection, mac: str, bucket: int, sample: dict, rate: str | None) -> None:
    row = connection.execute("SELECT data FROM energy_cursors WHERE mac=?", (mac,)).fetchone()
    cursor = json.loads(row[0]) if row else {
        "seen": -1, "baseline": None, "rate": None, "rate_changed": False,
    }
    if bucket <= cursor["seen"]:
        return
    stamp = datetime.fromtimestamp(bucket).astimezone()
    year = stamp.year
    row = connection.execute("SELECT data FROM energy_years WHERE mac=? AND year=?", (mac, year)).fetchone()
    total = json.loads(row[0]) if row else {
        "since": bucket, "last": bucket, "kwh": "0", "cost": "0", "unpriced_kwh": "0", "incomplete": False,
    }
    total["last"] = bucket
    current = {"bucket": bucket, "today": _number(sample.get("today_kwh")),
               "month": _number(sample.get("month_kwh"))}
    previous = cursor["baseline"]
    valid = not sample.get("error") and (current["today"] is not None or current["month"] is not None)
    if not valid:
        total["incomplete"] = True
    else:
        with localcontext() as context:
            context.prec = 350
            delta = None
            if previous is not None:
                before = datetime.fromtimestamp(previous["bucket"]).astimezone()
                if (before.year, before.month) == (stamp.year, stamp.month):
                    if previous["month"] is not None and current["month"] is not None:
                        difference = Decimal(current["month"]) - Decimal(previous["month"])
                        if difference >= 0:
                            delta = difference
                if delta is None and before.date() == stamp.date():
                    if previous["today"] is not None and current["today"] is not None:
                        difference = Decimal(current["today"]) - Decimal(previous["today"])
                        if difference >= 0:
                            delta = difference
                if delta is None:
                    total["incomplete"] = True
                    # Across a date/reset boundary, only the new day's counter is known.
                    if before.date() < stamp.date() and current["today"] is not None:
                        delta = Decimal(current["today"])
                    if before.year != year:
                        old = connection.execute("SELECT data FROM energy_years WHERE mac=? AND year=?",
                                                 (mac, before.year)).fetchone()
                        if old:
                            old_total = json.loads(old[0])
                            old_total["incomplete"] = True
                            connection.execute("UPDATE energy_years SET data=? WHERE mac=? AND year=?",
                                               (json.dumps(old_total), mac, before.year))
            if delta is not None:
                total["kwh"] = format(Decimal(total["kwh"]) + delta, "f")
                if rate is None or cursor["rate"] != rate or cursor["rate_changed"]:
                    total["unpriced_kwh"] = format(Decimal(total["unpriced_kwh"]) + delta, "f")
                else:
                    total["cost"] = format(Decimal(total["cost"]) + delta * Decimal(rate) / 100, "f")
        cursor.update(baseline=current, rate=rate, rate_changed=False)
    cursor["seen"] = bucket
    connection.execute("INSERT OR REPLACE INTO energy_years VALUES (?,?,?)", (mac, year, json.dumps(total)))
    connection.execute("INSERT OR REPLACE INTO energy_cursors VALUES (?,?)", (mac, json.dumps(cursor)))


def years(connection: sqlite3.Connection, mac: str) -> list[dict]:
    result = []
    for year, data in connection.execute("SELECT year,data FROM energy_years WHERE mac=? ORDER BY year DESC", (mac,)):
        total = json.loads(data)
        with localcontext() as context:
            context.prec = 350
            result.append({
                "year": year, **{key: total[key] for key in ("kwh", "unpriced_kwh", "incomplete")},
                "cost": format(Decimal(total["cost"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP), "f"),
                "since": datetime.fromtimestamp(total["since"]).astimezone().isoformat(),
                "last": datetime.fromtimestamp(total["last"]).astimezone().isoformat(),
            })
    return result


def summary(rows: list[dict]) -> str:
    if not rows:
        return "Yearly electricity: waiting for recorded counters."
    lines = []
    for row in rows:
        unpriced = Decimal(row["unpriced_kwh"]) > 0
        since = datetime.fromisoformat(row["since"]).astimezone().strftime("%d/%m/%Y %H:%M")
        last = datetime.fromisoformat(row["last"]).astimezone().strftime("%d/%m/%Y %H:%M")
        lines.append(
            f"{row['year']}: {row['kwh']} kWh — "
            + (f"${row['cost']} priced portion; {row['unpriced_kwh']} kWh unpriced"
               if unpriced else f"${row['cost']} estimated")
            + f". Tracking {since} to {last}."
            + (" Incomplete: gaps or counter resets; known usage only." if row["incomplete"] else "")
        )
    return "\n".join(lines)


def export(connection: sqlite3.Connection) -> dict:
    return {
        "years": [[mac, year, json.loads(data)] for mac, year, data in
                  connection.execute("SELECT mac,year,data FROM energy_years ORDER BY mac,year")],
        "cursors": [[mac, json.loads(data)] for mac, data in
                    connection.execute("SELECT mac,data FROM energy_cursors ORDER BY mac")],
    }


def validate(value: object) -> None:
    def fail() -> None:
        raise ValueError("Invalid yearly electricity tally.")

    def timestamp(number: object) -> bool:
        return type(number) is int and 0 <= number <= 253402214400

    def decimal(text: object) -> bool:
        return isinstance(text, str) and len(text) <= 350 and bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text))

    if not isinstance(value, dict) or set(value) != {"years", "cursors"}:
        fail()
    if not isinstance(value["years"], list) or not isinstance(value["cursors"], list):
        fail()
    identities = set()
    year_keys = set()
    latest = {}
    for row in value["years"]:
        if not isinstance(row, list) or len(row) != 3:
            fail()
        mac, year, total = row
        if (not isinstance(mac, str) or normalize_mac(mac) != mac or type(year) is not int
                or not 1970 <= year <= 9999 or (mac, year) in year_keys
                or not isinstance(total, dict)
                or set(total) != {"since", "last", "kwh", "cost", "unpriced_kwh", "incomplete"}):
            fail()
        year_keys.add((mac, year))
        identities.add(mac)
        if (not timestamp(total["since"]) or not timestamp(total["last"])
                or total["last"] < total["since"] or type(total["incomplete"]) is not bool
                or any(not decimal(total[key]) for key in ("kwh", "cost", "unpriced_kwh"))
                or Decimal(total["unpriced_kwh"]) > Decimal(total["kwh"])):
            fail()
        if any(datetime.fromtimestamp(total[key]).year != year for key in ("since", "last")):
            fail()
        latest[mac] = max(latest.get(mac, 0), total["last"])
    cursors = set()
    for row in value["cursors"]:
        if not isinstance(row, list) or len(row) != 2:
            fail()
        mac, cursor = row
        if (not isinstance(mac, str) or mac not in identities or mac in cursors
                or not isinstance(cursor, dict)
                or set(cursor) != {"seen", "baseline", "rate", "rate_changed"}):
            fail()
        cursors.add(mac)
        if (not timestamp(cursor["seen"]) or cursor["seen"] != latest[mac]
                or type(cursor["rate_changed"]) is not bool
                or (cursor["rate"] is not None and not decimal(cursor["rate"]))):
            fail()
        baseline = cursor["baseline"]
        if baseline is not None and (
            not isinstance(baseline, dict) or set(baseline) != {"bucket", "today", "month"}
            or not timestamp(baseline["bucket"]) or baseline["bucket"] > cursor["seen"]
            or all(baseline[key] is None for key in ("today", "month"))
            or any(baseline[key] is not None and not decimal(baseline[key]) for key in ("today", "month"))
        ):
            fail()
    if cursors != identities:
        fail()


def restore(connection: sqlite3.Connection, value: dict) -> None:
    validate(value)
    connection.execute("DELETE FROM energy_years")
    connection.execute("DELETE FROM energy_cursors")
    connection.executemany("INSERT INTO energy_years VALUES (?,?,?)",
                           [(mac, year, json.dumps(data)) for mac, year, data in value["years"]])
    connection.executemany("INSERT INTO energy_cursors VALUES (?,?)",
                           [(mac, json.dumps(data)) for mac, data in value["cursors"]])
