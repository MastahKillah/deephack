"""The SWM341's hardware JPEG decoder, as the MYSTIQUE's firmware drives it.

The firmware parses a JPEG's header itself and programs the decoder: CFG (formats), IMGSIZ (width - 1,
height - 1), CSBASE and CODLEN (where the entropy-coded data is in RAM and how long), RGBASE (where
the pixels go: straight into an LCD frame buffer), IMGSTR (line length in 4-byte words), then the
quantisation and Huffman tables at 0x100 to 0x3FF, and CR = START | LASTBUF. It then polls SR.BUSY.

This model does not decode from those tables. The firmware always has the whole file in RAM, so the
header is right in front of the data CSBASE points at: the model finds the start-of-image marker
before it and hands header and data to Pillow. What the firmware wrote to the table registers is kept
(`reg`) but not used; a patch that programmed the tables wrongly would go unnoticed here.
"""
from __future__ import annotations

import io
import struct

CFG, CR, IR, SR, IMGSIZ, IMGSTR, CSBASE, RGBASE, CODLEN = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x30
START = 1
LOOK_BACK = 0x10000                # how far before the data the header may start


def rgb565(image) -> bytes:
    """A Pillow image as little-endian RGB565, the LCD layer's format."""
    out = bytearray(image.width * image.height * 2)
    raw = image.convert("RGB").tobytes()
    out[0::2] = bytes(((g << 3) & 0xE0) | (b >> 3) for g, b in zip(raw[1::3], raw[2::3]))
    out[1::2] = bytes((r & 0xF8) | (g >> 5) for r, g in zip(raw[0::3], raw[1::3]))
    return bytes(out)


class JpegDecoder:
    size = 0x800

    def __init__(self, base: int, memory, regions=((0x8000_0000, 0x0100_0000), (0x2000_0000, 0x0010_0000))):
        self.base, self.memory, self.regions = base, memory, regions
        self.reg = bytearray(self.size)
        self.decoded: list[tuple[int, int, int]] = []        # (output address, width, height) of every picture
        self.errors: list[str] = []

    def _word(self, offset: int) -> int:
        return struct.unpack_from("<I", self.reg, offset)[0]

    def read(self, offset: int) -> int:
        return 0 if offset == SR else self._word(offset)     # never busy: a decode is over at once

    def write(self, offset: int, value: int, size: int) -> None:
        self.reg[offset:offset + size] = (value & (1 << 8 * size) - 1).to_bytes(size, "little")
        if offset == CR and value & START:
            try:
                self._decode()
            except Exception as e:                           # a bad file must not take the emulator down
                self.errors.append(f"{type(e).__name__}: {e}")

    def _decode(self) -> None:
        from PIL import Image

        size, data_at, length, out = self._word(IMGSIZ), self._word(CSBASE), self._word(CODLEN), self._word(RGBASE)
        width, height, stride = (size & 0xFFFF) + 1, (size >> 16) + 1, self._word(IMGSTR) * 4
        start = next((base for base, span in self.regions if base <= data_at < base + span), None)
        if start is None:
            raise ValueError(f"the JPEG data is not in RAM ({data_at:#x})")
        before = self.memory(max(start, data_at - LOOK_BACK), data_at - max(start, data_at - LOOK_BACK))
        header = before.rfind(b"\xff\xd8\xff")
        if header < 0:
            raise ValueError("no JPEG header in front of the data")
        data = before[header:] + self.memory(data_at, length)
        image = Image.open(io.BytesIO(data if data.endswith(b"\xff\xd9") else data + b"\xff\xd9"))
        image.load()
        if image.size != (width, height):
            raise ValueError(f"the firmware said {width}x{height}, the file is {image.size[0]}x{image.size[1]}")
        pixels = rgb565(image)
        if stride == width * 2:
            self.memory(out, pixels)
        else:
            for row in range(height):
                self.memory(out + row * stride, pixels[row * width * 2:(row + 1) * width * 2])
        self.decoded.append((out, width, height))
