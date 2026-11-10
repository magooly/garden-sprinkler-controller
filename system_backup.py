"""Password-encrypted application recovery archives; never restore running outputs."""

from __future__ import annotations

import base64
import argparse
import binascii
from contextlib import closing
from datetime import datetime
import hashlib
import fcntl
from getpass import getpass
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from controller import _save_json, decode_zones, encode_zones, load_zones, load_rain_pause
from tapo_plugs import credentials_path, decode_mapping, load_credentials, mapping_path
from timetable_profiles import TimetableProfiles
from waveshare_relay import WaveshareRelays

MAX_BACKUP_BYTES = 4 * 1024 * 1024
MAX_TRANSPORT_BYTES = 8 * 1024 * 1024
AAD = b"garden-sprinkler-system-backup-v1"
PASSWORD_HELP = (
    "Choose a separate backup password (at least 12 characters). A few unrelated "
    "words work well. This is NOT your Tapo or phone sign-in password. Store it "
    "in a password manager or a safe place separate from the backup. "
    "A lost backup password cannot be recovered."
)


def sidecar(config: Path, suffix: str) -> Path:
    return config.with_name(config.name + suffix)


def held(config: Path) -> bool:
    path = sidecar(config, ".restore-hold.json")
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"held"} or type(data["held"]) is not bool:
        raise ValueError("Invalid restore safety hold. Fix it before starting the controller.")
    return data["held"]


def set_hold(config: Path, value: bool) -> None:
    _save_json(sidecar(config, ".restore-hold.json"), {"held": value})


def _key(password: object, salt: bytes) -> bytes:
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise ValueError("Enter the backup password (at most 1024 characters).")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def encrypt(payload: dict, password: object) -> dict:
    if not isinstance(password, str) or len(password) < 12 or not password.strip():
        raise ValueError("Use a backup password with at least 12 characters.")
    plaintext = json.dumps(payload).encode("utf-8")
    if len(plaintext) > MAX_BACKUP_BYTES:
        raise ValueError("Backup contents exceed the 4 MiB limit.")
    salt, nonce = os.urandom(16), os.urandom(12)
    encrypted = AESGCM(_key(password, salt)).encrypt(nonce, plaintext, AAD)
    return {
        "format": "garden-sprinkler-encrypted", "version": 1,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "data": base64.b64encode(encrypted).decode("ascii"),
    }


def decrypt(archive: object, password: object) -> dict:
    if (
        not isinstance(archive, dict)
        or set(archive) != {"format", "version", "salt", "nonce", "data"}
        or archive["format"] != "garden-sprinkler-encrypted"
        or type(archive["version"]) is not int or archive["version"] != 1
    ):
        raise ValueError("Choose a supported encrypted full-system backup, not an old settings export.")
    try:
        if any(not isinstance(archive[key], str) for key in ("salt", "nonce", "data")):
            raise ValueError
        if len(archive["data"]) > (MAX_BACKUP_BYTES + 16) * 4 // 3 + 4:
            raise ValueError
        salt, nonce, data = (base64.b64decode(archive[key], validate=True) for key in ("salt", "nonce", "data"))
        if len(salt) != 16 or len(nonce) != 12 or len(data) > MAX_BACKUP_BYTES + 16:
            raise ValueError
    except (ValueError, binascii.Error):
        raise ValueError("The encrypted backup is damaged or invalid.") from None
    try:
        plaintext = AESGCM(_key(password, salt)).decrypt(nonce, data, AAD)
    except InvalidTag:
        raise ValueError("Incorrect backup password, or the backup file has been damaged. Nothing was restored.") from None
    try:
        payload = json.loads(plaintext)
    except (UnicodeError, ValueError):
        raise ValueError("The decrypted backup contents are invalid.") from None
    validate(payload)
    return payload


def _private_text(path: Path) -> str:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ValueError(f"{path.name} must be a user-owned regular file with mode 0600.")
    if details.st_size > 65536:
        raise ValueError(f"{path.name} is too large.")
    return path.read_text(encoding="utf-8")


def boards_snapshot(relay) -> list[dict]:
    return [
        {"host": device.host, "port": device.port, "slave_id": device.slave_id,
         "timeout": getattr(device, "timeout", 1.0)}
        for device in relay.devices
    ]


def capture(config: Path, relay, history, zones=None) -> dict:
    profiles = TimetableProfiles(config)
    timetables = {
        "version": 2, "groups": {
            str(bank): {"selected": group.selected, "profiles": group.profiles}
            for bank, group in profiles.groups.items()
        },
    }
    mapping = json.loads(mapping_path(config).read_text(encoding="utf-8")) if mapping_path(config).exists() else None
    credentials = None
    if credentials_path(config).exists():
        load_credentials(credentials_path(config))
        credentials = json.loads(_private_text(credentials_path(config)))
    phone_path = config.parent / "phone-password"
    phone_password = _private_text(phone_path) if phone_path.exists() else None
    with closing(history._connect()) as connection:
        events = connection.execute(
            "SELECT timestamp, channel, name, event, reason FROM events ORDER BY id"
        ).fetchall()
    units = {}
    unit_root = Path.home() / ".config/systemd/user"
    for name in ("garden-sprinkler.service", "garden-sprinkler-phone.service"):
        paths = [unit_root / name]
        paths.extend(sorted((unit_root / (name + ".d")).glob("*.conf")))
        for path in paths:
            if path.exists():
                if path.stat().st_size > 65536:
                    raise ValueError("Service configuration is too large to back up.")
                units[str(path.relative_to(unit_root))] = path.read_text(encoding="utf-8")
    root = Path(__file__).parent
    application_files = [
        path for path in sorted(root.glob("*.py")) if not path.name.startswith("test_")
    ]
    application_files.extend(root / name for name in (
        "phone_ui.html", "phone_ui.css", "phone_ui.js", "requirements.txt",
        "garden-sprinkler.service", "garden-sprinkler-phone.service",
    ))
    versions = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in application_files
    }
    payload = {
        "format": "garden-sprinkler-system", "version": 1,
        "created": datetime.now().astimezone().isoformat(),
        "settings": encode_zones(load_zones(config) if zones is None else zones), "timetables": timetables,
        "rain_paused": load_rain_pause(config), "boards": boards_snapshot(relay),
        "mapping": mapping, "credentials": credentials, "phone_password": phone_password,
        "history": events, "service_files": units, "source_hashes": versions,
        "requirements": (root / "requirements.txt").read_text(encoding="utf-8"),
    }
    validate(payload)
    return payload


def validate(payload: object) -> None:
    required = {
        "format", "version", "created", "settings", "timetables", "rain_paused",
        "boards", "mapping", "credentials", "phone_password", "history",
        "service_files", "source_hashes", "requirements",
    }
    if (
        not isinstance(payload, dict) or set(payload) != required
        or payload["format"] != "garden-sprinkler-system"
        or type(payload["version"]) is not int or payload["version"] != 1
    ):
        raise ValueError("Unsupported full-system backup contents.")
    zones = decode_zones(payload["settings"])
    if not isinstance(payload["created"], str):
        raise ValueError("Invalid backup creation date.")
    datetime.fromisoformat(payload["created"])
    if type(payload["rain_paused"]) is not bool:
        raise ValueError("Invalid backed-up Rain Pause state.")
    boards = payload["boards"]
    if not isinstance(boards, list) or not 1 <= len(boards) <= 2:
        raise ValueError("Backup must contain one or two relay boards.")
    for board in boards:
        if not isinstance(board, dict) or set(board) != {"host", "port", "slave_id", "timeout"}:
            raise ValueError("Invalid relay connection configuration.")
        if not isinstance(board["host"], str) or type(board["timeout"]) not in (int, float):
            raise ValueError("Invalid relay host or timeout.")
        WaveshareRelays(**board)
    mapping = payload["mapping"]
    if mapping is not None:
        plugs = decode_mapping(mapping)
        if len(boards) != 1 or len(zones) != 16:
            raise ValueError("Tapo backup must use one primary board and 16 channels.")
        if any(plugs) and payload["credentials"] is None:
            raise ValueError("Assigned Tapo plugs require backed-up credentials.")
    elif len(zones) < len(boards) * 8:
        raise ValueError("Backup channel count does not fit its relay boards.")
    credentials = payload["credentials"]
    if credentials is not None and (
        not isinstance(credentials, dict) or set(credentials) != {"username", "password"}
        or any(not isinstance(value, str) or not value.strip() for value in credentials.values())
    ):
        raise ValueError("Invalid backed-up Tapo credentials.")
    phone_password = payload["phone_password"]
    if phone_password is not None and (not isinstance(phone_password, str) or len(phone_password.strip()) < 4):
        raise ValueError("Invalid backed-up phone sign-in password.")
    with tempfile.TemporaryDirectory() as directory:
        profiles = payload["timetables"]
        if not isinstance(profiles, dict) or profiles.get("version") != 2:
            raise ValueError("Invalid backed-up timetable groups.")
        config = Path(directory) / "settings.json"
        _save_json(config.with_name(config.name + ".timetables.json"), profiles)
        TimetableProfiles(config)
    events = payload["history"]
    if not isinstance(events, list) or len(events) > 1000:
        raise ValueError("Invalid backed-up activity history.")
    for row in events:
        if (
            not isinstance(row, (list, tuple)) or len(row) != 5
            or any(not isinstance(row[index], str) for index in (0, 2, 3, 4))
            or (row[1] is not None and (type(row[1]) is not int or row[1] not in range(16)))
        ):
            raise ValueError("Invalid backed-up history event.")
    files = payload["service_files"]
    if not isinstance(files, dict):
        raise ValueError("Invalid service configuration snapshot.")
    for name, text in files.items():
        parts = name.split("/")
        if not (
            name in ("garden-sprinkler.service", "garden-sprinkler-phone.service")
            or (len(parts) == 2 and parts[0] in (
                "garden-sprinkler.service.d", "garden-sprinkler-phone.service.d",
            ) and parts[1].endswith(".conf") and parts[1] not in (".conf", ".."))
        ) or not isinstance(text, str) or len(text) > 65536:
            raise ValueError("Invalid service configuration filename or contents.")
    if not isinstance(payload["source_hashes"], dict) or not isinstance(payload["requirements"], str):
        raise ValueError("Invalid application version information.")


def summary(payload: dict) -> str:
    groups = payload["timetables"]["groups"]
    count = sum(len(group["profiles"]) for group in groups.values())
    return (
        f"Created: {payload['created']}\n"
        f"{len(payload['settings']['zones'])} channels; {count} named timetables; "
        f"{len(payload['history'])} history events.\n"
        f"Tapo credentials: {'included' if payload['credentials'] else 'not configured'}. "
        f"Phone password: {'included' if payload['phone_password'] else 'not configured'}.\n"
        "Includes Rain Pause, relay connections, Tapo assignments, service configuration "
        "and application version fingerprints. Does not include Ubuntu, app binaries, "
        "network/router configuration or active runs."
    )


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install(config: Path, payload: dict, history) -> None:
    """Caller must persist a safety hold and stop outputs before installation."""
    validate(payload)
    _save_json(config, payload["settings"])
    _save_json(sidecar(config, ".timetables.json"), payload["timetables"])
    _save_json(sidecar(config, ".rain-pause.json"), {"rain_paused": payload["rain_paused"]})
    _save_json(sidecar(config, ".connections.json"), {"boards": payload["boards"]})
    for path, value in (
        (mapping_path(config), payload["mapping"]),
        (credentials_path(config), payload["credentials"]),
    ):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            _save_json(path, value)
    phone_path = config.parent / "phone-password"
    if payload["phone_password"] is not None:
        write_private(phone_path, payload["phone_password"].encode("utf-8"))
    else:
        phone_path.unlink(missing_ok=True)
    with closing(history._connect()) as connection:
        with connection:
            connection.execute("DELETE FROM events")
            connection.executemany(
                "INSERT INTO events (timestamp, channel, name, event, reason) VALUES (?, ?, ?, ?, ?)",
                payload["history"],
            )
    recovery = sidecar(config, ".restored-service-config")
    for name, text in payload["service_files"].items():
        write_private(recovery / name, text.encode("utf-8"))
    _save_json(recovery / "application-version.json", {
        "source_hashes": payload["source_hashes"], "requirements": payload["requirements"],
        "created": payload["created"],
    })
    write_private(recovery / "requirements.txt", payload["requirements"].encode("utf-8"))
    write_private(recovery / "RECOVERY.txt", (
        "Reinstall magooly/garden-sprinkler-controller and its requirements on a replacement computer.\n"
        "Saved relay connections are loaded from the settings .connections.json sidecar.\n"
        "Service files here are reference copies, NOT automatically installed or executed.\n"
        "Review paths, usernames, custom socket/password-file flags and any environment overrides\n"
        "before installing these files into ~/.config/systemd/user and running daemon-reload.\n"
        "The standard phone-password file is restored; restart phone service and sign in again.\n"
        "Custom phone-password file locations must be updated manually using this restored file.\n"
        "Restart controller service, reopen desktop, check all devices, then Resume operation.\n"
        "Active runs, Ubuntu packages, network/router settings and OS secrets are not restored.\n"
    ).encode("utf-8"))


def load_connections(config: Path) -> list[WaveshareRelays] | None:
    path = sidecar(config, ".connections.json")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], list) or not 1 <= len(data["boards"]) <= 2:
        raise ValueError("Invalid restored relay connections.")
    boards = []
    for board in data["boards"]:
        if not isinstance(board, dict) or set(board) != {"host", "port", "slave_id", "timeout"}:
            raise ValueError("Invalid restored relay connection.")
        if not isinstance(board["host"], str) or type(board["timeout"]) not in (int, float):
            raise ValueError("Invalid restored relay host or timeout.")
        boards.append(WaveshareRelays(**board))
    return boards


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or recover an encrypted sprinkler backup offline.")
    parser.add_argument("action", choices=("inspect", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/garden-sprinkler-controller/settings.json")
    args = parser.parse_args()
    try:
        if args.archive.stat().st_size > MAX_TRANSPORT_BYTES:
            raise ValueError("Backup file is too large.")
        password = getpass("Backup password (not phone/Tapo password): ")
        payload = decrypt(json.loads(args.archive.read_text(encoding="utf-8")), password)
        print(summary(payload))
        if args.action == "inspect":
            return
        config = args.config.expanduser()
        config.parent.mkdir(parents=True, exist_ok=True)
        with sidecar(config, ".service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Stop the controller service before offline restoration.") from None
            print("Offline disaster recovery overwrites configuration. Use the existing before-restore archive to undo a failed restore.")
            if input("Type RESTORE to replace configuration and keep operation held OFF: ") != "RESTORE":
                print("Cancelled; nothing changed.")
                return
            from activity_history import ActivityHistory
            set_hold(config, True)
            history = ActivityHistory(config)
            install(config, payload, history)
            history.record(None, "Controller", "RESTORED", "Offline recovery; persistent safety hold")
        print("Restored. Start/restart services, reopen desktop, review devices and settings, then Resume operation.")
        print("The phone password saved in this backup applies. Review the .restored-service-config/RECOVERY.txt guide.")
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Recovery failed: {error}\n")


if __name__ == "__main__":
    main()
