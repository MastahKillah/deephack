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

The sending itself is deephack.firmware, which is also what the app's "Firmware" section uses; this
tool is for trying an image that the app does not carry yet ("patched" is assembled from patch.py's
source here, the app sends the table made from it).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import patch  # noqa: E402

from deephack import device, firmware  # noqa: E402
from deephack.firmware import check_image, send_update, update_header, version  # noqa: E402,F401


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=("stock", "patched", "check"))
    ap.add_argument("--stock", default=str(patch.STOCK), help="DeepCool's MYSTIQUE_2.70.bin")
    ap.add_argument("--i-accept-the-risk", action="store_true", help="really flash: the cooler may not survive it")
    a = ap.parse_args()

    before = firmware.on_bus()
    if before is None:
        sys.exit("MYSTIQUE (3633:0009) not found on USB")
    print(f"the cooler runs firmware {version(before)} (bcdDevice {before:#06x})")
    if a.what == "check":
        with device.Mystique() as m:
            print("it answers: uuid", m.uuid(), "| direct drawing patch:", m.draw_version() or "none")
        return 0

    stock = Path(a.stock).read_bytes()
    if not firmware.is_stock(stock):
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
        except firmware.NoRoom as e:
            sys.exit(f"\nnot sent: {e}. The firmware would have thrown the file away. A patch before 15 holds 384 KB from "
                     "the first drawn picture until the cooler loses power: switch the PC off and on, and flash before "
                     "anything is drawn; or have a small stored picture on show.")
        except firmware.SendFailed as e:
            sys.exit(f"\nsending failed after {e.sent} bytes ({e}): the firmware has not been given a whole file, so nothing "
                     "is flashed. " + ("It was told that the upload is over." if e.ended else
                                       "It could NOT be told that the upload is over: send it nothing until it was off and on."))
        print("  100 %: sent. The firmware stores it and restarts into its bootloader.")
    seen = {"state": "waiting"}

    def report(seconds: float, state: str) -> None:
        if state != seen["state"]:
            seen["state"] = state
            print(f"  {seconds:5.1f} s: the cooler {'left the bus' if state == 'off' else 'is back'}")

    outcome, after = firmware.wait_for_restart(report=report)
    if outcome == "gone":
        print("The cooler has not come back in five minutes. Leave it powered a while longer, then power-cycle the PC.")
        return 1
    if outcome == "stayed":
        print("The cooler never left the bus: it did not restart.")
    time.sleep(3.0)
    try:
        with device.Mystique() as m:
            print(f"firmware {version(after)}; it answers: uuid", m.uuid(), "| direct drawing patch:", m.draw_version() or "none")
    except Exception as e:                                          # noqa: BLE001
        print(f"it is on the bus but does not answer yet: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
