"""A model of the MYSTIQUE for tests: what we know about its USB protocol and firmware, as rules.

It stands in for the pyusb device, so the real driver (deephack.device) runs against it byte for byte.
It keeps the state the firmware keeps (display mode, stored stills and animations by index, what is on
screen, options, clock, sensor screens) and records a violation for everything known to be unsafe or
wrong, from the README's protocol notes and docs/firmware-analysis.md. The `cooler` fixture fails a test
that leaves any violation behind.
"""
from __future__ import annotations

import io
import struct

import usb.core
from PIL import Image

from deephack import device

FINISH = b"dcldfinish".ljust(55, b"\0")
NO_EFFECT = {0x05, 0x06, 0x0B, 0x0C, 0x0D, 0x0E, 0x10, 0x11, 0x15, 0x16, 0x17}   # answered, nothing to model


class CoolerModel:
    def __init__(self, stored_stills: int = 1, stored_gifs: int = 0, uuid: str = "0123456789ABCDEF0123456789ABCDEF"):
        self.uuid = uuid
        self.mode: int | None = None            # device.MODE_*; None until the first CMD_MODE
        self.stills = [[f"app-still-{i}", b""] for i in range(stored_stills)]       # [file id, jpeg] by index
        self.gifs = [[f"app-gif-{i}", [], 1000] for i in range(stored_gifs)]       # [file id, frames, ms]
        self.shown: tuple[str, int] | None = None   # ("jpg" | "gif", index) picked with CMD_SHOW_SLOT
        self.options: tuple[int, ...] | None = None
        self.slideshow: tuple[int, int] | None = None
        self.dashboard: tuple[int, ...] | None = None
        self.recorder: int | None = None
        self.sensors: bytes | None = None
        self.clock: tuple[int, ...] | None = None
        self.flash_writes = 0                   # JPEG files committed to flash (an animation counts each frame)
        self.log: list[tuple] = []              # what happened, in order: ("cmd", name, payload) / ("stored", kind, index)
        self.violations: list[str] = []
        self._replies: dict[int, list[bytes]] = {0x81: [], 0x82: []}
        self._upload: dict | None = None

    # ---- what the firmware shows
    @property
    def screen(self):
        if self.mode == device.MODE_SITUATIONAL:
            return ("dashboard", self.dashboard)
        if self.mode == device.MODE_RECORDER:
            return ("recorder", self.recorder)
        return self.shown

    @property
    def light(self) -> int | None:
        return self.options[3] if self.options else None

    def still(self, index: int) -> Image.Image:
        return Image.open(io.BytesIO(self.stills[index][1]))

    def _violate(self, why: str) -> None:
        self.violations.append(why)

    # ---- the pyusb surface the driver uses
    def write(self, ep: int, data, timeout=None) -> int:
        data = bytes(data)
        is_command = len(data) == 48 and data[:2] == b"\xaa\x2e"
        if self._upload is not None and not (is_command and data[42:46] == b"HIDC"):
            self._upload_packet(data)
        elif is_command:
            if self._upload is not None:
                self._violate(f"command {data[2]:#04x} before dcldfinish: an unfinished upload hung the firmware once")
                self._upload = None
            self._command(ep, data)
        else:
            self._violate(f"{len(data)} stray bytes on endpoint {ep:#x} outside an upload")
        return len(data)

    def read(self, ep: int, size: int, timeout=None):
        queue = self._replies.get(ep)
        if not queue:
            raise usb.core.USBTimeoutError("no reply waiting")
        return queue.pop(0)

    # ---- commands
    def _reply(self, ep: int, cmd: int, payload: bytes = b"", known: bool = True) -> None:
        b = bytearray(48)
        b[0], b[1], b[2] = 0x55, 0x2E, cmd if known else 0x00    # unknown commands come back as command 00
        b[3:3 + len(payload)] = payload
        b[42:46] = b"HIDC"
        struct.pack_into("<H", b, 46, device.sum16(b[:46]))
        self._replies[0x80 | ep].append(bytes(b))

    def _command(self, ep: int, data: bytes) -> None:
        if data[42:46] != b"HIDC" or device.sum16(data[:46]) != struct.unpack_from("<H", data, 46)[0]:
            self._violate(f"malformed command frame {data.hex()}")
            return
        cmd, payload = data[2], data[3:42]
        self.log.append(("cmd", cmd, payload))
        if cmd in (device.CMD_FACTORY_RESET, device.CMD_DELETE_ALL):
            self._violate(f"command {cmd:#04x} erases every stored image")
        on_ep2 = cmd in (device.CMD_SENSORS, device.CMD_SET_TIME)
        if on_ep2 != (ep == 2):
            self._violate(f"command {cmd:#04x} sent on endpoint {ep}")
        reply = b"\x00"
        if cmd == device.CMD_SENSORS:
            self.sensors = payload
        elif cmd == device.CMD_SET_TIME:
            self.clock = struct.unpack_from("<HBBBBB", payload)
        elif cmd == device.CMD_OPTIONS:
            self.options = tuple(payload[:5])
            if payload[3] not in (device.LIGHT_MOTHERBOARD, device.LIGHT_WARNING, device.LIGHT_SCREEN):
                self._violate(f"unknown light source {payload[3]}")
        elif cmd == device.CMD_MODE:
            if payload[0] not in (device.MODE_SITUATIONAL, device.MODE_MOTION, device.MODE_RECORDER):
                self._violate(f"unknown display mode {payload[0]}")
            self.mode = payload[0]
        elif cmd == device.CMD_SITUATION_MODE:
            self.dashboard = tuple(payload[:4])
        elif cmd == device.CMD_RECORDER_MODE:
            self.recorder = payload[0]
        elif cmd == device.CMD_MOTION_MODE:
            self.slideshow = (payload[0], payload[1])
        elif cmd == device.CMD_SHOW_SLOT:
            kind, index = payload[0], payload[1]
            slots = self.stills if kind == device.TYPE_JPG else self.gifs
            if index >= len(slots):
                self._violate(f"show {'gif' if kind else 'jpg'} {index}, but only {len(slots)} are stored")
            if self.mode != device.MODE_MOTION:
                self._violate("show a stored image without switching back to image mode first (deephack's rule)")
            self.shown = ("gif" if kind == device.TYPE_GIF else "jpg", index)
        elif cmd == device.CMD_DELETE_SLOT:
            slots = self.stills if payload[0] == device.TYPE_JPG else self.gifs
            if payload[1] < len(slots):
                slots.pop(payload[1])
            else:
                self._violate(f"delete slot {payload[1]}, which does not exist")
        elif cmd == device.CMD_START_DOWNLOAD:
            self._upload = {"stage": "header", "files": []}
        elif cmd == device.CMD_GET_UUID:
            reply = b"\x00" + self.uuid.encode()
        elif cmd not in NO_EFFECT and cmd not in (device.CMD_FACTORY_RESET, device.CMD_DELETE_ALL):
            self._reply(ep, cmd, known=False)
            return
        self._reply(ep, cmd, reply)

    # ---- uploads: 0F, then a 64 byte header and the data in 64 byte packets per file, then dcldfinish
    def _upload_packet(self, data: bytes) -> None:
        up = self._upload
        if data.startswith(b"dcldfinish"):
            self._finish(data)
        elif len(data) > 64:
            self._violate(f"{len(data)} byte packet in an upload (at most 64)")
        elif up["stage"] == "header":
            if len(data) != 64 or data[:4] != b"DCLd":
                self._violate("upload data where a DCLd header belongs")
                return
            if device.sum16(data[:62]) != struct.unpack_from("<H", data, 62)[0]:
                self._violate("upload header checksum is wrong")
            ftype, size, total = data[4], *struct.unpack_from("<II", data, 5)
            count, duration, index = struct.unpack_from("<HHH", data, 13)
            up["current"] = {"type": ftype, "size": size, "sum": total, "count": count, "duration": duration,
                             "index": index, "id": data[20:52].decode("ascii", "replace"), "data": b""}
            up["stage"] = "data"
        else:
            cur = up["current"]
            cur["data"] += data
            if len(cur["data"]) > cur["size"]:
                self._violate("more upload data than the header announced")
            if len(cur["data"]) >= cur["size"]:
                if device.sum16(cur["data"]) != cur["sum"] & 0xFFFF:
                    self._violate("upload data does not match the header's sum16: the firmware waits forever")
                up["files"].append(cur)
                up["stage"] = "header"

    def _finish(self, data: bytes) -> None:
        up, self._upload = self._upload, None
        if data != FINISH:
            self._violate("malformed dcldfinish")
        if up["stage"] == "data":
            self._violate("dcldfinish before all of a file's data arrived")
            return
        files = up["files"]
        if not files:
            self._violate("an upload without a file")
            return
        for f in files:
            try:
                img = Image.open(io.BytesIO(f["data"]))
                img.load()
                if img.size != (device.WIDTH, device.HEIGHT) or img.format != "JPEG":
                    self._violate(f"stored a {img.format} of {img.size}, the screen is {device.WIDTH}x{device.HEIGHT} JPEG")
            except OSError:
                self._violate("stored something that is not a readable image")
        file_id = files[0]["id"]
        if any(f["id"] != file_id for f in files):
            self._violate("one upload with several file ids")
        if all(f["type"] == device.FILE_JPG for f in files):
            if len(files) != 1:
                self._violate("several stills in one upload")
            self._store(self.stills, file_id, [file_id, files[-1]["data"]], "jpg")
        elif all(f["type"] == device.FILE_GIF for f in files):
            if [f["index"] for f in files] != list(range(len(files))) or any(f["count"] != len(files) for f in files):
                self._violate("animation frames missing or out of order")
            if len({f["duration"] for f in files}) != 1:
                self._violate("animation frames disagree on the loop duration")
            self._store(self.gifs, file_id, [file_id, [f["data"] for f in files], files[0]["duration"]], "gif")
        else:
            self._violate("stills and animation frames mixed in one upload")
        self.flash_writes += len(files)

    def _store(self, slots: list, file_id: str, entry: list, kind: str) -> None:
        for i, slot in enumerate(slots):
            if slot[0] == file_id:                 # an id that exists is replaced in place
                slots[i] = entry
                self.log.append(("stored", kind, i))
                return
        slots.append(entry)
        self.log.append(("stored", kind, len(slots) - 1))
