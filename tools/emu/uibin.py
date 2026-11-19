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
    a widget          a blob: u32 size (0x5C at least), u8 type, 3 unused, u32 id, then fields every
                      kind has (see Widget) and the kind's own;
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
    """One widget as it is stored. What every kind has (the firmware's own names for the fields, from
    its property setter at 0x1511C): name at 0x10, visual 0x45, enable 0x46, left 0x48, top 0x4A,
    width 0x4C, height 0x4E. A widget that is not "visual" is made hidden, a screen included."""
    type: str
    id: int = 0                                    # what the firmware finds it by; a screen's is its screen id
    box: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, width, height
    visible: bool = True
    enabled: bool = True
    name: str = ""
    fields: dict[int, bytes] = field(default_factory=dict)      # what the kind adds: offset -> bytes
    strings: list[bytes] = field(default_factory=list)
    children: list["Widget"] = field(default_factory=list)
    unknown: int = 0

    def blob(self) -> bytes:
        blob = bytearray(SIZES.get(self.type, WIDGET_SIZE))
        struct.pack_into("<IB3xI", blob, 0, len(blob), TYPES.index(self.type), self.id)
        name = self.name.encode()[:31]
        blob[0x10:0x10 + len(name)] = name
        blob[0x45], blob[0x46] = self.visible, self.enabled
        struct.pack_into("<4h", blob, 0x48, *self.box)
        for offset, raw in self.fields.items():
            blob[offset:offset + len(raw)] = raw
        return bytes(blob)


def screen(colour: tuple[int, int, int] = (0, 0, 0), children: list[Widget] | None = None) -> Widget:
    """A screen: the whole panel, with a background colour (0x5C, red in the low byte)."""
    r, g, b = colour
    return Widget("screen", box=(0, 0, 480, 640), fields={0x5C: struct.pack("<I", r | g << 8 | b << 16)},
                  children=children or [])


def image(id: int, box: tuple[int, int, int, int], resource: int = 0, opacity: int = 255, **more) -> Widget:
    """A picture: opacity at 0x60, the id of its picture in the resource table at 0x64 (0: none; the
    firmware gives the image screen's pictures their content itself)."""
    return Widget("image", id, box, fields={0x60: bytes([opacity, 0]), 0x64: struct.pack("<I", resource)}, **more)


def label(id: int, text: str = "", box: tuple[int, int, int, int] = (0, 0, 100, 20), **more) -> Widget:
    """A label as the firmware's own "new label" code fills one in (0x10E40): three style blocks with
    no font and no colour chosen, and one string, the text."""
    fields = {0xE0: struct.pack("<H", 0x501)}
    for style in (0xE8, 0x100, 0x118):
        fields[style] = struct.pack("<iiIHIH", -1, -1, 0xFF000000, 0x200, 0, 0x1FF)
    return Widget("label", id, box, fields=fields, strings=[text.encode() + bytes(1)] if text else [], **more)


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
    by id and uses them unchecked). Everything else is left out, so most screens are blank."""
    screens = {sid: screen() for sid in SCREENS}
    # the logo screen: one picture per rotation of the panel (the firmware shows the one that fits and
    # centres all four) and two lines of text it sets the font of
    screens[BOOT_SCREEN].children += [image(wid, (140, 220, 200, 200)) for wid in (0x4A4, 0x912, 0x913, 0x914)]
    screens[BOOT_SCREEN].children += [label(0x918, "deephack bench", (140, 500, 200, 24)), label(0x919, "", (140, 530, 200, 24))]
    screens[UPLOAD_SCREEN].children.append(label(0x4A2, "uploading", (160, 299, 160, 40)))     # the firmware places it
    # the image screen: the stored picture, and two pictures shown when nothing is stored (the
    # firmware places and turns those two to match the panel's rotation)
    screens[IMAGE_SCREEN].children += [image(0x90F, (0, 0, 480, 640)), image(0x915, (165, 159, 150, 200)),
                                       image(0x916, (77, 503, 325, 28))]
    return build(screens, start=BOOT_SCREEN)
