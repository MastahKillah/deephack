"""Read the cooler's memory through the patch's command 21 (version 4) and write it to a file.

    python tools/fw/dump.py bootloader                 the 56 KiB below the firmware, to captures/bootloader.bin
    python tools/fw/dump.py range 0x7C000 0x1000       any stretch of its flash or RAM, to captures/

The bootloader is the part of the cooler nobody here has seen: it is not in the update file, and it is
what writes a new firmware over the old one. Reading it changes nothing on the cooler. Everything is read
twice and the two readings are compared.

Only the chip's flash (0 to 0x80000), its RAM and the SDRAM can be asked for (deephack.device.READABLE):
an address that is not memory stops the cooler until it is switched off and on. Whether the flash
really goes up to 0x80000 is something to find out from the bootloader first, not by reading there.

deephack must not run meanwhile: the cooler takes one program at a time.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deephack import device  # noqa: E402

APP = 0xE000                                # where the firmware begins: below it is the bootloader
OUT = Path(__file__).resolve().parents[2] / "captures"


def read(dev: device.Mystique, address: int, length: int, progress=None) -> bytes:
    """`length` bytes from `address`, read twice. Raises RuntimeError when the two readings differ."""
    readings = []
    for _ in range(2):
        data = b""
        while len(data) < length:
            data += dev.peek(address + len(data), min(39 * 64, length - len(data)))
            if progress:
                progress(len(data) * (len(readings) + 1) // 2 + (length // 2 if readings else 0))
        readings.append(data)
    if readings[0] != readings[1]:
        raise RuntimeError("two readings of the same memory differ")
    return readings[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=("bootloader", "range"))
    ap.add_argument("address", nargs="?", type=lambda s: int(s, 0))
    ap.add_argument("length", nargs="?", type=lambda s: int(s, 0))
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    address, length = (0, APP) if a.what == "bootloader" else (a.address, a.length)
    if address is None or length is None:
        ap.error("range needs an address and a length")
    out = Path(a.output) if a.output else OUT / ("bootloader.bin" if a.what == "bootloader" else f"memory_{address:08x}_{length:x}.bin")
    with device.Mystique() as dev:
        version = dev.draw_version()
        if version < device.PEEK_VERSION:
            sys.exit(f"the cooler's patch is version {version or 'none'}: reading memory needs {device.PEEK_VERSION}")
        began = time.perf_counter()
        data = read(dev, address, length, lambda done: print(f"  {done * 100 // length:3d} %", end="\r", flush=True))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"{out}: {len(data)} bytes from {address:#x}, read twice in {time.perf_counter() - began:.1f} s, "
          f"sha256 {hashlib.sha256(data).hexdigest()[:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
