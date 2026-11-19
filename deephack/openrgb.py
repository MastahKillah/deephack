"""Minimal client for the OpenRGB SDK server (TCP, localhost:6742 by default).

OpenRGB drives the PC's RGB hardware (motherboard headers, RAM, fans, ...); its SDK server lets other
programs set colours. Only what deephack needs is here: list the devices with their zones and LED
counts, switch a device to its direct ("custom") mode, and set all its LEDs. Nothing is saved on the
hardware: direct colours last until the next mode change or power-off.

Every packet is "ORGB", device index u32, packet id u32, payload size u32, payload (all little endian).
The controller description is versioned; protocol 4 is spoken here (the server answers with the lower
of its and our version), with the fields of versions 1 to 3 handled for older servers.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

PORT = 6742
CLIENT_VERSION = 4
REQUEST_CONTROLLER_COUNT, REQUEST_CONTROLLER_DATA, REQUEST_PROTOCOL_VERSION, SET_CLIENT_NAME = 0, 1, 40, 50
DEVICE_LIST_UPDATED = 100
UPDATELEDS, SETCUSTOMMODE = 1050, 1100


@dataclass(frozen=True)
class Zone:
    name: str
    leds: int


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    zones: tuple[Zone, ...]
    leds: int                     # all LEDs of the device, the length UPDATELEDS wants


def packet(device: int, pid: int, data: bytes = b"") -> bytes:
    return b"ORGB" + struct.pack("<III", device, pid, len(data)) + data


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, fmt: str):
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return values[0] if len(values) == 1 else values

    def string(self) -> str:
        n = self.take("H")
        text = self.data[self.pos:self.pos + n].rstrip(b"\0").decode("utf8", "replace")
        self.pos += n
        return text

    def skip(self, n: int) -> None:
        self.pos += n


def parse_controller(index: int, data: bytes, version: int) -> Device:
    """Name, zones and LED count from a REQUEST_CONTROLLER_DATA answer."""
    r = _Reader(data)
    r.take("I")                                   # data size
    r.take("i")                                   # device type
    name = r.string()
    for _ in range(5 if version >= 1 else 4):     # vendor (1+), description, version, serial, location
        r.string()
    modes = r.take("H")
    r.take("i")                                   # active mode
    for _ in range(modes):
        r.string()
        r.skip(4 * 4 + (8 if version >= 3 else 0))           # value, flags, speed min / max, brightness min / max
        r.skip(4 * 3 + (4 if version >= 3 else 0) + 4 * 2)   # colours min / max, speed, brightness, direction, colour mode
        r.skip(4 * r.take("H"))                               # mode colours
    zones = []
    for _ in range(r.take("H")):
        zname = r.string()
        r.take("iII")                             # type, LEDs min / max
        count = r.take("I")
        r.skip(r.take("H"))                       # matrix map
        if version >= 4:
            for _ in range(r.take("H")):          # segments
                r.string()
                r.take("iII")
        zones.append(Zone(zname, count))
    return Device(index, name, tuple(zones), r.take("H"))


class Client:
    def __init__(self, host: str = "127.0.0.1", port: int = PORT, timeout: float = 3.0, name: str = "deephack"):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.list_changed = False                 # the server announced added or removed devices
        try:
            server = struct.unpack("<I", self._request(0, REQUEST_PROTOCOL_VERSION, struct.pack("<I", CLIENT_VERSION)))[0]
        except socket.timeout:
            server = 0                            # servers from before the version request do not answer it
        self.version = min(server, CLIENT_VERSION)
        self.sock.sendall(packet(0, SET_CLIENT_NAME, name.encode() + b"\0"))

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OpenRGB closed the connection")
            buf += chunk
        return buf

    def _request(self, device: int, pid: int, data: bytes = b"") -> bytes:
        self.sock.sendall(packet(device, pid, data))
        while True:
            head = self._recv(16)
            if head[:4] != b"ORGB":
                raise ConnectionError("not an OpenRGB server")
            _, got, size = struct.unpack("<III", head[4:])
            body = self._recv(size)
            if got == pid:
                return body
            if got == DEVICE_LIST_UPDATED:
                self.list_changed = True

    def devices(self) -> list[Device]:
        self.list_changed = False
        count = struct.unpack("<I", self._request(0, REQUEST_CONTROLLER_COUNT))[0]
        arg = struct.pack("<I", self.version) if self.version else b""
        return [parse_controller(i, self._request(i, REQUEST_CONTROLLER_DATA, arg), self.version) for i in range(count)]

    def set_custom_mode(self, device: int) -> None:
        self.sock.sendall(packet(device, SETCUSTOMMODE))

    def set_leds(self, device: int, colours: list[tuple[int, int, int]]) -> None:
        body = struct.pack("<H", len(colours)) + b"".join(bytes((r, g, b, 0)) for r, g, b in colours)
        self.sock.sendall(packet(device, UPDATELEDS, struct.pack("<I", 4 + len(body)) + body))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
