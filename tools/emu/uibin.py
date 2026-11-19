"""Write a `ui.bin` the MYSTIQUE's firmware accepts: the UI data file of Synwit's UI framework, format 0.7.

The real file is made by Synwit's UI tool and exists only on the cooler's serial flash. This is the
format as the firmware's loader reads it (worked out from the loader code and by trying files in the
emulator; see docs/firmware-analysis.md):

    header, 0x34 bytes
        0x00  "SynwitUI\\0"
        0x10  format major (0)          0x14  format minor (7)
        0x18  flag byte                 0x1C  offset of the screen table
        0x20  offset of the widgets     0x24  offset of the resource table
        0x28, 0x2C, 0x30  offsets of three resource data sections
    screen table      u32 count, then count x u32 screen id
    widgets           12 bytes (u16 at +0x0A: the screen to start with), u32 count,
                      count x {u32 screen id, u32 offset of the screen's widget}
    a widget          a blob: u32 size (0x5C at least), u8 type, 3 unused, u32 id, the rest per type;
                      then u32 string count, u32 (unknown), the strings as {u32 length, bytes},
                      then u32 child count and that many u32 offsets of child widgets
    resource table    "RESLD\\0", u32 0, u32 1, u32 count, count x 12 bytes {screen id, offset, ?};
                      at each offset three lists, each u32 count + count x u32

Widget types (the firmware's class table): 0 widget, 1 screen, 2 button, 3 label, 4 checkbox, 5 image,
6 bar, 7 slider, 8 switch, 9 image_btn, 10 list, 11 artchar, 12 roller, 13 dropdown, 14 arc, 15 touch,
16 levelimg, 17 chart, 18 textarea, 19 imgroller, 20 clock, 21 avi.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = b"SynwitUI\0"
FORMAT = (0, 7)
HEADER_SIZE = 0x34
WIDGET_SIZE = 0x5C
SIZES = {"screen": 0x60, "label": 0x130, "image": 0x68, "levelimg": 0x98}   # the classes this firmware registers
TYPES = ("widget", "screen", "button", "label", "checkbox", "image", "bar", "slider", "switch", "image_btn", "list",
         "artchar", "roller", "dropdown", "arc", "touch", "levelimg", "chart", "textarea", "imgroller", "clock", "avi")


# The screens the firmware has code for: it registers three handlers for each of these ids
# (FUN_0002e340: on entering, on leaving, on the screen's timer) and switches between them by id.
SCREENS = (*range(0x01, 0x11), *range(0x17, 0x20), *range(0x29, 0x2E), 0x90E, 0x917)
BOOT_SCREEN = 0x01          # the logo, for 4 s; nothing else happens before its timer has run
UPLOAD_SCREEN = 0x2C        # shown while a file comes in; the file is taken by its "leaving" handler
IMAGE_SCREEN = 0x90E        # stored pictures and animations (mode 2)


@dataclass
class Widget:
    type: str
    id: int = 0                        # what the firmware finds it by; a screen's is its screen id
    body: bytes = b""                  # the structure after its first 12 bytes, zero padded to its size
    strings: list[bytes] = field(default_factory=list)
    children: list["Widget"] = field(default_factory=list)
    unknown: int = 0

    def blob(self) -> bytes:
        size = max(SIZES.get(self.type, WIDGET_SIZE), 12 + len(self.body))
        return struct.pack("<IB3xI", size, TYPES.index(self.type), self.id) + self.body.ljust(size - 12, b"\0")


def label(id: int, text: str = "") -> Widget:
    """A label as the firmware's own "new label" code fills one in (0x10E40): three style blocks with
    no font and no colour chosen, and one string, the text."""
    blob = bytearray(SIZES["label"])
    struct.pack_into("<H", blob, 0xE0, 0x501)
    for style in (0xE8, 0x100, 0x118):
        struct.pack_into("<iiIHIH", blob, style, -1, -1, 0xFF000000, 0x200, 0, 0x1FF)
    return Widget("label", id, bytes(blob[12:]), [text.encode() + bytes(1)] if text else [])


def _place(widget: Widget, out: bytearray) -> int:
    """Append a widget and everything below it; returns its offset."""
    children = [_place(child, out) for child in widget.children]       # children first: their offsets are needed
    offset = len(out)
    out += widget.blob()
    out += struct.pack("<II", len(widget.strings), widget.unknown)
    for s in widget.strings:
        out += struct.pack("<I", len(s)) + s
    out += struct.pack("<I", len(children)) + b"".join(struct.pack("<I", c) for c in children)
    return offset


def build(screens: dict[int, Widget], start: int | None = None, flag: int = 0) -> bytes:
    """A ui.bin with these screens (id -> its screen widget) and no resources."""
    start = next(iter(screens)) if start is None else start
    out = bytearray(HEADER_SIZE)
    screen_table = len(out)
    out += struct.pack("<I", len(screens)) + b"".join(struct.pack("<I", sid) for sid in screens)
    for sid, widget in screens.items():
        widget.id = widget.id or sid
    offsets = {sid: _place(widget, out) for sid, widget in screens.items()}
    widgets = len(out)
    out += struct.pack("<10xH", start) + struct.pack("<I", len(screens))
    out += b"".join(struct.pack("<II", sid, offsets[sid]) for sid in screens)
    resources = len(out)
    lists = resources + 20 + 12 * len(screens)
    out += b"RESLD\0\0\0" + struct.pack("<III", 0, 1, len(screens))
    out += b"".join(struct.pack("<III", sid, lists, 0) for sid in screens)
    out += bytes(12)                                # three empty lists, shared by every screen
    empty = len(out)
    out += bytes(16)
    out[0:len(MAGIC)] = MAGIC
    struct.pack_into("<IIB3xIIIIII", out, 0x10, FORMAT[0], FORMAT[1], flag, screen_table, widgets, resources, empty, empty, empty)
    return bytes(out)


def stock() -> bytes:
    """Every screen the firmware knows, with the widgets its code cannot do without (it looks them up
    by id and uses them unchecked). Everything else is left out, so the screens are blank."""
    screens = {sid: Widget("screen") for sid in SCREENS}
    screens[UPLOAD_SCREEN].children.append(label(0x4A2))            # moved about when the screen is entered
    # the image screen sizes and styles these three; what kind each really is, is a guess
    screens[IMAGE_SCREEN].children += [Widget("image", 0x90F), Widget("image", 0x915), label(0x916)]
    return build(screens, start=BOOT_SCREEN)
