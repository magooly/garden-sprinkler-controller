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
        "protocol": 1,
        "settings": encode_zones(default_zones()),
        "channel_count": 8,
        "master_channel": 7,
        "runs": [],
        "rain_paused": False,
        "pump_on": False,
        "fault": "",
    }


class PhoneServerTests(unittest.TestCase):
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
