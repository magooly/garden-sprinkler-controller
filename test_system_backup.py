"""Encrypted recovery, complete settings snapshots and fail-closed restoration."""

import copy
import base64
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
import fcntl
from unittest.mock import patch

from controller import default_zones, load_zones, save_zones, save_rain_pause
from service import Service
from tapo_plugs import credentials_path, mapping_path
from test_service import FakeDevice
from timetable_profiles import TimetableProfiles
import system_backup as backup

PASSWORD = "four unrelated backup words"


class SystemBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.enterContext(patch("system_backup.Path.home", return_value=self.root))
        self.config = self.root / "settings.json"
        zones = default_zones()
        save_zones(self.config, zones)
        save_rain_pause(self.config, True)
        profiles = TimetableProfiles(self.config)
        profiles.save("Summer", zones)
        credentials_path(self.config).write_text(json.dumps({
            "username": "example@example.invalid", "password": "private-tapo-secret",
        }))
        credentials_path(self.config).chmod(0o600)
        self.phone = self.root / "phone-password"
        self.phone.write_text("private-phone-secret\n")
        self.phone.chmod(0o600)
        unit = self.root / ".config/systemd/user/garden-sprinkler.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/example/service.py\n")
        self.service = Service(self.config, FakeDevice())

    def export(self) -> dict:
        return self.service.dispatch({"command": "backup_export", "password": PASSWORD})["archive"]

    def test_roundtrip_contains_all_components_and_no_plaintext_secrets(self) -> None:
        archive = self.export()
        serialized = json.dumps(archive)
        for secret in ("private-tapo-secret", "private-phone-secret", "example@example.invalid", "Summer"):
            self.assertNotIn(secret, serialized)
        payload = backup.decrypt(archive, PASSWORD)
        self.assertEqual(payload["credentials"]["password"], "private-tapo-secret")
        self.assertEqual(payload["phone_password"], "private-phone-secret\n")
        self.assertEqual(payload["timetables"]["groups"]["0"]["selected"], "Summer")
        self.assertTrue(payload["rain_paused"])
        self.assertIn("garden-sprinkler.service", payload["service_files"])
        self.assertTrue(payload["source_hashes"])
        self.assertTrue(payload["requirements"])
        self.assertTrue(payload["history"])
        self.assertNotEqual(archive, self.export())

    def test_password_validation_corruption_and_unsupported_archives(self) -> None:
        archive = self.export()
        corrupted = bytearray(base64.b64decode(archive["data"]))
        corrupted[-1] ^= 1
        before = self.config.read_bytes()
        for candidate, password in (
            (archive, "wrong password"),
            ({**archive, "data": "bad base64"}, PASSWORD),
            ({**archive, "version": True}, PASSWORD),
            ({**archive, "data": base64.b64encode(corrupted).decode("ascii")}, PASSWORD),
            ({"version": 9, "zones": []}, PASSWORD),
        ):
            with self.subTest(candidate=list(candidate)), self.assertRaises(ValueError):
                backup.decrypt(candidate, password)
        self.assertEqual(self.config.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "12"):
            backup.encrypt({}, "short")
        with self.assertRaises(ValueError):
            backup.decrypt(archive, "x" * 1025)

    def test_restore_saves_encrypted_recovery_and_requires_restart_and_resume(self) -> None:
        zones = default_zones()
        zones[0] = replace(
            zones[0], enabled=True, rain_pause_exempt=True,
            schedule=(("0000", "2359") + ("",) * 6,) * 7,
        )
        save_zones(self.config, zones)
        self.service.controller.apply(zones, datetime.now())
        self.assertIn(0, self.service.controller.runs)
        archive = self.export()
        changed = list(zones)
        changed[0] = replace(changed[0], name="New name")
        save_zones(self.config, changed)
        self.service.controller.apply(changed, datetime.now())
        preview = self.service.dispatch({"command": "backup_preview", "archive": archive, "password": PASSWORD})
        result = self.service.dispatch({
            "command": "backup_restore", "archive": archive, "password": PASSWORD,
            "revision": preview["revision"],
        })
        self.assertTrue(result["operation_held"])
        self.assertTrue(result["restore_restart_required"])
        self.assertFalse(any(self.service.relay.devices[0].states))
        self.assertNotEqual(load_zones(self.config)[0].name, "New name")
        recovery = Path(result["recovery_backup"])
        self.assertEqual(os.stat(recovery).st_mode & 0o777, 0o600)
        self.assertEqual(backup.decrypt(json.loads(recovery.read_text()), PASSWORD)["settings"]["zones"][0]["name"], "New name")
        with self.assertRaisesRegex(ValueError, "Restart"):
            self.service.dispatch({"command": "resume_operation"})
        with self.assertRaisesRegex(ValueError, "held"):
            self.service.dispatch({"command": "manual", "channel": 1})
        restarted = Service(self.config, FakeDevice())
        self.assertTrue(restarted.status()["operation_held"])
        restarted.tick()
        self.assertFalse(restarted.controller.runs)
        self.assertFalse(restarted.controller.pump_on)
        with self.assertRaisesRegex(ValueError, "held"):
            restarted.controller.start_manual(1, datetime.now())
        from controller import encode_zones
        edited = list(restarted.controller.zones)
        edited[1] = replace(edited[1], name="Reviewed before resuming")
        restarted.dispatch({
            "command": "save", "settings": encode_zones(edited), "revision": restarted.revision,
        })
        self.assertEqual(load_zones(self.config)[1].name, "Reviewed before resuming")
        self.assertFalse(restarted.controller.runs)
        self.assertFalse(restarted.dispatch({"command": "resume_operation"})["operation_held"])
        self.assertIn(0, restarted.controller.runs)
        self.assertFalse(backup.held(self.config))
        self.assertTrue((backup.sidecar(self.config, ".restored-service-config") / "RECOVERY.txt").exists())
        self.assertTrue(TimetableProfiles(self.config).groups[0].profiles["Summer"])
        self.assertEqual(self.phone.read_text(), "private-phone-secret\n")
        self.assertEqual(credentials_path(self.config).stat().st_mode & 0o777, 0o600)

    def test_wrong_password_or_stale_revision_changes_nothing(self) -> None:
        archive = self.export()
        original = self.config.read_bytes()
        for password, revision in (("bad password", self.service.revision), (PASSWORD, "stale")):
            with self.assertRaises(ValueError):
                self.service.dispatch({
                    "command": "backup_restore", "archive": archive,
                    "password": password, "revision": revision,
                })
        self.assertFalse(backup.held(self.config))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.before-restore-*")), [])

    def test_install_failure_keeps_hold_and_encrypted_recovery(self) -> None:
        archive = self.export()
        with patch("system_backup.install", side_effect=OSError("Disk full")):
            result = self.service.dispatch({
                "command": "backup_restore", "archive": archive, "password": PASSWORD,
                "revision": self.service.revision,
            })
        self.assertIn("Disk full", result["fault"])
        self.assertTrue(backup.held(self.config))
        self.assertTrue(list(self.root.glob("*.before-restore-*")))
        restarted = Service(self.config, FakeDevice())
        restarted.tick()
        self.assertTrue(restarted.controller.operation_held)
        self.assertFalse(restarted.controller.runs)

    def test_invalid_payloads_are_rejected_before_outputs_or_files_change(self) -> None:
        payload = backup.decrypt(self.export(), PASSWORD)
        for key, value in (
            ("service_files", {"../../danger.service": "bad"}),
            ("credentials", {"username": "x", "password": ""}),
            ("rain_paused", "false"), ("history", [["bad"]]),
            ("boards", [{"host": "", "port": 0, "slave_id": 1, "timeout": 1}]),
        ):
            candidate = {**payload, key: value}
            archive = backup.encrypt(candidate, PASSWORD)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.service.dispatch({
                    "command": "backup_restore", "archive": archive, "password": PASSWORD,
                    "revision": self.service.revision,
                })
        self.assertFalse(backup.held(self.config))

    def test_absent_optional_files_remove_old_assignments_and_passwords(self) -> None:
        payload = backup.decrypt(self.export(), PASSWORD)
        payload.update(credentials=None, phone_password=None, mapping=None)
        mapping_path(self.config).write_text('{"version":1,"plugs":[null,null,null,null,null,null,null,null]}')
        backup.install(self.config, payload, self.service.history)
        self.assertFalse(mapping_path(self.config).exists())
        self.assertFalse(credentials_path(self.config).exists())
        self.assertFalse(self.phone.exists())
        self.assertEqual(backup.load_connections(self.config)[0].host, self.service.relay.host)
        history = self.service.history.page(0)["events"]
        self.assertEqual(
            [[event[key] for key in ("timestamp", "channel", "name", "event", "reason")] for event in reversed(history)],
            payload["history"],
        )

    def test_incomplete_or_insecure_credentials_prevent_export(self) -> None:
        credentials_path(self.config).chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            self.export()
        credentials_path(self.config).chmod(0o600)
        credentials_path(self.config).write_text("{}")
        with self.assertRaises(ValueError):
            self.export()

    def test_tapo_topology_and_channel_counts_validate_without_connecting(self) -> None:
        payload = backup.decrypt(self.export(), PASSWORD)
        payload["settings"]["zones"].extend(copy.deepcopy(payload["settings"]["zones"]))
        payload["mapping"] = {"version": 1, "plugs": [
            {"host": "192.168.0.50", "mac": "aa:bb:cc:dd:ee:ff"}, *([None] * 7),
        ]}
        backup.validate(payload)
        payload["credentials"] = None
        with self.assertRaisesRegex(ValueError, "credentials"):
            backup.validate(payload)

    def test_off_peak_policy_survives_encrypted_backup_and_install(self) -> None:
        payload = backup.decrypt(self.export(), PASSWORD)
        payload["settings"]["zones"].extend(copy.deepcopy(payload["settings"]["zones"]))
        payload["mapping"] = {"version": 1, "plugs": [None] * 7 + [
            {"host": "192.168.0.121", "mac": "aa:bb:cc:dd:ee:ff", "off_peak": True},
        ]}
        restored = backup.decrypt(backup.encrypt(payload, PASSWORD), PASSWORD)
        backup.validate(restored)
        backup.install(self.config, restored, self.service.history)
        self.assertEqual(json.loads(mapping_path(self.config).read_text()), payload["mapping"])

    def test_offline_recovery_is_available_when_current_settings_are_corrupt(self) -> None:
        archive = self.root / "recovery.sprinkler-backup"
        archive.write_text(json.dumps(self.export()))
        self.config.write_text("corrupt settings")
        with patch("sys.argv", ["system_backup.py", "restore", str(archive), "--config", str(self.config)]), \
                patch("system_backup.getpass", return_value=PASSWORD), \
                patch("builtins.input", return_value="RESTORE"), patch("builtins.print"):
            backup.main()
        self.assertEqual(len(load_zones(self.config)), 8)
        self.assertTrue(backup.held(self.config))

    def test_offline_restore_refuses_a_running_controller_lock(self) -> None:
        archive = self.root / "recovery.sprinkler-backup"
        archive.write_text(json.dumps(self.export()))
        with backup.sidecar(self.config, ".service.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("sys.argv", ["system_backup.py", "restore", str(archive), "--config", str(self.config)]), \
                    patch("system_backup.getpass", return_value=PASSWORD), \
                    patch("builtins.print"), patch("sys.stderr"), self.assertRaises(SystemExit):
                backup.main()
        self.assertFalse(backup.held(self.config))
