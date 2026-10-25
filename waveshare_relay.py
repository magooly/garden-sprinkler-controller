"""Waveshare Modbus POE ETH Relay (B): Modbus RTU frames over TCP."""

from __future__ import annotations

import argparse
import math
import socket
import struct
import time

DEFAULT_HOST = "192.168.0.99"
DEFAULT_PORT = 4196


class ModbusError(OSError):
    """The device response is invalid or reports a Modbus exception."""


class ModbusDeviceError(ModbusError):
    """A valid response indicates that the device rejected the request."""


def rtu_frame(payload: bytes) -> bytes:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return payload + struct.pack("<H", crc)


class WaveshareRelays:
    """Synchronous, bounded requests; use only from one thread at a time.

    Construction does not connect or change outputs. The controller explicitly
    initializes outputs OFF when hardware operation is requested by the user.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        slave_id: int = 1,
        timeout: float = 1.0,
    ) -> None:
        if not host.strip():
            raise ValueError("A relay host is required.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535.")
        if type(slave_id) is not int or not 1 <= slave_id <= 247:
            raise ValueError("Modbus slave ID must be between 1 and 247.")
        if not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("Timeout must be greater than 0 and at most 10 seconds.")
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout

    def _exchange(self, function: int, data: bytes, response_size: int) -> bytes:
        request = rtu_frame(bytes((self.slave_id, function)) + data)
        deadline = time.monotonic() + self.timeout
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as sock:
                def remaining() -> float:
                    seconds = deadline - time.monotonic()
                    if seconds <= 0:
                        raise TimeoutError("Modbus transaction deadline exceeded.")
                    return seconds

                def receive(count: int) -> bytes:
                    result = bytearray()
                    while len(result) < count:
                        sock.settimeout(remaining())
                        chunk = sock.recv(count - len(result))
                        if not chunk:
                            raise ModbusError("Connection closed before a full response.")
                        result.extend(chunk)
                    return bytes(result)

                sock.settimeout(remaining())
                sock.sendall(request)
                header = receive(2)
                if header[0] != self.slave_id:
                    raise ModbusError(f"Unexpected slave ID {header[0]}.")
                if header[1] not in (function, function | 0x80):
                    raise ModbusError(f"Unexpected Modbus function {header[1]:02x}.")
                size = 5 if header[1] & 0x80 else response_size
                response = header + receive(size - 2)
        except OSError as exc:
            raise ModbusError(
                f"{self.host}:{self.port}, slave {self.slave_id}: {exc}"
            ) from exc
        if rtu_frame(response[:-2]) != response:
            raise ModbusError("Invalid Modbus CRC; response cannot be trusted.")
        if response[1] & 0x80:
            raise ModbusDeviceError(
                f"Slave {self.slave_id} rejected function {function:02x}: "
                f"exception code {response[2]:02x}."
            )
        return response[2:-2]

    def read_states(self) -> tuple[bool, ...]:
        """Function 01, coils 0..7. This request cannot switch relays."""
        result = self._exchange(0x01, struct.pack(">HH", 0, 8), 6)
        if result[0] != 1:
            raise ModbusError("Expected exactly one byte of relay status.")
        return tuple(bool(result[1] & (1 << channel)) for channel in range(8))

    def set_relay(self, channel: int, energized: bool) -> None:
        if type(channel) is not int or channel not in range(8):
            raise ValueError("Relay channel must be between 0 and 7.")
        if type(energized) is not bool:
            raise ValueError("Relay state must be true or false.")
        data = struct.pack(">HH", channel, 0xFF00 if energized else 0)
        result = self._exchange(0x05, data, 8)
        if result != data:
            raise ModbusError("Write acknowledgement does not match the requested relay/state.")
        if self.read_states()[channel] != energized:
            raise ModbusError(
                f"Relay {channel + 1} readback does not match the requested state. "
                "Check the relay control mode and external inputs."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Waveshare relay status / slave discovery. No writes are sent."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--slave-id", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument(
        "--discover", action="store_true",
        help="Try slave IDs 1..247, stopping at the first valid relay-status response",
    )
    args = parser.parse_args()
    # Validate all connection options before starting a potentially long scan.
    try:
        WaveshareRelays(args.host, args.port, args.slave_id, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    candidates = range(1, 248) if args.discover else (args.slave_id,)
    for slave_id in candidates:
        device = WaveshareRelays(args.host, args.port, slave_id, args.timeout)
        print(f"Reading {args.host}:{args.port}, slave {slave_id}...", flush=True)
        try:
            states = device.read_states()
        except ModbusError as exc:
            print(f"Read failed: {exc}", flush=True)
            if not args.discover:
                raise SystemExit(1) from exc
        else:
            print(f"Responding slave ID: {slave_id}")
            for channel, state in enumerate(states, 1):
                print(f"Relay {channel}: {'ON' if state else 'OFF'}")
            return
    parser.exit(1, "No valid relay-status response. Check LAN, port, and protocol settings.\n")


if __name__ == "__main__":
    main()
