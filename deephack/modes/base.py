"""Base class for display modes.

A mode produces 480x640 images. The engine calls `frame()` about once a second; return a new PIL image
when the display should change and None otherwise. Every image a mode returns ends up as a write to the
cooler's flash, so only return one when the content really changed.

Modes that sometimes have something worth showing (music playing, a game running) set `claims` to a short
description and keep `active` up to date in `frame()`; the Auto mode gives them the screen while it is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image


@dataclass
class Animation:
    """A looping animation. Every frame is stored on the cooler's flash, so keep the frame count low."""
    frames: list[Image.Image]
    duration_ms: int               # length of one loop


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str                      # "choice" | "text" | "folder" (text with a Browse button) | "order" (see parse_order)
    default: str
    choices: tuple[tuple[str, str], ...] = ()   # (value, label) for kind "choice" and "order"
    placeholder: str = ""
    hint: str = ""


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
    settings: tuple[Setting, ...] = ()

    def __init__(self, values: dict | None = None):
        values = values or {}
        self.values = {s.key: values.get(s.key, s.default) for s in self.settings}
        self.status = ""                                  # one-line state shown in the UI
        self.placeholder: tuple[str, str] | None = None   # (title, subtitle) for the preview when there is no frame
        self.active = False                               # has something worth showing right now (see `claims`)

    @classmethod
    def build(cls, settings) -> Mode:
        """Create the mode with its saved settings; `settings(mode_id)` returns any mode's saved values."""
        return cls(settings(cls.id))

    async def frame(self) -> Image.Image | Animation | None:
        raise NotImplementedError

    async def close(self) -> None:
        pass
