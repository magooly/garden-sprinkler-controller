"""Local P110M transport and offline setup; credentials never enter service replies."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from getpass import getpass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

from kasa import Credentials, Discover, KasaException
from kasa.smart.smartdevice import SmartDevice

from controller import _save_json
from waveshare_relay import WaveshareRelays

OPERATION_SECONDS = 2


def mapping_path(config: Path) -> Path:
    return config.with_name(config.name + ".tapo.json")


def credentials_path(config: Path) -> Path:
    return config.with_name(config.name + ".tapo-credentials.json")


def normalize_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise ValueError("Enter the plug's 12-digit MAC address.")
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


@dataclass(frozen=True)
class Plug:
    host: str
    mac: str


def load_mapping(path: Path) -> tuple[Plug | None, ...]:
    return decode_mapping(json.loads(path.read_text(encoding="utf-8")))


def decode_mapping(data: object) -> tuple[Plug | None, ...]:
    if (
        not isinstance(data, dict) or set(data) != {"version", "plugs"}
        or type(data["version"]) is not int or data["version"] != 1
        or not isinstance(data["plugs"], list) or len(data["plugs"]) != 8
    ):
        raise ValueError("Tapo mapping must contain version 1 and eight plug assignments.")
    result: list[Plug | None] = []
    for record in data["plugs"]:
        if record is None:
            result.append(None)
            continue
        if not isinstance(record, dict) or set(record) != {"host", "mac"}:
            raise ValueError("Each plug assignment needs host and mac.")
        if not isinstance(record["host"], str) or not isinstance(record["mac"], str):
            raise ValueError("Plug host and MAC must be strings.")
        address = ipaddress.IPv4Address(record["host"])
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_link_local:
            raise ValueError("Use the plug's private LAN IPv4 address.")
        result.append(Plug(str(address), normalize_mac(record["mac"])))
    assigned = [plug for plug in result if plug is not None]
    if len({plug.host for plug in assigned}) != len(assigned) or len({plug.mac for plug in assigned}) != len(assigned):
        raise ValueError("Each plug IP and MAC can be assigned only once.")
    return tuple(result)


def load_credentials(path: Path) -> Credentials:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("Tapo credentials must be a regular file owned by this user with mode 0600.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict) or set(data) != {"username", "password"}
        or not isinstance(data["username"], str) or not data["username"].strip()
        or not isinstance(data["password"], str) or not data["password"]
    ):
        raise ValueError("Tapo credentials need a nonempty username and password.")
    return Credentials(data["username"], data["password"])


async def plug_operation(plug: Plug, credentials: Credentials, state: bool | None) -> bool:
    device = None
    try:
        async with asyncio.timeout(OPERATION_SECONDS):
            device = await Discover.discover_single(
                plug.host, credentials=credentials, discovery_timeout=1, timeout=1,
            )
            if not isinstance(device, SmartDevice):
                raise OSError("Expected a supported Tapo smart plug.")
            try:
                device_mac = normalize_mac(device.mac)
            except ValueError:
                raise OSError("Plug returned an invalid MAC; no switch command sent.") from None
            if device_mac != plug.mac:
                raise OSError("Device identity differs from the assigned MAC; no switch command sent.")
            await device.update()
            if device.model.split("(", 1)[0].strip() != "P110M":
                raise OSError("Assigned device is not a P110M; no switch command sent.")
            if state is not None:
                # python-kasa's high-level switch methods retry writes by default.
                await device.protocol.query({"set_device_info": {"device_on": state}}, retry_count=0)
                await device.update()
                if device.is_on != state:
                    raise OSError("Plug readback does not match the requested state.")
            return device.is_on
    except (KasaException, TimeoutError):
        # Library errors can contain protocol data; do not put credentials in logs.
        raise OSError("Tapo request failed or timed out; state is unknown. Check LAN, credentials and firmware.") from None
    finally:
        if device is not None:
            try:
                async with asyncio.timeout(0.5):
                    await device.disconnect()
            except (KasaException, TimeoutError):
                raise OSError("Tapo connection could not be closed; state is unknown.") from None


class TapoRelays:
    """Keep Waveshare channels 1-8; map channels 9-16 to individually assigned plugs."""

    def __init__(self, primary: WaveshareRelays, config: Path) -> None:
        self.primary = primary
        self.plugs = load_mapping(mapping_path(config))
        self.credentials = load_credentials(credentials_path(config)) if any(self.plugs) else None
        self.host, self.port, self.slave_id = primary.host, primary.port, primary.slave_id
        self.channel_count = 16
        self.devices = (primary,)
        self.available_channels = frozenset(range(8)) | frozenset(
            index + 8 for index, plug in enumerate(self.plugs) if plug is not None
        )
        self.states: dict[int, bool | None] = {channel: None for channel in range(8, 16)}
        self.errors: dict[int, str] = {}

    def set_relay(self, channel: int, energized: bool) -> None:
        if type(channel) is not int or channel not in range(16) or type(energized) is not bool:
            raise ValueError("Choose a configured channel and boolean state.")
        if channel < 8:
            self.primary.set_relay(channel, energized)
            return
        plug = self.plugs[channel - 8]
        if plug is None or self.credentials is None:
            raise ValueError(f"Channel {channel + 1}: Tapo plug is unassigned.")
        try:
            self.states[channel] = asyncio.run(plug_operation(plug, self.credentials, energized))
        except OSError as error:
            self.states[channel] = None
            self.errors[channel] = str(error)
            raise OSError(f"Channel {channel + 1}: {error}") from None
        self.errors.pop(channel, None)

    def read_states(self) -> tuple[bool | None, ...]:
        primary = tuple(self.primary.read_states())
        for index, plug in enumerate(self.plugs, 8):
            if plug is not None and self.credentials is not None:
                try:
                    self.states[index] = asyncio.run(plug_operation(plug, self.credentials, None))
                except OSError as error:
                    self.states[index] = None
                    self.errors[index] = str(error)
                    raise OSError(f"Channel {index + 1}: {error}") from None
                self.errors.pop(index, None)
        return primary + tuple(self.states[channel] for channel in range(8, 16))

    def status(self) -> list[dict[str, object]]:
        return [
            {"channel": channel, "assigned": plug is not None,
             "host": plug.host if plug else "", "mac": plug.mac if plug else "",
             "state": self.states[channel], "error": self.errors.get(channel, "")}
            for channel, plug in enumerate(self.plugs, 8)
        ]


async def discover(credentials: Credentials, target: str) -> list[dict[str, str]]:
    devices = {}
    try:
        async with asyncio.timeout(8):
            devices = await Discover.discover(
                credentials=credentials, target=target, discovery_timeout=3, timeout=1,
            )
            result = []
            for device in devices.values():
                await device.update()
                if device.model.split("(", 1)[0].strip() == "P110M":
                    result.append({"host": device.host, "mac": normalize_mac(device.mac), "name": device.alias})
            return result
    finally:
        try:
            async with asyncio.timeout(1):
                await asyncio.gather(*(device.disconnect() for device in devices.values()))
        except (KasaException, TimeoutError):
            raise OSError("Discovery connections could not be closed.") from None


def main() -> None:
    from service import DEFAULT_CONFIG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="Create eight unassigned slots; no network access")
    commands.add_parser("credentials", help="Prompt privately for TP-Link credentials")
    scan = commands.add_parser("discover", help="Read-only LAN discovery; no switching")
    scan.add_argument("--target", default="255.255.255.255")
    assign = commands.add_parser("assign", help="Assign a plug for the next controller restart")
    assign.add_argument("channel", type=int, choices=range(9, 17))
    assign.add_argument("host")
    assign.add_argument("mac")
    unassign = commands.add_parser("unassign", help="Remove an assignment while the controller is stopped")
    unassign.add_argument("channel", type=int, choices=range(9, 17))
    commands.add_parser("show", help="Show assignments; never credentials")
    args = parser.parse_args()
    config = args.config.expanduser()
    path = mapping_path(config)
    try:
        if args.command == "prepare":
            if path.exists():
                load_mapping(path)
                print("Existing assignments preserved.")
            else:
                _save_json(path, {"version": 1, "plugs": [None] * 8})
                print("Tapo slots prepared. All are unassigned and cannot switch.")
        elif args.command == "credentials":
            username = input("TP-Link account email: ").strip()
            password = getpass("TP-Link password (hidden): ")
            if not username or not password:
                raise ValueError("Username and password are required.")
            _save_json(credentials_path(config), {"username": username, "password": password})
            load_credentials(credentials_path(config))
            print("Credentials saved privately. They are excluded from settings backups.")
        elif args.command == "discover":
            credentials = load_credentials(credentials_path(config))
            try:
                found = asyncio.run(discover(credentials, args.target))
            except (KasaException, TimeoutError):
                raise OSError("Discovery failed; check LAN and TP-Link credentials.") from None
            print(json.dumps(found, indent=2))
            if not found:
                print("No P110M plugs discovered. No switching commands were sent.")
        elif args.command in ("assign", "unassign"):
            # Use the same settings lock as the service, including custom socket paths.
            with config.with_name(config.name + ".service.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Stop the controller service before changing plug assignments.") from None
                plugs = load_mapping(path)
                records = [None if plug is None else {"host": plug.host, "mac": plug.mac} for plug in plugs]
                records[args.channel - 9] = (
                    {"host": args.host, "mac": normalize_mac(args.mac)} if args.command == "assign" else None
                )
                data = {"version": 1, "plugs": records}
                decode_mapping(data)
                _save_json(path, data)
            print("Assignment saved. Starting the controller will attempt all assigned plugs OFF.")
        else:
            print(json.dumps([
                {"channel": index, "host": plug.host if plug else None, "mac": plug.mac if plug else None}
                for index, plug in enumerate(load_mapping(path), 9)
            ], indent=2))
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(1, f"Setup failed: {error}\n")


if __name__ == "__main__":
    main()
