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

Animations ("GIFs") are a sequence of JPEG frames: one start command, then header + JPEG per frame with
type 2 and frame_count:u16 total_duration_ms:u16 frame_index:u16 at header offset 13, then one finish.
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
CMD_OPTIONS = 0x02          # device options: <?> <?> <?> <light source> <brightness>
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
CMD_FACTORY_RESET = 0x13    # reboots, erases every stored image (cmd 12 returns junk until a power cycle)
CMD_DELETE_ALL = 0x14       # erases every stored image, no reboot

TYPE_JPG, TYPE_GIF = 0, 1   # slot type in the show / delete commands
FILE_JPG, FILE_GIF = 1, 2   # file type in the upload header
MAX_GIF_DURATION_MS = 0xFFFF
MAX_STILLS = 10             # the cooler keeps stills 0 to 9; more are dropped without an error, and asking for
                            # one that is not there does nothing (found with the webcam)
MODE_SITUATIONAL = 1        # the firmware's own sensor dashboard, fed by CMD_SENSORS
MODE_MOTION = 2             # the mode in which stored images are displayed
MODE_RECORDER = 3           # the firmware's CPU clock / temperature recorder

# CMD_SENSORS payload: 3-byte fields, each <integer part:u16 LE> <fraction:u8> (the fraction byte is
# printed as-is after the decimal point). Confirmed with photos of the device: cpu_temp, ram_usage, the
# every named field below. The official app only ever filled the first seven in our captures (its fan
# and pump readouts were always zero); fan_rpm and pump_rpm were found by probing. field_9 / field_10
# have not shown up on any layout.
SENSOR_FIELDS = {"cpu_temp": 0, "cpu_load": 3, "ram_usage": 6, "v3_3": 9, "v5": 12, "v12": 15, "cpu_freq": 18,
                 "fan_rpm": 21, "pump_rpm": 24, "field_9": 27, "field_10": 30}
DASH_FREQUENCY, DASH_TIME, DASH_PUMP, DASH_FAN, DASH_FAN_PUMP, DASH_TEMPERATURE = range(6)
STRIP_VOLTAGE, STRIP_SYSTEM, STRIP_CORE = 0, 1, 2   # bottom strip of the dashboard
ANIMATION_NONE, ANIMATION_SLIDE, ANIMATION_FADE = 0, 1, 2
# What drives the light strip on the side of the pump head (fourth byte of CMD_OPTIONS; the app calls it
# rgbOptical): the motherboard's ARGB signal through the cooler's own ARGB cable, the cooler's fixed
# temperature warning colour, or the colours at the edges of the picture on screen, picked by the
# firmware whenever a stored image or animation is shown.
LIGHT_MOTHERBOARD, LIGHT_WARNING, LIGHT_SCREEN = 0, 1, 2


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


def sensor_payload(**values: float) -> bytes:
    payload = bytearray(39)
    for key, value in values.items():
        hundredths = round(value * 100)
        if not 0 <= hundredths <= 0xFFFF * 100 + 99:
            raise ValueError(f"{key} out of range")
        struct.pack_into("<HB", payload, SENSOR_FIELDS[key], hundredths // 100, hundredths % 100)
    return bytes(payload)


def upload_header(file_id: str, data: bytes, ftype: int = FILE_JPG, frame_count: int = 0, duration_ms: int = 0,
                  frame_index: int = 0) -> bytes:
    """Header that precedes every uploaded JPEG. The three frame fields are zero for stills."""
    fid = file_id.encode("ascii")
    if len(fid) != 32:
        raise ValueError("file id must be exactly 32 ASCII characters")
    h = bytearray(64)
    h[0:4] = b"DCLd"
    h[4] = ftype
    struct.pack_into("<I", h, 5, len(data))
    struct.pack_into("<I", h, 9, sum16(data))
    struct.pack_into("<HHH", h, 13, frame_count, duration_ms, frame_index)
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

    def set_options(self, brightness: int = 70, light: int = LIGHT_WARNING) -> None:
        """Backlight brightness, 0 (off) to 100, and the light strip's source (LIGHT_*). The first three bytes
        are sent as the official app sends them (its names: noLoadScreen, gyroStatus, screenRotate); they
        showed no visible effect."""
        self.command(CMD_OPTIONS, bytes([1, 1, 0, light, max(0, min(100, brightness))]))

    def set_brightness(self, percent: int) -> None:
        self.set_options(percent)

    def set_mode(self, mode: int) -> None:
        self.command(CMD_MODE, bytes([mode]))

    def send_sensors(self, **values: float) -> None:
        """Feed live values to the firmware's sensor dashboard (not stored, so no flash wear). Keys: SENSOR_FIELDS.

        Values are sent with two decimals; which of them are visible depends on the dashboard layout
        selected on the cooler.
        """
        self.command(CMD_SENSORS, sensor_payload(**values), ep=2)

    def set_dashboard(self, main: int, style: int = 0, strip: int = STRIP_VOLTAGE, byte2: int = 0) -> None:
        """Layout of the sensor dashboard. main: DASH_* (0 frequency, 1 time, 2 pump, 3 CPU fan, 4 CPU fan +
        pump, 5 temperature). style (frequency only): 0 normal, 1 ring, 2 stopwatch. strip is the row at
        the bottom: voltage (3.3 / 5 / 12 V), system (frequency, CPU load, RAM) or core (frequency, RAM).
        byte2 sits between style and strip on the wire; 1 changed nothing visible."""
        self.command(CMD_SITUATION_MODE, bytes([main, style, byte2, strip]))

    def set_recorder(self, frequency_only: int = 0) -> None:
        """Graphs of the recorder mode: CPU frequency and temperature (0) or frequency only (1)."""
        self.command(CMD_RECORDER_MODE, bytes([1 if frequency_only else 0]))

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

    def upload_gif(self, file_id: str, frames: list[bytes], duration_ms: int) -> None:
        """Store an animation: a list of 480x640 JPEG frames that loops over duration_ms in total."""
        if not frames or not 0 < duration_ms <= MAX_GIF_DURATION_MS:
            raise ValueError("need at least one frame and a duration of 1..65535 ms")
        self.command(CMD_START_DOWNLOAD)
        for i, jpeg in enumerate(frames):
            if jpeg[:2] != b"\xff\xd8" or jpeg[-2:] != b"\xff\xd9":
                raise ValueError("not a JPEG")
            self.dev.write(1, upload_header(file_id, jpeg, FILE_GIF, len(frames), duration_ms, i), self.timeout)
            for off in range(0, len(jpeg), 64):
                self.dev.write(1, jpeg[off:off + 64], self.timeout)
        self.dev.write(1, b"dcldfinish".ljust(55, b"\0"), self.timeout)
        time.sleep(1.0)


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
    p = sub.add_parser("probe-sensors", help="show the firmware's sensor dashboard fed with one recognisable number per field")
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--mode", type=int, default=MODE_SITUATIONAL, choices=(MODE_SITUATIONAL, MODE_RECORDER))
    p = sub.add_parser("map-dashboards", help="step through the sensor dashboard layouts with test values (beeps at each step)")
    p.add_argument("--hold", type=int, default=15, help="seconds per layout")
    a = ap.parse_args()

    with Mystique() as m:
        if a.cmd == "map-dashboards":
            import winsound

            probe = {"cpu_load": 22, "ram_usage": 63, "v3_3": 3.33, "v5": 5.05, "v12": 12.12, "cpu_freq": 4.44,
                     "fan_rpm": 1111, "pump_rpm": 2222, "field_9": 3333, "field_10": 4444}
            steps = [  # (main, style, strip, byte2); cpu_temp is fed as 10 + step to tell photos apart
                (DASH_TEMPERATURE, 0, STRIP_VOLTAGE, 0), (DASH_TEMPERATURE, 0, STRIP_SYSTEM, 0),
                (DASH_TEMPERATURE, 0, STRIP_CORE, 0), (DASH_FREQUENCY, 0, STRIP_SYSTEM, 0),
                (DASH_TIME, 0, STRIP_SYSTEM, 0), (DASH_FAN_PUMP, 0, STRIP_SYSTEM, 0),
            ]
            print("feeding:", probe)
            m.set_time()
            m.set_mode(MODE_SITUATIONAL)
            try:
                for n, step in enumerate(steps, 1):
                    m.set_dashboard(*step)
                    print(f"step {n:2d}/{len(steps)}: main={step[0]} style={step[1]} strip={step[2]} byte2={step[3]}", flush=True)
                    for i in range(a.hold):
                        m.send_sensors(cpu_temp=10 + n, **probe)
                        if i == 1:
                            winsound.Beep(1200 if n % 2 else 900, 180)   # the layout has settled: photo time
                        time.sleep(1)
            finally:
                m.set_dashboard(DASH_TEMPERATURE, 1)    # the layout the cooler had before
                m.set_mode(MODE_MOTION)
                winsound.Beep(600, 500)
            return 0
        if a.cmd == "probe-sensors":
            probe = {"cpu_temp": 51, "cpu_load": 22, "ram_usage": 63, "v3_3": 3.33, "v5": 5.05, "v12": 12.12, "cpu_freq": 4.44,
                     "fan_rpm": 1111, "pump_rpm": 2222}
            print("feeding:", probe)
            m.set_mode(a.mode)
            try:
                for _ in range(a.seconds):
                    m.send_sensors(**probe)
                    time.sleep(1)
            finally:
                m.set_mode(MODE_MOTION)
            return 0
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
