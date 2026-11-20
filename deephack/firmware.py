"""The cooler's firmware: which one it runs, and giving it the one that draws pictures straight from USB.

FLASHING CAN KILL THE COOLER'S SCREEN. What happens: an upload like a picture's, but with file type 0.
The running firmware stores the data as FW_update.bin on its SD card and restarts the chip; the
bootloader, which nobody here has seen (it is not in the update file), then writes the file over the
firmware. Nothing can be done from USB while that runs, and nothing afterwards if the new firmware does
not start: there is no way back short of a debug probe on the board.

Two images can be sent, both v2.70:

- "direct": DeepCool's image with the direct drawing patch (firmware_patch.py; its source is
  tools/fw/patch.py). Pictures are then drawn from RAM, and nothing is written to the cooler's flash.
- "stock": DeepCool's own image, as its app ships it. The way back.

DeepCool's image is not part of deephack. It is read from the DeepCool app's folder, checked by its
hash, and a copy is kept next to deephack's settings, so that the way back does not depend on that app
staying installed.

What is known (docs/firmware-analysis.md has the details):

- On the one cooler this was done on, the bootloader took both images, 12 seconds off the bus each time.
- That cooler came with v2.71. There is no image of v2.71 anywhere: for a cooler that reports 2.71,
  either image is a downgrade that cannot be undone.
- The running firmware checks the size and a 16 bit sum before it stores the file. A file that arrives
  short or damaged is not stored and nothing restarts (seen in the emulator, tests/test_flash.py).
- An upload that stopped half way leaves the firmware waiting for the rest, and it would take the next
  picture upload for it. "dcldfinish" ends that (`end_upload`), and `send_update` sends it when it fails.
"""
from __future__ import annotations

import hashlib
import os
import struct
import time
from pathlib import Path

from . import device, firmware_patch
from .config import config_dir

STOCK_NAME = "MYSTIQUE_2.70.bin"
STOCK_SHA256 = firmware_patch.STOCK_SHA256
PATCH_VERSION = firmware_patch.VERSION       # of the patch this deephack carries
IN_APP = Path("resources") / "app.asar.unpacked" / "resources" / "fw" / STOCK_NAME      # inside the DeepCool app's folder
APP_BASE = 0xE000                            # where the image runs
LIMIT = 2 * 1024 * 1024                      # the firmware's staging buffer; it refuses 2 MiB and more
RAM = range(0x2000_0000, 0x2001_0001)        # where a stack pointer can be
KNOWN = {0x0246: "2.70", 0x0247: "2.71"}     # bcdDevice of the firmwares this is for (2.71: see above)
FINISH = b"dcldfinish".ljust(55, b"\0")
OFF_BUS, PATIENCE = 60.0, 300.0              # seconds to wait for the cooler to leave the bus, and to come back


def version(bcd: int) -> str:
    """bcdDevice as the version DeepCool means: the low byte is the minor version as a number (0x0246: 2.70)."""
    return f"{bcd >> 8}.{bcd & 0xFF:02d}"


# ---- DeepCool's image
def kept_stock() -> Path:
    """Where deephack keeps its copy of DeepCool's image once it has seen it."""
    return config_dir() / "firmware" / STOCK_NAME


def _app_folders() -> list[Path]:
    """Where the DeepCool app may be installed: what Windows knows about it, then the usual places."""
    folders: list[Path] = []
    try:
        import winreg

        for root, flags in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                            (winreg.HKEY_CURRENT_USER, 0)):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | flags) as apps:
                    for i in range(winreg.QueryInfoKey(apps)[0]):
                        try:
                            with winreg.OpenKey(apps, winreg.EnumKey(apps, i)) as app:
                                if winreg.QueryValueEx(app, "DisplayName")[0] != "DeepCool":
                                    continue
                                icon = winreg.QueryValueEx(app, "DisplayIcon")[0]       # "C:\DeepCool\DeepCool.exe,0"
                                folders.append(Path(icon.rsplit(",", 1)[0].strip('"')).parent)
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass
    for base in ("C:/", os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "") + "/Programs"):
        if base:
            folders.append(Path(base) / "DeepCool")
    return folders


def is_stock(data: bytes) -> bool:
    return hashlib.sha256(data).hexdigest() == STOCK_SHA256


def read_stock(path: Path | str | None = None) -> bytes:
    """DeepCool's v2.70 image: from `path`, or from deephack's own copy, or from the DeepCool app. Checked by
    its hash, and kept. Raises ValueError with something to tell the user when it is not to be had."""
    if path is not None:
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            raise ValueError(f"Could not read {path}: {e.strerror or e}.") from e
        if not is_stock(data):
            raise ValueError(f"{Path(path).name} is not DeepCool's {STOCK_NAME} (the only firmware this works with).")
    else:
        for candidate in [kept_stock()] + [folder / IN_APP for folder in _app_folders()]:
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            if is_stock(data):
                break
        else:
            raise ValueError(f"DeepCool's firmware file ({STOCK_NAME}) was not found. It comes with the DeepCool app: "
                             "install that, or pick the file yourself.")
    try:
        kept_stock().parent.mkdir(parents=True, exist_ok=True)
        if not kept_stock().exists() or not is_stock(kept_stock().read_bytes()):
            kept_stock().write_bytes(data)
    except OSError:
        pass                                 # no copy, then: it only matters once the DeepCool app is gone
    return data


def have_stock() -> bool:
    try:
        read_stock()
    except ValueError:
        return False
    return True


def direct_image(stock: bytes) -> bytes:
    """DeepCool's image with the direct drawing patch. Refuses any other image, and a result that is not
    byte for byte what the patch's source (tools/fw/patch.py) builds."""
    if not is_stock(stock):
        raise ValueError(f"not DeepCool's {STOCK_NAME}: every address in the patch would be wrong")
    image = bytearray(stock + firmware_patch.CODE)
    for offset, jump in firmware_patch.JUMPS.items():
        image[offset:offset + 4] = bytes.fromhex(jump)
    if hashlib.sha256(image).hexdigest() != firmware_patch.SHA256:
        raise ValueError("the patched image is not what it should be: deephack's files are damaged")
    return bytes(image)


def image_for(which: str, stock: bytes) -> bytes:
    if which not in ("direct", "stock"):
        raise ValueError(f"unknown firmware {which!r}")
    return direct_image(stock) if which == "direct" else stock


# ---- sending
def check_image(image: bytes) -> None:
    """Refuse what cannot be a firmware for this chip, or would not arrive whole."""
    if not 0x10000 < len(image) < LIMIT:
        raise ValueError(f"{len(image)} bytes is not a plausible firmware size")
    stack, reset = struct.unpack_from("<II", image, 0)
    if stack not in RAM or not reset & 1 or not APP_BASE <= reset < APP_BASE + len(image):
        raise ValueError(f"no vector table at the start (stack {stack:#x}, reset {reset:#x})")
    eaten = [at for at in range(0, len(image), 64) if image[at:at + 2] == b"\xaa\x2e"]
    if eaten:
        raise ValueError(f"the packet at {eaten[0]:#x} starts AA 2E: the firmware would take it for a command")


def update_header(image: bytes) -> bytes:
    """The upload header for a firmware update: type 0, size, sum16. The firmware reads nothing else of it."""
    h = bytearray(64)
    h[0:4] = b"DCLd"
    h[4] = device.FILE_FIRMWARE
    struct.pack_into("<II", h, 5, len(image), device.sum16(image))
    h[20:52] = b"0" * 32
    struct.pack_into("<H", h, 62, device.sum16(h[:62]))
    return bytes(h)


def finish_is_safe(image: bytes, got: int) -> bool:
    """Whether "dcldfinish" may be sent after `got` bytes of the image. The firmware counts that packet as
    data first: with fewer than its 55 bytes missing it would complete the file, and if the 16 bit sum
    then happened to match, a file with a wrong end would be flashed."""
    left = len(image) - got
    return left > len(FINISH) or left <= 0 or device.sum16(image[got:]) != device.sum16(FINISH[:left])


def end_upload(driver, image: bytes, sent: int) -> bool:
    """End a firmware upload that stopped after `sent` bytes (the packet after them may have arrived or
    not), so that the firmware does not take the next upload for the rest of it. False when that could
    not be done: nothing must be uploaded then until the cooler has been off and on again."""
    if not all(finish_is_safe(image, got) for got in (sent, min(sent + 64, len(image)))):
        return False
    try:
        driver.dev.write(1, FINISH, 2000)
    except Exception:                        # noqa: BLE001  (the cooler is gone: it has nothing half done then)
        return False
    return True


class SendFailed(RuntimeError):
    """A firmware update did not go out whole. `ended`: the firmware was told that the upload is over."""

    def __init__(self, message: str, sent: int, ended: bool):
        super().__init__(message)
        self.sent, self.ended = sent, ended


def send_update(driver, image: bytes, progress=None) -> None:
    """Hand the image to the running firmware. After the last packet it stores the file and restarts.

    `progress(bytes sent)` is called now and then; it may raise to stop the upload. Whatever stops it, the
    firmware is told that the upload is over, and SendFailed is raised: nothing has been flashed then."""
    check_image(image)
    # The firmware takes an upload while it changes from the upload screen to its mode's screen: with no
    # mode set (a fresh emulated cooler) it never does. Image mode is what every upload here ran under.
    driver.set_mode(device.MODE_MOTION)
    driver.command(device.CMD_START_DOWNLOAD)
    sent = 0
    try:
        driver.dev.write(1, update_header(image), 5000)
        for at in range(0, len(image), 64):
            if progress and at % 0x1000 == 0 and len(image) - at > 0x1000:      # not at the very end: see finish_is_safe
                progress(at)
            driver.dev.write(1, image[at:at + 64], 5000)
            sent = min(at + 64, len(image))
    except BaseException as e:
        ended = end_upload(driver, image, sent)
        raise SendFailed(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__, sent, ended) from e


def on_bus() -> int | None:
    """The cooler's bcdDevice if it is on the bus, without claiming it."""
    import libusb_package
    import usb.util

    dev = libusb_package.find(idVendor=device.VID, idProduct=device.PID)
    if dev is None:
        return None
    bcd = dev.bcdDevice
    usb.util.dispose_resources(dev)
    return bcd


def wait_for_restart(look=None, report=None, off_bus: float = OFF_BUS, patience: float = PATIENCE,
                     clock=None) -> tuple[str, int | None]:
    """Watch the cooler leave the bus and come back. ("back", its bcdDevice), ("stayed", bcdDevice) when it
    never left (it did not restart: nothing was flashed), or ("gone", None) when it left and has not come
    back. `report(seconds, state)` is called at each look, state "waiting", "off" or "back"; it may raise
    to stop the waiting."""
    look, clock = look or on_bus, clock or time.monotonic
    started, gone = clock(), False
    while clock() - started < (patience if gone else off_bus):
        time.sleep(0.5)
        bcd = look()
        if bcd is None:
            gone = True
        elif gone:
            if report:
                report(clock() - started, "back")
            return "back", bcd
        if report:
            report(clock() - started, "off" if gone else "waiting")
    return ("gone", None) if gone else ("stayed", look())
