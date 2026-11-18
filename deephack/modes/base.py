"""Base class for display modes.

A mode produces 480x640 images. The engine calls `frame()` about once a second; return a new PIL image
when the display should change and None otherwise. Every image a mode returns ends up as a write to the
cooler's flash, so only return one when the content really changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str                      # "choice" | "text"
    default: str
    choices: tuple[tuple[str, str], ...] = ()   # (value, label) for kind == "choice"
    placeholder: str = ""
    hint: str = ""


class Mode:
    id: str = ""
    title: str = ""
    description: str = ""
    glyph: str = ""                # character from the "Segoe Fluent Icons" font
    settings: tuple[Setting, ...] = ()

    def __init__(self, values: dict | None = None):
        values = values or {}
        self.values = {s.key: values.get(s.key, s.default) for s in self.settings}
        self.status = ""                                  # one-line state shown in the UI
        self.placeholder: tuple[str, str] | None = None   # (title, subtitle) for the preview when there is no frame

    async def frame(self) -> Image.Image | None:
        raise NotImplementedError

    async def close(self) -> None:
        pass
