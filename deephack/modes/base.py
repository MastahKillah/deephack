"""Base class for display modes.

A mode produces 480x640 images. The engine calls `frame()` about once a second; return a new PIL image
when the display should change and None otherwise. Every image a mode returns ends up as a write to the
cooler's flash, so only return one when the content really changed. The exception is a cooler with the
direct drawing firmware: the engine sets `direct` on the mode then, and pictures cost nothing.

Modes that sometimes have something worth showing (music playing, a game running) set `claims` to a short
description and keep `active` up to date in `frame()`; the Auto mode gives them the screen while it is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from PIL import Image


@dataclass
class Animation:
    """A looping animation. Every frame is stored on the cooler's flash, so keep the frame count low. (A cooler
    with the direct drawing firmware stores none: the engine draws each frame when it is due.)"""
    frames: list[Image.Image]
    duration_ms: int               # length of one loop


@dataclass(frozen=True, eq=False)
class Part:
    """A picture of which only one rectangle changed since the last one: a video on a page that stands still.

    `page` is the picture without it, and **the same object for as long as it is the same page**; `picture`
    is what lies on it at `at` (x, y). A cooler whose direct drawing patch can do it (version 3) is sent the
    page once and after that only the pictures, which are a fraction of the bytes; the window, and every
    other cooler, get `whole`. One rectangle at a time: a picture somewhere else takes the last one's place,
    and where that was the page shows again.

    The picture's width and height must be multiples of 16 and it must lie inside the screen (`fits`): the
    cooler's JPEG decoder writes whole blocks. One that does not fit is sent whole."""
    page: Image.Image
    picture: Image.Image
    at: tuple[int, int]

    @cached_property
    def whole(self) -> Image.Image:
        out = self.page.copy()
        out.paste(self.picture, self.at)
        return out

    @property
    def fits(self) -> bool:
        (x, y), (width, height) = self.at, self.picture.size
        return (width > 0 and height > 0 and width % 16 == 0 and height % 16 == 0
                and x >= 0 and y >= 0 and x + width <= self.page.width and y + height <= self.page.height)


@dataclass
class Dashboard:
    """The cooler's own sensor screen, drawn by its firmware from values sent over USB. Nothing is stored,
    so a mode can send one every second without wearing the flash."""
    screen: str                    # "dashboard" (device.set_dashboard) or "recorder" (device.set_recorder)
    layout: tuple[int, ...]        # set_dashboard(main, style, strip), or set_recorder(frequency_only)
    values: dict[str, float]       # device.SENSOR_FIELDS
    preview: Image.Image           # deephack's approximation of the screen, for the window


@dataclass
class Flipbook:
    """A set of stills stored on the cooler once and then flipped through. Showing a stored image writes
    nothing, and the firmware keeps up with about ten a second, so this is how a mode moves in real time.
    Return one whenever `index` changes; the frames are only stored again when `key` changes."""
    key: str                       # names the set of frames
    frames: list[Image.Image]
    index: int                     # the frame to show now


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str                      # "choice" | "text" | "secret" (text shown as dots) | "folder" (text with a Browse button)
                                   # | "file" (the same, for a file) | "order" (see parse_order)
                                   # | "shortcut" (a key that works in any window, for `action`; see hotkeys.py)
                                   # | "capture" (a window or a part of the screen, chosen on it; see mirror.py)
    default: str
    choices: tuple[tuple[str, str], ...] = ()   # (value, label) for kind "choice" and "order"
    placeholder: str = ""
    hint: str = ""
    action: str = ""               # kind "shortcut": which of the mode's `actions` the key is for
    live: bool = False             # the running mode reads it at every frame: a change reaches it as it is, and the
                                   # mode is not started anew for it (the loudness of a video that is playing)


def parse_order(value: str, ids: list[str]) -> list[tuple[str, bool]]:
    """An "order" setting: ids comma separated in priority order, '-' in front of the switched off ones.

    Unknown ids are dropped and ids missing from the value are appended switched off, so modes added in a
    later version show up at the end of the list.
    """
    out, seen = [], set()
    for token in value.split(","):
        token = token.strip()
        mode_id = token.lstrip("-")
        if mode_id in ids and mode_id not in seen:
            out.append((mode_id, not token.startswith("-")))
            seen.add(mode_id)
    return out + [(mode_id, False) for mode_id in ids if mode_id not in seen]


class Mode:
    id: str = ""
    title: str = ""
    description: str = ""
    glyph: str = ""                # character from the "Segoe Fluent Icons" font
    claims: str = ""               # when the mode wants the screen in Auto ("music plays"); empty: never
    reads: tuple[str, ...] = ()    # other modes whose settings `build()` reads, reloaded when those change
    tick: float | None = None      # seconds between frame() calls when the mode needs them faster than usual
    quality: int | None = None     # JPEG quality of its drawn pictures, when the engine's own choice will not do (a
                                   # video: smaller frames, so that the cooler takes more of them a second)
    needs_direct: bool = False     # of little use on DeepCool's own firmware (a video is one picture there): the
                                   # mode's page says so on such a cooler, and points to Settings
    settings: tuple[Setting, ...] = ()
    actions: tuple[tuple[str, str], ...] = ()    # (name, label) of what the user can tell the mode to do ("next"):
                                                 # buttons on its page, and keys (settings of kind "shortcut")

    def __init__(self, values: dict | None = None):
        values = values or {}
        self.values = {s.key: values.get(s.key, s.default) for s in self.settings}
        self.status = ""                                  # one-line state shown in the UI
        self.placeholder: tuple[str, str] | None = None   # (title, subtitle) for the preview when there is no frame
        self.active = False                               # has something worth showing right now (see `claims`)
        self.direct = False                               # set by the engine: its pictures are drawn from USB, not
                                                          # stored, so a new one costs nothing and may come every tick
        self.sent_bytes = 0                               # set by the engine: how large its last drawn picture was as
                                                          # it went to the cooler (a JPEG), for a mode that minds
        self.fast = False                                 # set by the engine: the cooler's patch takes bytes at the
                                                          # bus's full speed (version 9), so a picture may be larger
        self.audible = False                              # set by the engine: this is the mode on the cooler, not one
                                                          # that only shows in the window. Only that one may make sound

    @classmethod
    def build(cls, settings) -> Mode:
        """Create the mode with its saved settings; `settings(mode_id)` returns any mode's saved values."""
        return cls(settings(cls.id))

    async def frame(self) -> Image.Image | Animation | Dashboard | Flipbook | None:
        raise NotImplementedError

    def act(self, action: str) -> None:
        """One of `actions` was asked for. Called between two frame() calls."""

    async def close(self) -> None:
        """The engine is done with this mode: another one, or the same with other settings, takes its place."""
