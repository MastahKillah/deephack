"""Driver for the DeepCool MYSTIQUE LCD (USB 3633:0009, WinUSB vendor interface).

Protocol reverse engineered from the official DeepCool app (v1.2.15) by hooking its USB layer.

Command frame (48 bytes, OUT ep 0x01 or 0x02, reply on the matching IN ep 0x81 / 0x82):

    AA 2E <cmd> <payload, zero padded to 39 bytes> 'H' 'I' 'D' 'C' <sum16 LE of bytes 0..45>

Reply is the same layout with 0x55 as first byte; payload[0] is a status byte (0 = ok).

Image upload (to on-device flash, addressed by a 32 char id; re-using an id overwrites in place):

    cmd 0x0F                       "start download", acked
    64 byte header                 "DCLd" type size:u32 sum16(data):u32 0*7 id[32] 0*10 sum16(header[:62]):u16
    raw JPEG in 64 byte packets    480x640 baseline JPEG, 4:2:0
    "dcldfinish" padded to 55      no reply; device commits to flash
    cmd 0x08 <type> <index>        show slot (type 0 = jpg, 1 = gif)
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import time

import libusb_package
import usb.core
import usb.util

VID, PID = 0x3633, 0x0009
WIDTH, HEIGHT = 480, 640

CMD_SENSORS = 0x01          # periodic sensor values (ep 2)
CMD_OPTIONS = 0x02          # device options (brightness etc.)
CMD_MODE = 0x03             # display mode
CMD_SITUATION_MODE = 0x04
CMD_RECORDER_MODE = 0x05
CMD_REFRESH_RECORDER = 0x06
CMD_MOTION_MODE = 0x07      # <duration> <animation>
CMD_SHOW_SLOT = 0x08        # <type> <index>
CMD_DELETE_SLOT = 0x09      # <type> <index>
CMD_SET_TIME = 0x0A         # year:u16 month day hour minute second (ep 2)
CMD_START_DOWNLOAD = 0x0F
CMD_ROTATE_QUERY = 0x10
CMD_GET_UUID = 0x12

TYPE_JPG, TYPE_GIF = 0, 1
MODE_MOTION = 2             # the mode in which stored images are displayed
ANIMATION_NONE, ANIMATION_SLIDE, ANIMATION_FADE = 0, 1, 2


def sum16(data: bytes) -> int:
    return sum(data) & 0xFFFF


def frame(cmd: int, payload: bytes = b"") -> bytes:
    if len(payload) > 39:
        raise ValueError("payload too long")
    b = bytearray(48)
    b[0], b[1], b[2] = 0xAA, 0x2E, cmd
    b[3:3 + len(payload)] = payload
    b[42:46] = b"HIDC"
    struct.pack_into("<H", b, 46, sum16(b[:46]))
    return bytes(b)


def upload_header(file_id: str, data: bytes, ftype: int = 1) -> bytes:
    fid = file_id.encode("ascii")
    if len(fid) != 32:
        raise ValueError("file id must be exactly 32 ASCII characters")
    h = bytearray(64)
    h[0:4] = b"DCLd"
    h[4] = ftype
    struct.pack_into("<I", h, 5, len(data))
    struct.pack_into("<I", h, 9, sum16(data))
    h[20:52] = fid
    struct.pack_into("<H", h, 62, sum16(h[:62]))
    return bytes(h)


class DeviceBusy(RuntimeError):
    pass


class Mystique:
    def __init__(self, timeout_ms: int = 2000):
        self.timeout = timeout_ms
        self.dev = libusb_package.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise RuntimeError("MYSTIQUE (3633:0009) not found on USB")
        try:
            usb.util.claim_interface(self.dev, 0)
        except (usb.core.USBError, NotImplementedError) as e:
            raise DeviceBusy(
                "could not claim the MYSTIQUE USB interface - is the DeepCool app still running? "
                f"It holds the device exclusively; quit it first. ({e})"
            ) from e

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, 0)
        finally:
            usb.util.dispose_resources(self.dev)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- low level -------------------------------------------------------
    def command(self, cmd: int, payload: bytes = b"", ep: int = 1, reply: bool = True) -> bytes | None:
        self.dev.write(ep, frame(cmd, payload), self.timeout)
        if not reply:
            return None
        r = bytes(self.dev.read(0x80 | ep, 64, self.timeout))
        if len(r) != 48 or r[0] != 0x55 or r[2] != cmd:
            raise RuntimeError(f"unexpected reply to cmd {cmd:#04x}: {r.hex()}")
        if sum16(r[:46]) != struct.unpack_from("<H", r, 46)[0]:
            raise RuntimeError(f"bad checksum in reply to cmd {cmd:#04x}: {r.hex()}")
        return r[3:42]

    # -- high level ------------------------------------------------------
    def uuid(self) -> str:
        return self.command(CMD_GET_UUID)[1:33].decode("ascii", "replace").rstrip("\0")

    def set_mode(self, mode: int) -> None:
        self.command(CMD_MODE, bytes([mode]))

    def set_slideshow(self, enabled: bool) -> None:
        """Cycle through all stored images (fade transition) or keep the selected one on screen.

        Payload is <duration> <animation>; animation 0 = none, 1 = slide, 2 = fade (names from the official UI).
        """
        self.command(CMD_MOTION_MODE, bytes([0, ANIMATION_FADE if enabled else ANIMATION_NONE]))

    def show_slot(self, index: int, ftype: int = TYPE_JPG) -> None:
        self.command(CMD_SHOW_SLOT, bytes([ftype, index]))

    def delete_slot(self, index: int, ftype: int = TYPE_JPG) -> None:
        self.command(CMD_DELETE_SLOT, bytes([ftype, index]))

    def set_time(self, t: time.struct_time | None = None) -> None:
        t = t or time.localtime()
        self.command(CMD_SET_TIME, struct.pack("<HBBBBB", t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec), ep=2)

    def upload_jpeg(self, file_id: str, jpeg: bytes) -> None:
        """Store a 480x640 baseline JPEG under file_id (overwrites if the id already exists)."""
        if jpeg[:2] != b"\xff\xd8" or jpeg[-2:] != b"\xff\xd9":
            raise ValueError("not a JPEG")
        self.command(CMD_START_DOWNLOAD)
        self.dev.write(1, upload_header(file_id, jpeg), self.timeout)
        for off in range(0, len(jpeg), 64):
            self.dev.write(1, jpeg[off:off + 64], self.timeout)
        self.dev.write(1, b"dcldfinish".ljust(55, b"\0"), self.timeout)
        time.sleep(1.0)  # device writes to flash; the official app also waits here


def encode_jpeg(image, quality: int = 92) -> bytes:
    """Encode a PIL image the way the official app does: 480x640, baseline, 4:2:0, default Huffman tables."""
    from PIL import Image

    if image.size != (WIDTH, HEIGHT):
        image = image.resize((WIDTH, HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality, subsampling="4:2:0", optimize=False, progressive=False)
    return buf.getvalue()


def _main() -> int:
    ap = argparse.ArgumentParser(prog="python -m deephack.device", description="DeepCool MYSTIQUE LCD tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="print device UUID")
    p = sub.add_parser("upload", help="upload an image file to a slot id and show it")
    p.add_argument("image")
    p.add_argument("--id", required=True, help="32 character file id")
    p.add_argument("--index", type=int, help="slot index to show afterwards")
    p = sub.add_parser("show", help="show a stored slot")
    p.add_argument("index", type=int)
    p.add_argument("--gif", action="store_true")
    p = sub.add_parser("delete", help="delete a stored slot")
    p.add_argument("index", type=int)
    p.add_argument("--gif", action="store_true")
    a = ap.parse_args()

    with Mystique() as m:
        if a.cmd == "info":
            print("uuid:", m.uuid())
        elif a.cmd == "upload":
            from PIL import Image

            jpeg = encode_jpeg(Image.open(a.image))
            t = time.time()
            m.upload_jpeg(a.id, jpeg)
            print(f"uploaded {len(jpeg)} bytes in {time.time() - t:.2f}s")
            if a.index is not None:
                m.show_slot(a.index)
        elif a.cmd == "show":
            m.show_slot(a.index, TYPE_GIF if a.gif else TYPE_JPG)
        elif a.cmd == "delete":
            m.delete_slot(a.index, TYPE_GIF if a.gif else TYPE_JPG)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except DeviceBusy as e:
        sys.exit(f"error: {e}")
