"""Send a firmware image to the MYSTIQUE through its own update channel. THIS CAN KILL THE COOLER'S SCREEN.

    python tools/fw/flash.py stock   --i-accept-the-risk     DeepCool's v2.70, as the DeepCool app ships it
    python tools/fw/flash.py patched --i-accept-the-risk     v2.70 with the direct drawing patch (patch.py)
    python tools/fw/flash.py check                            only say what the cooler runs now

What happens: an upload like a picture's, but with file type 0. The running firmware stores the data as
FW_update.bin on its SD card and restarts the chip (through its watchdog); the bootloader, which nobody
here has seen, then writes the file over the firmware. Nothing can be done from USB while that runs,
and nothing from USB afterwards if the new firmware does not start: there is no way back short of a
debug probe on the board. Do not unplug or switch off while it runs.

Done once for real: "patched" on 19 Nov 2026, to a cooler that ran DeepCool's v2.70. It was off the bus
for 12 seconds and came back drawing (docs/firmware-analysis.md, "On the cooler").

"stock" is also the way back from "patched", for as long as the firmware on the cooler still takes
uploads. There is no image of v2.71, which some coolers come with (docs/firmware-analysis.md, "Which
firmware"): for those, flashing either image is a downgrade that cannot be undone.

Quit the DeepCool app and deephack first: only one program can have the cooler.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import patch  # noqa: E402

from deephack import device  # noqa: E402

LIMIT = 2 * 1024 * 1024                     # the firmware's staging buffer; it refuses 2 MiB and more
RAM = range(0x2000_0000, 0x2001_0001)       # where a stack pointer can be


def version(bcd: int) -> str:
    """bcdDevice as the version DeepCool means: the low byte is the minor version as a number (0x0246: 2.70)."""
    return f"{bcd >> 8}.{bcd & 0xFF:02d}"


def check_image(image: bytes) -> None:
    """Refuse what cannot be a firmware for this chip, or would not arrive whole."""
    if not 0x10000 < len(image) < LIMIT:
        raise ValueError(f"{len(image)} bytes is not a plausible firmware size")
    stack, reset = struct.unpack_from("<II", image, 0)
    if stack not in RAM or not reset & 1 or not patch.APP <= reset < patch.APP + len(image):
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


def send_update(driver, image: bytes, progress=None) -> None:
    """Hand the image to the running firmware. After the last packet it stores the file and restarts."""
    check_image(image)
    # The firmware takes an upload while it changes from the upload screen to its mode's screen: with no
    # mode set (a fresh emulated cooler) it never does. Image mode is what every upload here ran under.
    driver.set_mode(device.MODE_MOTION)
    driver.command(device.CMD_START_DOWNLOAD)
    driver.dev.write(1, update_header(image), 5000)
    for at in range(0, len(image), 64):
        driver.dev.write(1, image[at:at + 64], 5000)
        if progress and at % 0x4000 == 0:
            progress(at)


def find():
    import libusb_package

    return libusb_package.find(idVendor=device.VID, idProduct=device.PID)


def wait_for_restart(before: int, patience: float = 300.0) -> int | None:
    """Watch the cooler leave the bus and come back; its bcdDevice then, or None if it does not come back."""
    import usb.util

    started, gone = time.monotonic(), None
    while time.monotonic() - started < patience:
        time.sleep(0.5)
        dev = find()
        elapsed = time.monotonic() - started
        if dev is None:
            if gone is None:
                gone = elapsed
                print(f"  {elapsed:5.1f} s: the cooler left the bus")
            continue
        bcd = dev.bcdDevice
        usb.util.dispose_resources(dev)
        if gone is not None:
            print(f"  {elapsed:5.1f} s: the cooler is back, firmware {version(bcd)}")
            return bcd
        if bcd != before:                    # restarted faster than we looked
            print(f"  {elapsed:5.1f} s: the cooler answers as firmware {version(bcd)}")
            return bcd
        if elapsed > 60 and gone is None:
            print(f"  {elapsed:5.1f} s: the cooler never left the bus: it did not restart")
            return bcd
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=("stock", "patched", "check"))
    ap.add_argument("--stock", default=str(patch.STOCK), help="DeepCool's MYSTIQUE_2.70.bin")
    ap.add_argument("--i-accept-the-risk", action="store_true", help="really flash: the cooler may not survive it")
    a = ap.parse_args()

    dev = find()
    if dev is None:
        sys.exit("MYSTIQUE (3633:0009) not found on USB")
    before = dev.bcdDevice
    print(f"the cooler runs firmware {version(before)} (bcdDevice {before:#06x})")
    if a.what == "check":
        with device.Mystique() as m:
            print("it answers: uuid", m.uuid(), "| direct drawing:", "yes" if m.can_draw() else "no")
        return 0

    stock = Path(a.stock).read_bytes()
    if hashlib.sha256(stock).hexdigest() != patch.STOCK_SHA256:
        sys.exit(f"{a.stock} is not the MYSTIQUE_2.70.bin this was made for")
    image = patch.build(stock) if a.what == "patched" else stock
    check_image(image)
    print(f"image: {a.what}, {len(image)} bytes, sha256 {hashlib.sha256(image).hexdigest()[:16]}...")
    if not a.i_accept_the_risk:
        sys.exit("not flashing without --i-accept-the-risk (read this file's first lines)")

    with device.Mystique(timeout_ms=5000) as m:
        print("sending (do not unplug, do not switch off) ...")
        try:
            send_update(m, image, progress=lambda at: print(f"  {at * 100 // len(image):3d} %", end="\r", flush=True))
        except Exception as e:                                      # noqa: BLE001
            sys.exit(f"\nsending failed before the end ({e}): the firmware has not been given a whole file, "
                     "so nothing is flashed. It may sit in its upload screen for a while.")
        print("  100 %: sent. The firmware stores it and restarts into its bootloader.")
    after = wait_for_restart(before)
    if after is None:
        print("The cooler has not come back in five minutes. Leave it powered a while longer, then power-cycle the PC.")
        return 1
    time.sleep(3.0)
    try:
        with device.Mystique() as m:
            print("it answers: uuid", m.uuid(), "| direct drawing:", "yes" if m.can_draw() else "no")
    except Exception as e:                                          # noqa: BLE001
        print(f"it is on the bus but does not answer yet: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
