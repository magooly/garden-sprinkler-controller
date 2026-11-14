"""Authentication, loopback binding, and command routing for phone control."""

from http.cookies import SimpleCookie
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from controller import default_zones, encode_zones
from phone_server import LOGIN_FAILURE_LIMIT, LOGIN_FAILURE_WINDOW, create_server, load_password

PASSWORD = "a-private-phone-password-long-enough"


def service_status() -> dict[str, object]:
    return {
        "protocol": 2,
        "continuous_supported": True,
        "manual_timers_supported": True,
        "continuous_reminders_supported": True,
        "full_backup_supported": True,
        "settings": encode_zones(default_zones()),
        "channel_count": 8,
        "master_channel": 7,
        "runs": [],
        "rain_paused": False,
        "pump_on": False,
        "fault": "",
        "revision": "revision-1",
    }


class PhoneServerTests(unittest.TestCase):
    def test_group_stops_and_station_retry_require_auth_csrf_and_preserve_local_faults(self):
        for action in ("stop_garden", "stop_appliances", "retry_station"):
            body = {"action": action, "channel": 8}
            self.assertEqual(self.request("POST", "/api/command", body)[0], 401)
        cookie, csrf = self.login()
        data = service_status()
        data.update({
            "settings": encode_zones(default_zones(16)), "channel_count": 16,
            "group_controls_supported": True, "garden_fault": "",
            "channel_faults": {"8": "plug unavailable"},
        })
        self.mock_exchange.return_value = data
        for action in ("stop_garden", "stop_appliances", "retry_station"):
            self.mock_exchange.reset_mock()
            body = {"action": action, "channel": 8}
            self.assertEqual(self.request("POST", "/api/command", body, cookie)[0], 403)
            self.mock_exchange.assert_not_called()
            status, response, _ = self.request("POST", "/api/command", body, cookie, csrf)
            self.assertEqual(status, 200)
            self.assertFalse(response["status"]["fault"])
            self.assertFalse(response["status"]["channels"][0]["fault"])
            self.assertEqual(response["status"]["channels"][8]["fault"], "plug unavailable")
            expected = {"command": action}
            if action == "retry_station":
                expected["channel"] = 8
            self.mock_exchange.assert_called_once_with(self.socket_path, expected)

    def test_tapo_timeline_auth_and_channel_validation(self) -> None:
        self.assertEqual(self.request("GET", "/api/tapo-timeline?channel=8")[0], 401)
        cookie, _ = self.login()
        self.mock_exchange.return_value = {"channel": 8, "segments": [], "events": []}
        status, data, _ = self.request("GET", "/api/tapo-timeline?channel=8", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["timeline"]["channel"], 8)
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "tapo_timeline", "channel": 8},
        )
        self.mock_exchange.reset_mock()
        for query in ("channel=7", "channel=16", "channel=8&channel=9", "channel=bad", "channel=8&extra=1"):
            self.assertEqual(self.request("GET", "/api/tapo-timeline?" + query, cookie=cookie)[0], 503)
        self.mock_exchange.assert_not_called()

    def test_tapo_details_require_auth_and_only_route_read_command(self) -> None:
        self.assertEqual(self.request("GET", "/api/tapo-details")[0], 401)
        cookie, _ = self.login()
        self.mock_exchange.return_value = {"reading": False, "rows": []}
        status, body, _ = self.request("GET", "/api/tapo-details?refresh=1", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["details"]["rows"], [])
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "tapo_details", "refresh": True},
        )
        self.mock_exchange.reset_mock()
        self.assertEqual(self.request("GET", "/api/tapo-details?refresh=invalid", cookie=cookie)[0], 503)
        self.mock_exchange.assert_not_called()

    def test_window_pause_is_revision_guarded_and_changes_only_selected_group(self) -> None:
        cookie, csrf = self.login()
        current = service_status()
        self.mock_exchange.return_value = current
        body = {"channel": 1, "group": 2, "paused": True, "revision": "revision-1"}
        self.assertEqual(self.request("POST", "/api/window-pause", body, cookie)[0], 403)
        self.mock_exchange.assert_not_called()
        for changes in ({"group": -1}, {"group": 4}, {"group": True}, {"paused": 1},
                        {"channel": 7}, {"channel": 15}, {"revision": "stale"}):
            self.assertEqual(self.request(
                "POST", "/api/window-pause", {**body, **changes}, cookie, csrf,
            )[0], 400)
        self.mock_exchange.reset_mock()
        status, response, _ = self.request("POST", "/api/window-pause", body, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(self.mock_exchange.call_count, 2)
        save = self.mock_exchange.call_args.args[1]
        from controller import decode_zones
        from dataclasses import replace
        expected = default_zones()
        expected[1] = replace(expected[1], paused_windows=(False, False, True, False))
        self.assertEqual(decode_zones(save["settings"]), expected)
        self.assertEqual(save["revision"], "revision-1")
        self.assertIn("paused_windows", response["status"]["channels"][1])

    def test_rain_pause_requires_csrf_and_boolean(self) -> None:
        cookie, csrf = self.login()
        self.mock_exchange.return_value = service_status()
        body = {"action": "rain_pause", "paused": True}
        self.assertEqual(self.request("POST", "/api/command", body, cookie)[0], 403)
        self.mock_exchange.assert_not_called()
        for invalid in (None, 1, "true"):
            status, _, _ = self.request(
                "POST", "/api/command", {**body, "paused": invalid}, cookie, csrf,
            )
            self.assertEqual(status, 400)
        self.mock_exchange.assert_not_called()
        for paused in (True, False):
            status, _, _ = self.request(
                "POST", "/api/command", {**body, "paused": paused}, cookie, csrf,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                self.mock_exchange.call_args.args[1],
                {"command": "rain_pause", "paused": paused},
            )

    def test_reminder_change_is_revision_guarded_and_validated(self) -> None:
        cookie, csrf = self.login()
        current = service_status()
        self.mock_exchange.return_value = current
        body = {"channel": 1, "minutes": 30, "revision": "revision-1"}
        self.assertEqual(self.request("POST", "/api/reminder", body, cookie)[0], 403)
        status, _, _ = self.request("POST", "/api/reminder", body, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(self.mock_exchange.call_count, 2)
        save = self.mock_exchange.call_args.args[1]
        self.assertEqual(save["revision"], "revision-1")
        self.assertEqual(save["settings"]["zones"][1]["continuous_reminder_minutes"], 30)
        self.mock_exchange.reset_mock()
        status, _, _ = self.request("POST", "/api/reminder", {**body, "minutes": 0}, cookie, csrf)
        self.assertEqual(status, 400)
        self.mock_exchange.assert_not_called()
        status, _, _ = self.request("POST", "/api/reminder", {**body, "revision": "stale"}, cookie, csrf)
        self.assertEqual(status, 400)
        self.assertEqual(self.mock_exchange.call_count, 1)

    def test_backup_restore_requires_auth_csrf_and_revision(self) -> None:
        self.assertEqual(self.request("GET", "/api/backup")[0], 401)
        cookie, csrf = self.login()
        self.mock_exchange.return_value = service_status()
        status, backup, _ = self.request("GET", "/api/backup", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("settings", backup)
        self.assertEqual(backup["revision"], "revision-1")
        self.mock_exchange.reset_mock()
        archive = {"format": "test-encrypted-archive"}
        payload = {"archive": archive, "password": "four unrelated backup words", "revision": "revision-1"}
        self.assertEqual(self.request("POST", "/api/restore", payload, cookie)[0], 403)
        self.mock_exchange.assert_not_called()
        status, _, _ = self.request("POST", "/api/restore", payload, cookie, csrf)
        self.assertEqual(status, 200)
        self.mock_exchange.assert_called_once_with(self.socket_path, {
            "command": "backup_restore", **payload,
        })
        self.mock_exchange.reset_mock()
        status, _, _ = self.request("POST", "/api/restore", {
            "archive": archive, "revision": "revision-1",
        }, cookie, csrf)
        self.assertEqual(status, 400)
        self.mock_exchange.assert_not_called()

    def test_full_backup_export_and_preview_require_csrf_and_do_not_return_plaintext_settings(self) -> None:
        cookie, csrf = self.login()
        payload = {"password": "four unrelated backup words"}
        self.mock_exchange.return_value = {"archive": {"data": "encrypted"}, "summary": "All settings", "revision": "r"}
        self.mock_exchange.reset_mock()
        self.assertEqual(self.request("POST", "/api/backup", payload, cookie)[0], 403)
        self.mock_exchange.assert_not_called()
        status, response, _ = self.request("POST", "/api/backup", payload, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertNotIn("settings", response)
        self.mock_exchange.assert_called_once_with(self.socket_path, {"command": "backup_export", **payload})
        self.mock_exchange.reset_mock()
        preview = {**payload, "archive": {"data": "encrypted"}}
        self.assertEqual(self.request("POST", "/api/backup-preview", preview, cookie, csrf)[0], 200)
        self.mock_exchange.assert_called_once_with(self.socket_path, {"command": "backup_preview", **preview})

    def test_activity_requires_sign_in_and_passes_offset(self) -> None:
        status, _, _ = self.request("GET", "/api/history?offset=25")
        self.assertEqual(status, 401)
        cookie, _ = self.login()
        self.mock_exchange.return_value = {
            "events": [], "offset": 25, "total": 25, "page_size": 25, "warning": "",
        }
        status, data, _ = self.request("GET", "/api/history?offset=25", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["history"]["offset"], 25)
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "history", "offset": 25},
        )
        self.mock_exchange.reset_mock()
        status, _, _ = self.request("GET", "/api/history?offset=bad", cookie=cookie)
        self.assertEqual(status, 503)
        self.mock_exchange.assert_not_called()
        self.mock_exchange.return_value = {"events": [], "offset": 0, "total": 0, "page_size": 25, "warning": ""}
        status, _, _ = self.request("GET", "/api/history?offset=0&filter=faults", cookie=cookie)
        self.assertEqual(status, 200)
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "history", "offset": 0, "filter": "faults"},
        )

    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.socket_path = Path(self.directory) / "control.sock"
        self.server = create_server(
            "127.0.0.1", 0, self.socket_path, PASSWORD,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(self._close_server)
        self.exchange = patch("phone_server.exchange")
        self.mock_exchange = self.exchange.start()
        self.addCleanup(self.exchange.stop)

    def _close_server(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(
        self, method: str, path: str, body: object | None = None,
        cookie: str = "", csrf: str = "",
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        headers = {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        self.connection.request(method, path, body=encoded, headers=headers)
        response = self.connection.getresponse()
        data = json.loads(response.read())
        return response.status, data, dict(response.getheaders())

    def login(self) -> tuple[str, str]:
        status, data, headers = self.request(
            "POST", "/api/login", {"password": PASSWORD},
        )
        self.assertEqual(status, 200)
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        session_cookie = cookie["garden_session"].OutputString()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return session_cookie, str(data["csrf"])

    def test_static_interface_is_available_without_exposing_control_data(self) -> None:
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn(b"Sprinklers", response.read())
        self.mock_exchange.assert_not_called()

    def test_status_and_commands_require_login_and_csrf(self) -> None:
        status, _, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 401)
        cookie, csrf = self.login()
        status, _, _ = self.request(
            "POST", "/api/command", {"action": "run", "channel": 0}, cookie,
        )
        self.assertEqual(status, 403)
        self.mock_exchange.assert_not_called()
        self.mock_exchange.return_value = service_status()
        status, data, _ = self.request("GET", "/api/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"]["channels"][0]["channel"], 0)
        self.assertEqual(data["status"]["channels"][7]["master"], True)
        self.assertIs(data["status"]["channels"][0]["pump_exempt"], False)
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "status"},
        )
        status, _, _ = self.request(
            "POST", "/api/command", {"action": "run", "channel": 0}, cookie, csrf,
        )
        self.assertEqual(status, 200)
        self.mock_exchange.assert_called_with(
            self.socket_path, {"command": "manual", "channel": 0},
        )

    def test_password_file_requires_private_permissions_and_minimum_length(self) -> None:
        path = Path(self.directory) / "phone-password"
        path.write_text(PASSWORD, encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(load_password(path), PASSWORD)
        path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            load_password(path)
        path.chmod(0o600)
        path.write_text("1234", encoding="utf-8")
        self.assertEqual(load_password(path), "1234")
        path.write_text("123", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "at least 4"):
            load_password(path)

    def test_four_digit_pin_login(self) -> None:
        self.server.expected_password = "1234"
        status, _, _ = self.request("POST", "/api/login", {"password": "1234"})
        self.assertEqual(status, 200)
        self.mock_exchange.assert_not_called()

    def test_failed_login_limit_blocks_until_window_expires(self) -> None:
        with patch("phone_server.time.monotonic", return_value=1000) as clock:
            for _ in range(LOGIN_FAILURE_LIMIT):
                status, _, _ = self.request("POST", "/api/login", {"password": "wrong"})
                self.assertEqual(status, 401)
            status, _, _ = self.request("POST", "/api/login", {"password": PASSWORD})
            self.assertEqual(status, 429)
            clock.return_value = 1000 + LOGIN_FAILURE_WINDOW - 1
            status, _, _ = self.request("POST", "/api/login", {"password": PASSWORD})
            self.assertEqual(status, 429)
            clock.return_value = 1000 + LOGIN_FAILURE_WINDOW
            status, _, _ = self.request("POST", "/api/login", {"password": PASSWORD})
            self.assertEqual(status, 200)
        self.mock_exchange.assert_not_called()

    def test_server_refuses_non_loopback_bind_addresses(self) -> None:
        for host in ("0.0.0.0", "192.0.2.1", "localhost"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "127.0.0.1"):
                create_server(host, 8765, self.socket_path, PASSWORD)

    def test_unknown_actions_and_master_pump_are_rejected(self) -> None:
        cookie, csrf = self.login()
        status, _, _ = self.request(
            "POST", "/api/command", {"action": "test"}, cookie, csrf,
        )
        self.assertEqual(status, 400)
        self.mock_exchange.assert_not_called()
        self.mock_exchange.side_effect = ValueError(
            "The master pump is controlled automatically.",
        )
        status, data, _ = self.request(
            "POST", "/api/command", {"action": "run", "channel": 7}, cookie, csrf,
        )
        self.assertEqual(status, 409)
        self.assertIn("master pump", data["error"])
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "manual", "channel": 7},
        )

    def test_stop_and_stop_all_actions_are_forwarded(self) -> None:
        cookie, csrf = self.login()
        self.mock_exchange.return_value = service_status()
        for body, command in (
            ({"action": "stop", "channel": 0}, {"command": "stop", "channel": 0}),
            ({"action": "stop_all"}, {"command": "stop_all"}),
        ):
            with self.subTest(command=command):
                status, _, _ = self.request(
                    "POST", "/api/command", body, cookie, csrf,
                )
                self.assertEqual(status, 200)
                self.assertEqual(self.mock_exchange.call_args.args, (
                    self.socket_path, command,
                ))

    def test_duration_forwarded_and_older_service_rejected(self) -> None:
        cookie, csrf = self.login()
        self.mock_exchange.return_value = service_status()
        status, data, _ = self.request(
            "POST", "/api/command",
            {"action": "run", "channel": 0, "duration_minutes": 30}, cookie, csrf,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(data["status"]["channels"][0]["max_manual_minutes"])
        self.mock_exchange.assert_called_with(
            self.socket_path, {"command": "manual", "channel": 0, "duration_minutes": 30},
        )
        self.mock_exchange.reset_mock()
        self.mock_exchange.return_value = {**service_status(), "manual_timers_supported": False}
        status, _, _ = self.request(
            "POST", "/api/command",
            {"action": "run", "channel": 0, "duration_minutes": 30}, cookie, csrf,
        )
        self.assertEqual(status, 409)
        self.mock_exchange.assert_called_once_with(self.socket_path, {"command": "status"})
        self.mock_exchange.reset_mock()
        for duration in (0, 1441, True, "30"):
            status, _, _ = self.request(
                "POST", "/api/command",
                {"action": "run", "channel": 0, "duration_minutes": duration}, cookie, csrf,
            )
            self.assertEqual(status, 400)
        self.mock_exchange.assert_not_called()

    def test_continuous_command_and_null_remaining_are_forwarded(self) -> None:
        cookie, csrf = self.login()
        self.mock_exchange.return_value = {
            **service_status(),
            "runs": [{"channel": 0, "source": "Continuous", "remaining": None}],
        }
        status, data, _ = self.request(
            "POST", "/api/command", {"action": "continuous", "channel": 0}, cookie, csrf,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["status"]["continuous_supported"])
        self.assertIsNone(data["status"]["channels"][0]["running"]["remaining"])
        self.mock_exchange.assert_called_once_with(
            self.socket_path, {"command": "continuous", "channel": 0},
        )
        status, _, _ = self.request(
            "POST", "/api/command", {"action": "continuous", "channel": 0}, cookie,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.mock_exchange.call_count, 1)

    def test_null_remaining_rejected_for_timed_run(self) -> None:
        cookie, _ = self.login()
        self.mock_exchange.return_value = {
            **service_status(),
            "runs": [{"channel": 0, "source": "Manual", "remaining": None}],
        }
        status, _, _ = self.request("GET", "/api/status", cookie=cookie)
        self.assertEqual(status, 503)

    def test_logout_invalidates_session(self) -> None:
        cookie, csrf = self.login()
        status, _, _ = self.request(
            "POST", "/api/logout", {}, cookie, csrf,
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/status", cookie=cookie)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
