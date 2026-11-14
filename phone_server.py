"""Loopback-only phone web interface for the garden sprinkler service."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import replace
import hmac
import ipaddress
import json
import logging
import os
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from controller import MAX_MANUAL_MINUTES, decode_zones, encode_zones
from service import DEFAULT_CONFIG, default_socket, exchange
from system_backup import MAX_TRANSPORT_BYTES

DEFAULT_PASSWORD_FILE = DEFAULT_CONFIG.parent / "phone-password"
SESSION_COOKIE = "garden_session"
SESSION_SECONDS = 12 * 60 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = 5 * 60
MAX_REQUEST_BYTES = 4096
STATIC_FILES = {
    "/": ("phone_ui.html", "text/html; charset=utf-8"),
    "/app.css": ("phone_ui.css", "text/css; charset=utf-8"),
    "/app.js": ("phone_ui.js", "text/javascript; charset=utf-8"),
}


@dataclass
class Session:
    csrf_token: str
    expires_at: float


class PhoneControlServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], service_socket: Path, password: str,
    ) -> None:
        self.service_socket = service_socket
        self.expected_password = password
        self.sessions: dict[str, Session] = {}
        self.login_failures: list[float] = []
        self.sessions_lock = threading.Lock()
        super().__init__(address, PhoneRequestHandler)


class PhoneRequestHandler(BaseHTTPRequestHandler):
    server: PhoneControlServer
    server_version = "GardenSprinklerPhone/1"
    timeout = 10

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self';",
        )
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self, status: HTTPStatus, value: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json(self, limit: int = MAX_REQUEST_BYTES) -> object:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send a JSON request.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Request body length is required.")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("Invalid request body length.") from error
        if length < 0 or length > limit:
            raise ValueError("Request body is too large.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Incomplete request body.")
        try:
            return json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must contain valid JSON.") from error

    def _session(self) -> tuple[str, Session] | None:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookies.get(SESSION_COOKIE)
        if morsel is None:
            return None
        token = morsel.value
        now = time.monotonic()
        with self.server.sessions_lock:
            session = self.server.sessions.get(token)
            if session is None:
                return None
            if session.expires_at <= now:
                del self.server.sessions[token]
                return None
            session.expires_at = now + SESSION_SECONDS
            return token, session

    def _check_csrf(self, session: Session) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        return hmac.compare_digest(
            supplied.encode("utf-8"), session.csrf_token.encode("utf-8"),
        )

    @staticmethod
    def _public_status(data: dict[str, object]) -> dict[str, object]:
        try:
            zones = decode_zones(data["settings"])
            channel_count = data["channel_count"]
            master_channel = data["master_channel"]
            runs_data = data["runs"]
            rain_paused = data["rain_paused"]
            pump_on = data["pump_on"]
            fault = data["fault"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid status from the sprinkler service.") from error
        if (
            type(channel_count) is not int or channel_count not in (8, 16)
            or channel_count > len(zones)
            or (master_channel is not None and type(master_channel) is not int)
            or type(rain_paused) is not bool or type(pump_on) is not bool
            or not isinstance(fault, str) or not isinstance(runs_data, list)
        ):
            raise ValueError("Invalid status from the sprinkler service.")
        runs: dict[int, dict[str, object]] = {}
        for item in runs_data:
            if (
                not isinstance(item, dict)
                or type(item.get("channel")) is not int
                or not isinstance(item.get("source"), str)
                or (
                    item.get("remaining") is not None
                    and not isinstance(item.get("remaining"), (int, float))
                )
                or (
                    item.get("remaining") is None
                    and (item.get("source") != "Continuous" or "remaining" not in item)
                )
                or type(item.get("remaining")) is bool
                or (item.get("elapsed") is not None and (
                    type(item["elapsed"]) not in (int, float) or item["elapsed"] < 0
                ))
            ):
                raise ValueError("Invalid watering run in service status.")
            runs[item["channel"]] = {
                "source": item["source"],
                "remaining": None if item["remaining"] is None else max(0, item["remaining"]),
                "elapsed": item.get("elapsed"),
            }
        channels = []
        available = data.get("available_channels", list(range(channel_count)))
        if not isinstance(available, list) or any(type(channel) is not int or channel not in range(channel_count) for channel in available):
            raise ValueError("Invalid available channel list in service status.")
        tapo = data.get("tapo")
        if tapo is not None and (
            not isinstance(tapo, list) or any(
                not isinstance(item, dict) or type(item.get("channel")) is not int
                or type(item.get("assigned")) is not bool for item in tapo
            )
        ):
            raise ValueError("Invalid Tapo channel status.")
        next_starts = data.get("next_starts")
        if next_starts is not None and (
            not isinstance(next_starts, list) or len(next_starts) != channel_count
            or any(value is not None and not isinstance(value, str) for value in next_starts)
        ):
            raise ValueError("Invalid next schedule in service status.")
        for channel, zone in enumerate(zones[:channel_count]):
            channels.append({
                "channel": channel,
                "name": zone.name,
                "available": channel in available,
                "tapo": next((item for item in tapo if item["channel"] == channel), None) if tapo is not None else None,
                "enabled": zone.enabled,
                "paused_windows": list(zone.paused_windows),
                "rain_pause_exempt": zone.rain_pause_exempt,
                "pump_exempt": zone.pump_exempt,
                "max_manual_minutes": zone.max_manual_minutes,
                "continuous_reminder_minutes": zone.continuous_reminder_minutes,
                "master": channel == master_channel,
                "running": runs.get(channel),
                "pump_on": pump_on if channel == master_channel else False,
                "fault": data.get("garden_fault", "") if channel < 8
                else data.get("channel_faults", {}).get(str(channel), ""),
                "next_start": next_starts[channel] if next_starts is not None else None,
            })
        return {
            "channels": channels,
            "rain_paused": rain_paused,
            "fault": fault,
            "garden_fault": data.get("garden_fault", ""),
            "channel_faults": data.get("channel_faults", {}),
            "group_controls_supported": data.get("group_controls_supported") is True,
            "continuous_supported": data.get("continuous_supported") is True,
            "manual_timers_supported": data.get("manual_timers_supported") is True,
            "next_schedule_supported": next_starts is not None,
            "history_supported": data.get("history_supported") is True,
            "history_filters_supported": data.get("history_filters_supported") is True,
            "continuous_reminders_supported": data.get("continuous_reminders_supported") is True,
            "revision": data.get("revision"),
            "backup_supported": data.get("full_backup_supported") is True,
            "operation_held": data.get("operation_held") is True,
            "restore_restart_required": data.get("restore_restart_required") is True,
            "history_warning": data.get("history_warning", ""),
        }

    def _get_service_status(self) -> dict[str, object]:
        return self._public_status(exchange(
            self.server.service_socket, {"command": "status"},
        ))

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        static_file = STATIC_FILES.get(path)
        if static_file:
            file_name, content_type = static_file
            try:
                body = Path(__file__).with_name(file_name).read_bytes()
            except OSError as error:
                logging.error("Cannot read phone interface file %s: %s", file_name, error)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Phone interface file is unavailable.")
                return
            self._send(HTTPStatus.OK, body, content_type)
            return
        if path not in ("/api/session", "/api/status", "/api/history", "/api/backup", "/api/tapo-details", "/api/tapo-timeline"):
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        session_info = self._session()
        if session_info is None:
            self._error(HTTPStatus.UNAUTHORIZED, "Sign in to control the sprinkler.")
            return
        if path == "/api/session":
            _, session = session_info
            self._send_json(HTTPStatus.OK, {"csrf": session.csrf_token})
            return
        try:
            if path == "/api/tapo-timeline":
                query = parse_qs(url.query, keep_blank_values=True)
                values = query.get("channel", [])
                if set(query) != {"channel"} or len(values) != 1 or values[0] not in tuple(str(i) for i in range(8, 16)):
                    raise ValueError("Choose an assigned Tapo channel.")
                result = exchange(self.server.service_socket, {
                    "command": "tapo_timeline", "channel": int(values[0]),
                })
                self._send_json(HTTPStatus.OK, {"timeline": result})
                return
            if path == "/api/tapo-details":
                query = parse_qs(url.query, keep_blank_values=True)
                if query not in ({}, {"refresh": ["1"]}):
                    raise ValueError("Invalid Tapo details request.")
                result = exchange(self.server.service_socket, {
                    "command": "tapo_details", "refresh": bool(query),
                })
                self._send_json(HTTPStatus.OK, {"details": result})
                return
            if path == "/api/backup":
                result = exchange(self.server.service_socket, {"command": "status"})
                decode_zones(result["settings"])
                if not isinstance(result.get("revision"), str):
                    raise ValueError("Invalid service revision.")
                self._send_json(HTTPStatus.OK, {"revision": result["revision"]})
                return
            if path == "/api/history":
                query = parse_qs(url.query, keep_blank_values=True)
                values = query.get("offset", ["0"])
                filters = query.get("filter", ["all"])
                if set(query) - {"offset", "filter"} or len(values) != 1 or not values[0].isascii() or not values[0].isdecimal() or len(values[0]) > 4 or len(filters) != 1:
                    raise ValueError("Invalid history page.")
                category = filters[0]
                if category not in ("all", "system", "faults") and (
                    not category.isascii() or not category.isdecimal()
                    or len(category) > 2 or int(category) not in range(16)
                ):
                    raise ValueError("Invalid history filter.")
                command = {"command": "history", "offset": int(values[0])}
                if category != "all":
                    command["filter"] = category
                result = exchange(self.server.service_socket, command)
                self._send_json(HTTPStatus.OK, {"history": result})
                return
            self._send_json(HTTPStatus.OK, {"status": self._get_service_status()})
        except (OSError, ValueError, KeyError) as error:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/api/login", "/api/logout", "/api/command", "/api/restore", "/api/reminder", "/api/window-pause",
                        "/api/backup", "/api/backup-preview"):
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        try:
            body = self._read_json(
                MAX_TRANSPORT_BYTES if path in ("/api/restore", "/api/backup-preview")
                else 16384 if path == "/api/backup" else MAX_REQUEST_BYTES,
            )
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if not isinstance(body, dict):
            self._error(HTTPStatus.BAD_REQUEST, "Request body must be an object.")
            return
        if path == "/api/login":
            self._login(body)
            return
        session_info = self._session()
        if session_info is None:
            self._error(HTTPStatus.UNAUTHORIZED, "Sign in to control the sprinkler.")
            return
        token, session = session_info
        if not self._check_csrf(session):
            self._error(HTTPStatus.FORBIDDEN, "Request verification failed; reload the page.")
            return
        if path == "/api/logout":
            with self.server.sessions_lock:
                self.server.sessions.pop(token, None)
            self._send_json(
                HTTPStatus.OK, {"ok": True},
                {"Set-Cookie": (
                    f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
                )},
            )
            return
        if path in ("/api/restore", "/api/backup", "/api/backup-preview"):
            try:
                password = body.get("password")
                if not isinstance(password, str) or not password or len(password) > 1024:
                    raise ValueError("Enter the backup password.")
                command = {
                    "command": "backup_restore" if path == "/api/restore" else
                    "backup_preview" if path == "/api/backup-preview" else "backup_export",
                    "password": password,
                }
                if path != "/api/backup":
                    command["archive"] = body.get("archive")
                if path == "/api/restore":
                    revision = body.get("revision")
                    if not isinstance(revision, str) or not revision:
                        raise ValueError("A preview revision is required.")
                    command["revision"] = revision
                result = exchange(self.server.service_socket, command)
                if result.get("fault"):
                    raise RuntimeError("Controller fault: " + str(result["fault"]))
                if path == "/api/restore":
                    self._send_json(HTTPStatus.OK, {
                        "status": self._public_status(result), "recovery_backup": result.get("recovery_backup", ""),
                    })
                else:
                    self._send_json(HTTPStatus.OK, result)
            except (ValueError, KeyError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except (OSError, RuntimeError) as error:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
            return
        if path in ("/api/reminder", "/api/window-pause"):
            try:
                channel, minutes, revision = body.get("channel"), body.get("minutes"), body.get("revision")
                if type(channel) is not int or channel not in range(16):
                    raise ValueError("Choose a configured channel.")
                group, paused = body.get("group"), body.get("paused")
                if path == "/api/window-pause" and (
                    type(group) is not int or group not in range(4) or type(paused) is not bool
                ):
                    raise ValueError("Choose a schedule group from 0 to 3 and a true or false pause state.")
                if path == "/api/reminder" and minutes is not None and (type(minutes) is not int or not 1 <= minutes <= MAX_MANUAL_MINUTES):
                    raise ValueError("Reminder must be blank or 1 to 1440 minutes.")
                if not isinstance(revision, str) or not revision:
                    raise ValueError("A settings revision is required.")
                current = exchange(self.server.service_socket, {"command": "status"})
                if path == "/api/reminder" and current.get("continuous_reminders_supported") is not True:
                    raise ValueError("Restart the updated controller service to configure reminders.")
                if current.get("revision") != revision:
                    raise ValueError("Settings changed elsewhere; refresh before saving.")
                zones = decode_zones(current["settings"])
                if channel >= current["channel_count"] or channel == current["master_channel"]:
                    raise ValueError("Choose a configured non-pump channel.")
                if path == "/api/window-pause":
                    windows = list(zones[channel].paused_windows)
                    windows[group] = paused
                    zones[channel] = replace(zones[channel], paused_windows=tuple(windows))
                else:
                    zones[channel] = replace(zones[channel], continuous_reminder_minutes=minutes)
                result = exchange(self.server.service_socket, {
                    "command": "save", "settings": encode_zones(zones), "revision": revision,
                })
                if result.get("fault"):
                    raise RuntimeError("Controller fault: " + str(result["fault"]))
                self._send_json(HTTPStatus.OK, {"status": self._public_status(result)})
            except (ValueError, KeyError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except (OSError, RuntimeError) as error:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
            return
        self._command(body)

    def _login(self, body: dict[str, object]) -> None:
        password = body.get("password")
        with self.server.sessions_lock:
            now = time.monotonic()
            self.server.login_failures = [
                timestamp for timestamp in self.server.login_failures
                if timestamp > now - LOGIN_FAILURE_WINDOW
            ]
            limited = len(self.server.login_failures) >= LOGIN_FAILURE_LIMIT
            correct = not limited and isinstance(password, str) and hmac.compare_digest(
                password.encode("utf-8"), self.server.expected_password.encode("utf-8"),
            )
            if not limited and not correct:
                self.server.login_failures.append(now)
        if limited:
            self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many incorrect sign-in attempts. Wait five minutes and try again.",
            )
            return
        if not correct:
            self._error(HTTPStatus.UNAUTHORIZED, "Incorrect password.")
            return
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self.server.sessions_lock:
            now = time.monotonic()
            expired = [
                key for key, value in self.server.sessions.items()
                if value.expires_at <= now
            ]
            for key in expired:
                del self.server.sessions[key]
            full = len(self.server.sessions) >= 64
            if not full:
                self.server.sessions[token] = Session(
                    csrf_token, now + SESSION_SECONDS,
                )
        if full:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Too many active phone sessions.")
            return
        self._send_json(
            HTTPStatus.OK,
            {"csrf": csrf_token},
            {"Set-Cookie": (
                f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; "
                "Secure; HttpOnly; SameSite=Strict"
            )},
        )

    def _command(self, body: dict[str, object]) -> None:
        action = body.get("action")
        if action in ("stop_all", "stop_garden", "stop_appliances", "resume_operation"):
            command: dict[str, object] = {"command": action}
        elif action == "rain_pause":
            paused = body.get("paused")
            if type(paused) is not bool:
                self._error(HTTPStatus.BAD_REQUEST, "Rain Pause must be true or false.")
                return
            command = {"command": "rain_pause", "paused": paused}
        elif action in ("run", "continuous", "stop", "retry_station"):
            channel = body.get("channel")
            if type(channel) is not int or channel < 0 or channel >= 16:
                self._error(HTTPStatus.BAD_REQUEST, "Choose a configured channel.")
                return
            command = {
                "command": "manual" if action == "run" else action,
                "channel": channel,
            }
            if action == "run" and "duration_minutes" in body:
                duration = body["duration_minutes"]
                if type(duration) is not int or not 1 <= duration <= MAX_MANUAL_MINUTES:
                    self._error(HTTPStatus.BAD_REQUEST, "Manual duration must be 1 to 1440 minutes.")
                    return
                command["duration_minutes"] = duration
        else:
            self._error(HTTPStatus.BAD_REQUEST, "Unknown sprinkler action.")
            return
        try:
            if action == "run" and "duration_minutes" in body:
                status = exchange(self.server.service_socket, {"command": "status"})
                if status.get("manual_timers_supported") is not True:
                    raise ValueError("Restart the updated background service before using manual timers.")
            result = exchange(self.server.service_socket, command)
            fault = result.get("fault")
            if isinstance(fault, str) and fault:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Controller fault: " + fault)
                return
            public_status = self._public_status(result)
        except ValueError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
            return
        except OSError as error:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
            return
        self._send_json(HTTPStatus.OK, {"status": public_status})


def load_password(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"Phone password file must not be a symbolic link: {path}")
    try:
        details = path.stat()
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"Phone password path is not a regular file: {path}")
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError(f"Phone password file must be owned by this user and mode 0600: {path}")
        password = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"Cannot read phone password file {path}: {error}") from error
    if len(password) < 4:
        raise ValueError("Phone password or PIN must contain at least 4 characters.")
    return password


def create_server(
    host: str, port: int, service_socket: Path, password: str,
) -> PhoneControlServer:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Phone server bind address must be the IPv4 loopback address 127.0.0.1.") from error
    if address != ipaddress.IPv4Address("127.0.0.1"):
        raise ValueError("Phone server must bind to 127.0.0.1, not a network interface.")
    return PhoneControlServer((str(address), port), service_socket, password)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--socket", type=Path, default=default_socket())
    parser.add_argument("--password-file", type=Path, default=DEFAULT_PASSWORD_FILE)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Phone server port must be between 1 and 65535.")
    try:
        password = load_password(args.password_file.expanduser())
        server = create_server(args.bind, args.port, args.socket.expanduser(), password)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Phone interface listening on loopback %s:%d", args.bind, args.port)
    with server:
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
