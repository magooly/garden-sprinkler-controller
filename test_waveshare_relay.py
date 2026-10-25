"""Protocol tests use mock sockets and a loopback server, never the relay board."""

import socketserver
import struct
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from controller import Controller, default_zones
from waveshare_relay import (
    ModbusDeviceError,
    ModbusError,
    WaveshareRelays,
    main,
    rtu_frame,
)


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def settimeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise AssertionError("Socket timeout must be positive.")

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        # Deliberately split every response across single-byte TCP reads.
        result, self.response = self.response[:1], self.response[1:]
        return result


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = WaveshareRelays()

    def test_vendor_crc_examples(self) -> None:
        self.assertEqual(
            rtu_frame(bytes.fromhex("01 05 00 00 FF 00")),
            bytes.fromhex("01 05 00 00 FF 00 8C 3A"),
        )
        self.assertEqual(
            rtu_frame(bytes.fromhex("01 05 00 00 00 00")),
            bytes.fromhex("01 05 00 00 00 00 CD CA"),
        )

    def test_construction_never_connects(self) -> None:
        with patch("waveshare_relay.socket.create_connection") as connect:
            WaveshareRelays()
        connect.assert_not_called()

    def test_read_only_status_fragmented_and_lsb_order(self) -> None:
        sock = FakeSocket(rtu_frame(bytes((1, 1, 1, 0b10000101))))
        with patch("waveshare_relay.socket.create_connection", return_value=sock) as connect:
            states = self.device.read_states()
        self.assertEqual(states, (True, False, True, False, False, False, False, True))
        self.assertEqual(sock.sent, rtu_frame(bytes.fromhex("01 01 00 00 00 08")))
        connect.assert_called_once_with(("192.168.0.99", 4196), 1.0)

    def test_write_and_readback_for_all_channels_and_states(self) -> None:
        for channel in range(8):
            for state in (False, True):
                with self.subTest(channel=channel, state=state):
                    request = bytes((1, 5)) + struct.pack(
                        ">HH", channel, 0xFF00 if state else 0
                    )
                    write = FakeSocket(rtu_frame(request))
                    read = FakeSocket(rtu_frame(bytes((1, 1, 1, (1 << channel) if state else 0))))
                    with patch(
                        "waveshare_relay.socket.create_connection", side_effect=[write, read]
                    ):
                        self.device.set_relay(channel, state)
                    self.assertEqual(write.sent, rtu_frame(request))
                    self.assertEqual(read.sent[1], 1)

    def test_bad_crc_rejected(self) -> None:
        sock = FakeSocket(bytes((1, 1, 1, 0, 0, 0)))
        with patch("waveshare_relay.socket.create_connection", return_value=sock):
            with self.assertRaisesRegex(ModbusError, "CRC"):
                self.device.read_states()

    def test_modbus_exception_response(self) -> None:
        sock = FakeSocket(rtu_frame(bytes((1, 0x81, 2))))
        with patch("waveshare_relay.socket.create_connection", return_value=sock):
            with self.assertRaisesRegex(ModbusDeviceError, "exception code 02"):
                self.device.read_states()

    def test_incorrect_slave_function_and_byte_count_rejected(self) -> None:
        for payload in (bytes((2, 1, 1, 0)), bytes((1, 2, 1, 0)), bytes((1, 1, 2, 0))):
            with self.subTest(payload=payload):
                sock = FakeSocket(rtu_frame(payload))
                with patch("waveshare_relay.socket.create_connection", return_value=sock):
                    with self.assertRaises(ModbusError):
                        self.device.read_states()

    def test_truncated_response_rejected(self) -> None:
        sock = FakeSocket(bytes((1, 1, 1)))
        with patch("waveshare_relay.socket.create_connection", return_value=sock):
            with self.assertRaisesRegex(ModbusError, "closed"):
                self.device.read_states()

    def test_timeout_is_explicit(self) -> None:
        with patch("waveshare_relay.socket.create_connection", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(ModbusError, "192.168.0.99:4196"):
                self.device.read_states()

    def test_total_transaction_deadline(self) -> None:
        sock = FakeSocket(rtu_frame(bytes((1, 1, 1, 0))))
        with patch("waveshare_relay.socket.create_connection", return_value=sock):
            with patch("waveshare_relay.time.monotonic", side_effect=(10.0, 12.0)):
                with self.assertRaisesRegex(ModbusError, "deadline"):
                    self.device.read_states()

    def test_write_echo_must_match(self) -> None:
        sock = FakeSocket(rtu_frame(bytes.fromhex("01 05 00 01 FF 00")))
        with patch("waveshare_relay.socket.create_connection", return_value=sock):
            with self.assertRaisesRegex(ModbusError, "acknowledgement"):
                self.device.set_relay(0, True)

    def test_readback_must_match(self) -> None:
        write = FakeSocket(rtu_frame(bytes.fromhex("01 05 00 00 FF 00")))
        read = FakeSocket(rtu_frame(bytes((1, 1, 1, 0))))
        with patch("waveshare_relay.socket.create_connection", side_effect=[write, read]):
            with self.assertRaisesRegex(ModbusError, "readback"):
                self.device.set_relay(0, True)

    def test_invalid_parameters(self) -> None:
        for kwargs in (
            {"host": ""}, {"port": 0}, {"port": 65536}, {"port": True},
            {"slave_id": 0}, {"slave_id": 248}, {"slave_id": True},
            {"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")},
            {"timeout": float("inf")}, {"timeout": 11},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                WaveshareRelays(**kwargs)

    def test_invalid_channel_and_state_never_connect(self) -> None:
        with patch("waveshare_relay.socket.create_connection") as connect:
            for channel, state in ((-1, True), (8, True), (True, True), (0, 1)):
                with self.subTest(channel=channel, state=state), self.assertRaises(ValueError):
                    self.device.set_relay(channel, state)
        connect.assert_not_called()

    def test_read_only_discovery_stops_at_first_valid_status(self) -> None:
        with patch("sys.argv", ["waveshare_relay.py", "--discover"]):
            with patch.object(
                WaveshareRelays, "read_states",
                side_effect=[ModbusError("No response"), (False,) * 8],
            ) as read:
                with patch.object(WaveshareRelays, "set_relay") as write:
                    with patch("builtins.print"):
                        main()
        self.assertEqual(read.call_count, 2)
        write.assert_not_called()

    def test_read_failure_exits_nonzero_without_writes(self) -> None:
        with patch("sys.argv", ["waveshare_relay.py"]):
            with patch.object(WaveshareRelays, "read_states", side_effect=ModbusError("offline")):
                with patch.object(WaveshareRelays, "set_relay") as write:
                    with patch("builtins.print"), self.assertRaises(SystemExit) as caught:
                        main()
        self.assertEqual(caught.exception.code, 1)
        write.assert_not_called()


class LoopbackTests(unittest.TestCase):
    def test_controller_over_real_local_tcp_sockets(self) -> None:
        states = [True] * 8
        requests: list[bytes] = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(2)
                data = bytearray()
                while len(data) < 8:
                    chunk = self.request.recv(8 - len(data))
                    if not chunk:
                        return
                    data.extend(chunk)
                frame = bytes(data)
                requests.append(frame)
                if rtu_frame(frame[:-2]) != frame:
                    return
                if frame[1] == 5:
                    channel, value = struct.unpack(">HH", frame[2:6])
                    states[channel] = value == 0xFF00
                    response = frame
                else:
                    bits = sum((1 << channel) for channel, state in enumerate(states) if state)
                    response = rtu_frame(bytes((frame[0], 1, 1, bits)))
                for byte in response:
                    self.request.sendall(bytes((byte,)))

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                device = WaveshareRelays("127.0.0.1", server.server_address[1])
                controller = Controller(default_zones(), device)
                self.assertFalse(any(states))
                controller.start_manual(0, datetime.now())
                self.assertTrue(states[0])
                self.assertTrue(states[7])
                writes = [frame for frame in requests if frame[1] == 5]
                self.assertEqual(struct.unpack(">H", writes[0][2:4])[0], 7)
                self.assertEqual(
                    [struct.unpack(">H", frame[2:4])[0] for frame in writes[-2:]], [0, 7]
                )
                controller.close(datetime.now())
                self.assertFalse(any(states))
                self.assertEqual(len(requests), (8 + 2 + 8) * 2)
            finally:
                server.shutdown()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
