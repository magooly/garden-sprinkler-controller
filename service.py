"""Headless relay owner and local, same-user control socket."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
from dataclasses import replace
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import socket
import socketserver
import threading
import uuid

from controller import (
    Controller, decode_zones, default_zones, encode_zones, load_rain_pause, load_zones,
    next_scheduled_start, save_rain_pause, save_zones,
)
from activity_history import ActivityHistory
from waveshare_relay import DEFAULT_HOST, DEFAULT_PORT, RelayBoards, WaveshareRelays
from timetable_profiles import TimetableProfiles
from tapo_plugs import TapoRelays, mapping_path
from tapo_details import TapoDetails

import sqlite3
import system_backup

MAX_MESSAGE = system_backup.MAX_TRANSPORT_BYTES
DEFAULT_CONFIG = Path.home() / ".config/garden-sprinkler-controller/settings.json"


def default_socket() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "garden-sprinkler-controller/control.sock"


def error_details(error: Exception) -> str:
    if isinstance(error, ExceptionGroup):
        return "; ".join(error_details(item) for item in error.exceptions)
    return str(error)


class Service:
    def __init__(
        self, config: Path, relay: WaveshareRelays, secondary: WaveshareRelays | None = None,
    ) -> None:
        self.config = config
        restore_held = system_backup.held(config)
        self.restore_restart_required = False
        self.recovery_backup = ""
        self.history = ActivityHistory(config)
        if secondary is not None and mapping_path(config).exists():
            raise ValueError("Choose either Tapo plugs or a second Waveshare board for channels 9-16.")
        self.relay = TapoRelays(relay, config) if mapping_path(config).exists() else RelayBoards(relay, secondary)
        self.tapo_details = TapoDetails(
            self.relay.plugs if isinstance(self.relay, TapoRelays) else (),
            self.relay.credentials if isinstance(self.relay, TapoRelays) else None,
            config,
        )
        zones = load_zones(config)
        if self.relay.channel_count == 16 and len(zones) == 8:
            zones.extend(default_zones(16)[8:])
            if isinstance(self.relay, TapoRelays):
                for channel in range(8, 13):
                    zones[channel] = replace(zones[channel], name=f"Tapo Plug {channel - 7}")
        self.timetables = TimetableProfiles(config)
        self.controller = Controller(
            zones, self.relay, rain_paused=load_rain_pause(config),
            channel_count=self.relay.channel_count,
            activity=self.history.record,
            available_channels=self.relay.available_channels if isinstance(self.relay, TapoRelays) else None,
        )
        self.revision = uuid.uuid4().hex
        self.controller.operation_held = restore_held
        self.fault = ""
        if isinstance(self.relay, TapoRelays) and self.relay.hot_water is not None:
            self.relay.hot_water.activity = self.history.record
        self.history.record(None, "Controller", "STARTED",
                            "Startup; OFF commands attempted. Check group/station faults; "
                            "off-peak OFF requests are confirmed asynchronously."
                            if self.controller.split_groups else "Startup; outputs initialized OFF")
        if isinstance(self.relay, TapoRelays) and self.relay.hot_water is not None:
            self.history.record(15, "Hot Water System", "REQUESTED OFF", "Startup request; not yet confirmed")

    def fail(self, error: Exception) -> None:
        self.fault = error_details(error)
        logging.error("Scheduling stopped: %s", self.fault)
        self.controller.closed = True
        try:
            self.controller.stop_all(datetime.now(), reason="Controller fault")
        except ExceptionGroup as shutdown:
            self.fault += "; OFF failures: " + error_details(shutdown)
            logging.error("Shutdown failed: %s", error_details(shutdown))
        self.history.record(None, "Controller", "FAULT", self.fault)

    def tick(self) -> None:
        try:
            self.controller.tick(datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as error:
            self.fail(error)

    def status(self) -> dict[str, object]:
        controller = self.controller
        now = datetime.now()
        return {
            "protocol": 2,
            "full_backup_supported": True,
            "operation_held": controller.operation_held,
            "restore_restart_required": self.restore_restart_required,
            "recovery_backup": self.recovery_backup,
            "continuous_supported": True,
            "manual_timers_supported": True,
            "history_supported": True,
            "history_filters_supported": True,
            "continuous_reminders_supported": True,
            "history_warning": self.history.error,
            "next_starts": [
                (start.isoformat(timespec="minutes") if start else None)
                for channel, zone in enumerate(controller.zones[:controller.channel_count])
                for start in [None if channel == controller.master_channel or channel not in controller.available_channels
                              else next_scheduled_start(zone, now)]
            ],
            "settings": encode_zones(controller.zones),
            "revision": self.revision,
            "rain_paused": controller.rain_paused,
            "master_channel": controller.master_channel,
            "pump_on": controller.pump_on,
            "fault": self.fault,
            "group_controls_supported": True,
            "garden_fault": controller.garden_fault,
            "channel_faults": {str(channel): error for channel, error in controller.channel_faults.items()},
            "host": self.relay.host,
            "port": self.relay.port,
            "slave_id": self.relay.slave_id,
            "channel_count": controller.channel_count,
            "available_channels": sorted(controller.available_channels),
            "tapo": self.relay.status() if isinstance(self.relay, TapoRelays) else None,
            "timetables": self.timetables.status(controller.zones),
            "timetable_groups": {
                str(bank): self.timetables.status(controller.zones, bank)
                for bank in range(0, controller.channel_count, 8)
            },
            "boards": [
                {"host": device.host, "port": device.port, "slave_id": device.slave_id}
                for device in self.relay.devices
            ],
            "runs": [
                {"channel": channel, "source": run.source,
                 "remaining": None if run.deadline is None
                 else max(0, run.deadline - controller.clock()),
                 "elapsed": None if run.started is None else max(0, controller.clock() - run.started)}
                for channel, run in controller.runs.items()
            ],
        }

    def dispatch(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or not isinstance(request.get("command"), str):
            raise ValueError("Expected an object with a command.")
        command = request["command"]
        if command == "status":
            return self.status()
        if command == "tapo_details":
            refresh = request.get("refresh", False)
            if type(refresh) is not bool:
                raise ValueError("Refresh must be a boolean.")
            details = self.tapo_details.snapshot(
                [zone.name for zone in self.controller.zones], refresh,
            )
            if isinstance(self.relay, TapoRelays) and self.relay.hot_water is not None:
                for row in details["rows"]:
                    if row["channel"] == 15:
                        control = self.relay.hot_water.status()
                        error = "; ".join(str(value) for value in (row["error"], control["error"]) if value)
                        row.update(control)
                        row["error"] = error
            for row in details["rows"]:
                row["station_fault"] = self.controller.channel_faults.get(row["channel"], "")
            return details
        if command == "tapo_timeline":
            channel = request.get("channel")
            if type(channel) is not int or channel not in range(8, 16):
                raise ValueError("Choose an assigned Tapo channel.")
            if not isinstance(self.relay, TapoRelays):
                raise ValueError("This Tapo channel is unassigned.")
            plug = self.relay.plugs[channel - 8]
            if plug is None:
                raise ValueError("This Tapo channel is unassigned.")
            timeline = self.history.timeline(channel)
            if self.tapo_details.store is None:
                raise OSError("Tapo sample storage is unavailable.")
            return {
                **timeline, "name": self.controller.zones[channel].name,
                "off_peak": plug.off_peak,
                **self.tapo_details.store.history(
                    plug.mac, datetime.fromisoformat(timeline["end"]),
                ),
            }
        if command == "history":
            return self.history.page(request.get("offset", 0), request.get("filter", "all"))
        if command in ("stop_garden", "stop_appliances"):
            self.controller.stop_group(0 if command == "stop_garden" else 8, datetime.now())
            return self.status()
        if command == "stop_all":
            try:
                self.controller.stop_all(datetime.now())
            except ExceptionGroup as error:
                self.fail(error)
            self.history.record(None, "Controller", "STOP ALL", "Requested")
            return self.status()
        if self.fault:
            raise ValueError("Service fault: " + self.fault + ". Check hardware and restart the service.")
        now = datetime.now()
        try:
            if command in ("backup_export", "backup_preview", "backup_restore"):
                if self.restore_restart_required and command != "backup_preview":
                    raise ValueError("Restart the controller service after restore before exporting or restoring another backup.")
                if command == "backup_export":
                    payload = system_backup.capture(self.config, self.relay, self.history, self.controller.zones)
                    return {"archive": system_backup.encrypt(payload, request.get("password")),
                            "summary": system_backup.summary(payload), "revision": self.revision}
                payload = system_backup.decrypt(request.get("archive"), request.get("password"))
                if command == "backup_preview":
                    return {"summary": system_backup.summary(payload), "revision": self.revision}
                if request.get("revision") != self.revision:
                    raise ValueError("Settings changed. Preview the backup again before restoring.")
                recovery = system_backup.encrypt(
                    system_backup.capture(self.config, self.relay, self.history, self.controller.zones), request.get("password"),
                )
                path = system_backup.sidecar(self.config, ".before-restore-" + uuid.uuid4().hex + ".sprinkler-backup")
                system_backup.write_private(path, json.dumps(recovery).encode("utf-8"))
                system_backup.set_hold(self.config, True)
                self.controller.operation_held = True
                self.restore_restart_required = True
                self.revision = uuid.uuid4().hex
                self.controller.stop_all(now, suppress=False, reason="System restore")
                system_backup.install(self.config, payload, self.history)
                self.recovery_backup = str(path)
                self.history.record(None, "Controller", "RESTORED", "Safety hold; explicit resume required after restart")
                return self.status()
            elif command == "resume_operation":
                if self.restore_restart_required:
                    raise ValueError("Restart the controller service and reopen the desktop before resuming.")
                system_backup.set_hold(self.config, False)
                self.controller.operation_held = False
                self.history.record(None, "Controller", "RESUMED", "User confirmed restored devices and settings")
                self.controller.tick(now)
            elif self.controller.operation_held and (
                self.restore_restart_required
                or command not in ("save", "profile_save", "profile_load", "rain_pause", "read_states")
            ):
                raise ValueError("Operation is held after restore. Restart if required, review devices, then Resume operation.")
            elif command in ("save", "profile_save", "profile_load"):
                if request.get("revision") != self.revision:
                    raise ValueError(
                        "Settings changed in another app or the service restarted. "
                        "Close and reopen this window before saving; your edits have not been applied."
                    )
                if command == "profile_save":
                    self.timetables.save(request.get("name"), self.controller.zones, request.get("bank", 0))
                    self.revision = uuid.uuid4().hex
                    return self.status()
                zones = (
                    self.timetables.load(request.get("name"), self.controller.zones, request.get("bank", 0))
                    if command == "profile_load" else decode_zones(request.get("settings"))
                )
                if len(zones) != len(self.controller.zones):
                    raise ValueError("Zone count changed; reopen the desktop before editing.")
                save_zones(self.config, zones)
                self.revision = uuid.uuid4().hex
                self.controller.apply(zones, now)
            elif command == "rain_pause":
                paused = request.get("paused")
                if type(paused) is not bool:
                    raise ValueError("Rain pause must be true or false.")
                if paused:
                    self.controller.rain_paused = True
                save_rain_pause(self.config, paused)
                self.controller.set_rain_pause(paused, now)
                self.history.record(None, "Controller", "RAIN PAUSE", "Enabled" if paused else "Disabled")
            elif command in ("manual", "continuous", "test", "stop", "retry_station"):
                channel = request.get("channel")
                if type(channel) is not int or channel not in range(self.controller.channel_count):
                    raise ValueError("Channel is not configured; configure the second board for channels 9-16.")
                if channel not in self.controller.available_channels:
                    raise ValueError("This channel has no assigned output device; configure its Tapo plug first.")
                if channel == self.controller.master_channel:
                    raise ValueError("The master pump is controlled automatically.")
                if (
                    command not in ("stop", "retry_station") and self.controller.rain_paused
                    and not self.controller.zones[channel].rain_pause_exempt
                ):
                    raise ValueError("Rain Pause is enabled.")
                if command in ("manual", "continuous"):
                    duration = request.get("duration_minutes", 10)
                    if command == "continuous" and "duration_minutes" in request:
                        raise ValueError("Continuous runs cannot specify a duration.")
                    self.controller.start_manual(
                        channel, now, continuous=command == "continuous",
                        duration_minutes=duration,
                    )
                elif command == "test":
                    run = self.controller.runs.get(channel)
                    if run and run.source != "Test":
                        raise ValueError("Stop the current watering run before testing.")
                    self.controller.toggle_test(channel, now)
                elif command == "retry_station":
                    self.controller.retry_station(channel, now)
                else:
                    self.controller.stop(channel, now)
            elif command == "read_states":
                try:
                    states = self.relay.read_states()
                except OSError as error:
                    if not self.controller.split_groups:
                        raise
                    self.controller.garden_fault = str(error)
                    self.controller._record(None, "GARDEN FAULT", str(error))
                    self.controller.stop_group(0, now, reason="Garden read fault")
                    return self.status()
                for channel, error in self.relay.errors.items():
                    self.controller.fail_station(channel, OSError(error))
                return {"states": states}
            else:
                raise ValueError(f"Unknown command: {command}")
        except (OSError, RuntimeError, sqlite3.Error, ExceptionGroup) as error:
            self.fail(error)
        return self.status()


def exchange(path: Path, request: dict[str, object]) -> dict:
    """Never retry a command: a lost reply may follow a successful write."""
    data = json.dumps(request).encode() + b"\n"
    if len(data) > MAX_MESSAGE:
        raise ValueError("Control request is too large.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2 if request.get("command") == "status" else 50)
        connection.connect(str(path))
        connection.sendall(data)
        with connection.makefile("rb") as reader:
            response = reader.readline(MAX_MESSAGE + 1)
    if len(response) > MAX_MESSAGE or not response.endswith(b"\n"):
        raise OSError("Incomplete or oversized service reply.")
    try:
        result = json.loads(response)
    except ValueError as error:
        raise OSError("Invalid service reply.") from error
    if not isinstance(result, dict):
        raise OSError("Invalid service reply.")
    if "error" in result:
        raise ValueError(str(result["error"]))
    return result


class RequestHandler(socketserver.StreamRequestHandler):
    server: ControlServer
    timeout = 1

    def handle(self) -> None:
        try:
            data = self.rfile.readline(MAX_MESSAGE + 1)
            if len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
                raise ValueError("Incomplete or oversized command.")
            result = self.server.service.dispatch(json.loads(data))
        except (ValueError, UnicodeError) as error:
            logging.warning("Rejected local control request: %s", error)
            result = {"error": str(error)}
        except (OSError, TimeoutError) as error:
            logging.warning("Local control connection failed: %s", error)
            return
        try:
            self.wfile.write(json.dumps(result).encode() + b"\n")
        except OSError as error:
            logging.warning("Control reply was not delivered: %s", error)


class ControlServer(socketserver.UnixStreamServer):
    def __init__(self, path: Path, service: Service) -> None:
        self.service = service
        super().__init__(str(path), RequestHandler)
        self.timeout = 0.25


def serve(
    config: Path, relay: WaveshareRelays, path: Path, stop: threading.Event,
    secondary: WaveshareRelays | None = None,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Both locks are acquired before any hardware commands. Different socket or
    # config arguments must not create competing owners of this relay device.
    boards = RelayBoards(relay, secondary)
    if mapping_path(config).exists() and secondary is not None:
        raise ValueError("Tapo mappings and a second Waveshare board cannot use the same channels.")
    lock_paths = sorted({
        default_socket().parent / (
            "relay-" + hashlib.sha256(f"{device.host}:{device.port}".encode()).hexdigest()[:16] + ".lock"
        ) for device in boards.devices
    })
    with ExitStack() as stack:
        def acquire_lock(lock_path: Path) -> None:
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = stack.enter_context(lock_path.open("a"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(f"Another controller owns {lock_path}") from error

        acquire_lock(config.with_name(config.name + ".service.lock"))
        if mapping_path(config).exists():
            from tapo_plugs import load_mapping
            lock_paths.extend(
                default_socket().parent / ("tapo-" + plug.mac.replace(":", "") + ".lock")
                for plug in load_mapping(mapping_path(config)) if plug is not None
            )
        for lock_path in sorted(lock_paths):
            acquire_lock(lock_path)
        if path.exists():
            if not path.is_socket():
                raise RuntimeError(f"Refusing to replace a non-socket file: {path}")
            path.unlink()
        service = Service(config, relay, secondary)
        try:
            with ControlServer(path, service) as server:
                path.chmod(0o600)
                logging.info("Controller ready on %s (rain pause: %s)", path,
                             service.controller.rain_paused)
                while not stop.is_set():
                    service.tapo_details.sample_due()
                    service.tick()
                    if isinstance(service.relay, TapoRelays) and service.relay.hot_water is not None:
                        service.relay.hot_water.reconcile()
                    server.handle_request()
        finally:
            try:
                service.controller.close(datetime.now())
            finally:
                if isinstance(service.relay, TapoRelays) and service.relay.hot_water is not None:
                    service.relay.hot_water.shutdown()
                service.history.record(None, "Controller", "STOPPED", "Controller shutdown")
                path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--socket", type=Path, default=default_socket())
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--slave-id", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1)
    parser.add_argument("--second-host", help="Second board IP/host; mutually exclusive with a Tapo mapping file")
    parser.add_argument("--second-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--second-slave-id", type=int, default=1)
    parser.add_argument("--second-timeout", type=float, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    try:
        restored_boards = system_backup.load_connections(args.config.expanduser())
        primary = WaveshareRelays(args.host, args.port, args.slave_id, args.timeout)
        secondary = None if args.second_host is None else WaveshareRelays(
            args.second_host, args.second_port, args.second_slave_id, args.second_timeout,
        )
        if restored_boards is not None:
            primary = restored_boards[0]
            secondary = restored_boards[1] if len(restored_boards) == 2 else None
        serve(args.config.expanduser(),
              primary, args.socket.expanduser(), stop, secondary)
    except (OSError, RuntimeError, ValueError, ExceptionGroup):
        logging.exception("Controller service stopped; outputs may be unknown")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
