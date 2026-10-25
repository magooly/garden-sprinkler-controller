"""Headless relay owner and local, same-user control socket."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
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
    Controller, decode_zones, encode_zones, load_rain_pause, load_zones,
    save_rain_pause, save_zones,
)
from waveshare_relay import DEFAULT_HOST, DEFAULT_PORT, WaveshareRelays

MAX_MESSAGE = 65536
DEFAULT_CONFIG = Path.home() / ".config/garden-sprinkler-controller/settings.json"


def default_socket() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "garden-sprinkler-controller/control.sock"


def error_details(error: Exception) -> str:
    if isinstance(error, ExceptionGroup):
        return "; ".join(error_details(item) for item in error.exceptions)
    return str(error)


class Service:
    def __init__(self, config: Path, relay: WaveshareRelays) -> None:
        self.config = config
        self.relay = relay
        self.controller = Controller(
            load_zones(config), relay, rain_paused=load_rain_pause(config)
        )
        self.revision = uuid.uuid4().hex
        self.fault = ""

    def fail(self, error: Exception) -> None:
        self.fault = error_details(error)
        logging.error("Scheduling stopped: %s", self.fault)
        self.controller.closed = True
        try:
            self.controller.stop_all(datetime.now())
        except ExceptionGroup as shutdown:
            self.fault += "; OFF failures: " + error_details(shutdown)
            logging.error("Shutdown failed: %s", error_details(shutdown))

    def tick(self) -> None:
        try:
            self.controller.tick(datetime.now())
        except (OSError, RuntimeError, ExceptionGroup) as error:
            self.fail(error)

    def status(self) -> dict[str, object]:
        controller = self.controller
        return {
            "protocol": 1,
            "settings": encode_zones(controller.zones),
            "revision": self.revision,
            "rain_paused": controller.rain_paused,
            "master_channel": controller.master_channel,
            "pump_on": controller.pump_on,
            "fault": self.fault,
            "host": self.relay.host,
            "port": self.relay.port,
            "slave_id": self.relay.slave_id,
            "runs": [
                {"channel": channel, "source": run.source,
                 "remaining": max(0, run.deadline - controller.clock())}
                for channel, run in controller.runs.items()
            ],
        }

    def dispatch(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or not isinstance(request.get("command"), str):
            raise ValueError("Expected an object with a command.")
        command = request["command"]
        if command == "status":
            return self.status()
        if command == "stop_all":
            try:
                self.controller.stop_all(datetime.now())
            except ExceptionGroup as error:
                self.fail(error)
            return self.status()
        if self.fault:
            raise ValueError("Service fault: " + self.fault + ". Check hardware and restart the service.")
        now = datetime.now()
        try:
            if command == "save":
                if request.get("revision") != self.revision:
                    raise ValueError(
                        "Settings changed in another app or the service restarted. "
                        "Close and reopen this window before saving; your edits have not been applied."
                    )
                zones = decode_zones(request.get("settings"))
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
            elif command in ("manual", "test", "stop"):
                channel = request.get("channel")
                if type(channel) is not int or channel not in range(8):
                    raise ValueError("Channel must be a number from 0 through 7.")
                if channel == self.controller.master_channel:
                    raise ValueError("The master pump is controlled automatically.")
                if command != "stop" and self.controller.rain_paused:
                    raise ValueError("Rain Pause is enabled.")
                if command == "manual":
                    self.controller.start_manual(channel, now)
                elif command == "test":
                    run = self.controller.runs.get(channel)
                    if run and run.source != "Test":
                        raise ValueError("Stop the current watering run before testing.")
                    self.controller.toggle_test(channel, now)
                else:
                    self.controller.stop(channel, now)
            elif command == "read_states":
                return {"states": self.relay.read_states()}
            else:
                raise ValueError(f"Unknown command: {command}")
        except (OSError, RuntimeError, ExceptionGroup) as error:
            self.fail(error)
        return self.status()


def exchange(path: Path, request: dict[str, object]) -> dict:
    """Never retry a command: a lost reply may follow a successful write."""
    data = json.dumps(request).encode() + b"\n"
    if len(data) > MAX_MESSAGE:
        raise ValueError("Control request is too large.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2 if request.get("command") == "status" else 25)
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


def serve(config: Path, relay: WaveshareRelays, path: Path, stop: threading.Event) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Both locks are acquired before any hardware commands. Different socket or
    # config arguments must not create competing owners of this relay device.
    identity = hashlib.sha256(f"{relay.host}:{relay.port}".encode()).hexdigest()[:16]
    lock_paths = (
        default_socket().parent / f"relay-{identity}.lock",
        config.with_name(config.name + ".service.lock"),
    )
    with ExitStack() as stack:
        for lock_path in lock_paths:
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = stack.enter_context(lock_path.open("a"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(f"Another controller owns {lock_path}") from error
        if path.exists():
            if not path.is_socket():
                raise RuntimeError(f"Refusing to replace a non-socket file: {path}")
            path.unlink()
        service = Service(config, relay)
        try:
            with ControlServer(path, service) as server:
                path.chmod(0o600)
                logging.info("Controller ready on %s (rain pause: %s)", path,
                             service.controller.rain_paused)
                while not stop.is_set():
                    service.tick()
                    server.handle_request()
        finally:
            try:
                service.controller.close(datetime.now())
            finally:
                path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--socket", type=Path, default=default_socket())
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--slave-id", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    try:
        serve(args.config.expanduser(),
              WaveshareRelays(args.host, args.port, args.slave_id, args.timeout),
              args.socket.expanduser(), stop)
    except (OSError, RuntimeError, ValueError, ExceptionGroup):
        logging.exception("Controller service stopped; outputs may be unknown")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
