"""What is on the cooler's screen, read from the cooler itself: its frame buffer, through the patch's
command 21 (version 4 or later).

    python tools/screen.py [out.png]          default: captures/screen.png

The LCD shows one of two frame buffers of 480 x 640 pixels, 16 bits each, in the SDRAM; LVGL draws into
the other and then copies what changed, so outside a redraw the two hold the same picture. Reading one
takes about six seconds (39 bytes a question), and the picture may change meanwhile: `read` can be told
to read a few lines only, and `settled` reads until two readings agree.

This is the exact picture, pixel for pixel, where a webcam gives a blurred one: good for checking what
the firmware made of a JPEG. deephack must not run meanwhile (the cooler takes one program at a time),
unless it is this program that draws.
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from deephack import device  # noqa: E402

DISP_BUF = 0x200005A4                       # LVGL's lv_disp_buf_t: buf1, buf2, buf_act, size in pixels
WIDTH, HEIGHT = device.WIDTH, device.HEIGHT
LINE = WIDTH * 2


def buffers(dev: device.Mystique) -> tuple[int, int]:
    """Where the two frame buffers are: (the one LVGL does not draw into now, the one it does). They come
    from the firmware's heap, and not at the same place on the cooler as in the emulator (0x80006A00 and
    0x8009CA20 against 0x800061A0 and 0x8009C1C0): so they are asked for, from LVGL's own record."""
    first, second, active, size = struct.unpack("<4I", dev.peek(DISP_BUF, 16))
    if size != WIDTH * HEIGHT or active not in (first, second) or not all(0x8000_0000 <= b < 0x8080_0000 for b in (first, second)):
        raise RuntimeError(f"no frame buffers where LVGL keeps them ({first:#x}, {second:#x}, {active:#x}, {size:#x})")
    return (second, first) if active == first else (first, second)


def read(dev: device.Mystique, lines=None) -> bytes:
    """The bytes of the frame buffer that is on the glass (little-endian RGB565) for `lines` (a range or
    list of line numbers; all of them when None), one line after the other.

    Line by line, and each only from the buffer LVGL was not drawing into before and after it was read:
    the firmware redraws its picture ten times a second into the hidden buffer and then swaps the two, and
    with a part of the picture on a page that redraw is the page first and the part after, so the hidden
    buffer shows the page where the part belongs for a few milliseconds each time. The one on the glass is
    never written to."""
    out = bytearray()
    for y in range(HEIGHT) if lines is None else lines:
        for _ in range(50):
            shown, drawing = buffers(dev)
            line = dev.peek(shown + y * LINE, LINE)
            if buffers(dev) == (shown, drawing):
                break
        else:
            raise RuntimeError("the cooler swaps its frame buffers faster than a line can be read")
        out += line
    return bytes(out)


def settled(dev: device.Mystique, lines=None, tries: int = 6, wait: float = 0.05) -> bytes:
    """`read`, repeated until two readings in a row are the same: what is on the glass once a picture that
    was just sent has been drawn. Raises RuntimeError when the picture never stands still."""
    last = read(dev, lines)
    for _ in range(tries):
        time.sleep(wait)
        now = read(dev, lines)
        if now == last:
            return now
        last = now
    raise RuntimeError("the cooler's picture did not stand still")


def picture(raw: bytes, height: int | None = None) -> Image.Image:
    """Frame buffer bytes as a picture (RGB565 to RGB, the low bits filled from the high ones)."""
    height = height or len(raw) // LINE
    out = bytearray(WIDTH * height * 3)
    for i in range(WIDTH * height):
        v = raw[2 * i] | raw[2 * i + 1] << 8
        r, g, b = v >> 11, v >> 5 & 0x3F, v & 0x1F
        out[3 * i:3 * i + 3] = bytes((r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2))
    return Image.frombytes("RGB", (WIDTH, height), bytes(out))


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "captures" / "screen.png"
    with device.Mystique() as dev:
        version = dev.draw_version()
        if version < device.PEEK_VERSION:
            sys.exit(f"the cooler's patch is version {version or 'none'}: reading its screen needs {device.PEEK_VERSION}")
        began = time.perf_counter()
        raw = read(dev)
    out.parent.mkdir(parents=True, exist_ok=True)
    picture(raw).save(out)
    print(f"{out}: the cooler's screen, read in {time.perf_counter() - began:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
