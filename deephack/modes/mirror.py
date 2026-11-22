"""Mirror: a window, or a part of the PC's screen, on the cooler as it is right now.

Needs the direct drawing firmware, as Video does: a cooler that stores every picture before it shows it
gets one picture, and a line that says why.

How it works:

- **The picture is taken when the engine asks** (capture.py), some sixteen times a second: a part of
  the screen as Windows shows it, or one window as it draws itself, which also works while another
  window covers it.
- **The next picture is taken while the last one is on its way.** Sending a picture to the cooler takes
  some 60 ms, taking one some 25 (Windows hands the screen out once per refresh). One after the other
  that was ten pictures a second on the cooler; with the taking begun 30 ms after a picture is handed
  over (`LAG`), so that it is ready just when the cooler is, it is as many as a video gets.
- **Only a picture that differs is sent.** What was taken is compared with what was taken before, byte
  for byte (a fifth of a millisecond); a window in which nothing moves costs the taking and no more.
  After a second without a change it is looked at four times a second only, until something moves.
- **A window is found again** by its program and its kind when it is gone (the program was started
  anew), with the title it had if there is one like that. While it is minimised or closed the cooler
  says so: Windows does not draw a minimised window.
- The picture fills the screen with its middle, or is shown whole with bars, and can lie on its side:
  a wide window fits the upright screen better turned.
"""
from __future__ import annotations

import asyncio
import time

from PIL import Image

from .. import capture, render
from ..render import H, W
from .base import Mode, Setting

TICK = 0.06                 # seconds between pictures, as for a video
STILL_AFTER = 16            # pictures without a change before the looking slows down (a second)
STILL_EVERY = 4             # ... to every fourth tick
SEEK = 1.0                  # seconds between looks for a window that is gone
QUALITY = 75                # JPEG quality of its pictures. A window full of moving colour is 45 KB at 85, which the cooler
                            # takes eleven of a second, 0.16 s behind the screen; at 75 it is 33 KB: sixteen, 0.09 s behind
LAG = 0.03                  # seconds from handing a picture over to taking the next: it is ready when the cooler is
SMALLEST = 16               # pixels a side: a smaller part of the screen is a slip of the hand
TURNS = {"left": Image.Transpose.ROTATE_90, "right": Image.Transpose.ROTATE_270}


def part_value(left: int, top: int, width: int, height: int) -> str:
    return f"part:{left},{top},{width},{height}"


def window_value(window: capture.Window) -> str:
    return "window:" + "\t".join((window.program, window.kind, window.title))


def parse(value: str) -> tuple | None:
    """A "source" setting: ("part", left, top, width, height), ("window", program, kind, title), or None
    for nothing chosen and for anything that is neither."""
    kind, _, rest = (value or "").partition(":")
    if kind == "part":
        try:
            left, top, width, height = (int(n) for n in rest.split(","))
        except ValueError:
            return None
        return ("part", left, top, width, height) if width >= SMALLEST and height >= SMALLEST else None
    if kind == "window":
        fields = rest.split("\t", 2)
        return ("window", *fields) if len(fields) == 3 and fields[1] else None
    return None


def describe(value: str) -> str:
    """The choice in words, for the window and the status line."""
    what = parse(value)
    if what is None:
        return "Nothing chosen"
    if what[0] == "part":
        return f"Part of the screen · {what[3]} × {what[4]}"
    return what[3] or what[1]


def shape(picture: Image.Image, fit: str = "fill", turn: str = "") -> Image.Image:
    """A picture of any size as the screen's 480 x 640: filling it (the middle of what does not fit) or
    whole, with bars; lying on its left or right side if wanted."""
    turned = TURNS.get(turn)
    width, height = (H, W) if turned is not None else (W, H)          # the size before it is turned
    if fit == "fit":
        scale = min(width / picture.width, height / picture.height)
        size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
        small = picture.resize(size, Image.Resampling.HAMMING, reducing_gap=2.0)
        out = Image.new("RGB", (width, height))
        out.paste(small, ((width - size[0]) // 2, (height - size[1]) // 2))
    else:
        scale = max(width / picture.width, height / picture.height)
        w, h = width / scale, height / scale                          # the part of the picture that fills it
        box = ((picture.width - w) / 2, (picture.height - h) / 2, (picture.width + w) / 2, (picture.height + h) / 2)
        out = picture.resize((width, height), Image.Resampling.HAMMING, box=box, reducing_gap=2.0)
    return out.transpose(turned) if turned is not None else out


class Mirror(Mode):
    id = "mirror"
    title = "Mirror"
    description = "A window or part of the screen"
    glyph = ""
    needs_direct = True
    quality = QUALITY
    settings = (
        Setting("source", "Show", "capture", "", live=True,
                hint="A part of the screen is shown as it looks, whatever is there. A window is followed when it moves and "
                     "shown while others cover it, but not while it is minimised. Video that is protected against copying "
                     "(Netflix and the like) comes out black."),
        Setting("fit", "Picture", "choice", "fill", (("fill", "Fill the screen"), ("fit", "The whole picture, with bars")),
                live=True, hint="Filling shows the middle of what does not fit."),
        Setting("turn", "Turned", "choice", "", (("", "Upright"), ("left", "On its left side"), ("right", "On its right side")),
                live=True, hint="A wide window fits the upright screen better on its side."),
    )

    def __init__(self, values: dict | None = None, grabber=capture.Grabber, windows=capture, clock=time.monotonic):
        super().__init__(values)
        self._grabber = grabber()
        self._windows = windows                  # what finds windows and says how they are: capture, or a stand-in
        self._clock = clock
        self._hwnd: int | None = None
        self._sought = float("-inf")             # when the window was last looked for
        self._key = None                         # the settings the last picture was made with
        self._raw = None                         # ... and what was taken for it
        self._said = None                        # (what is the matter, name) of the message on the cooler, if one is
        self._same = self._ticks = 0
        self._still = None                       # the settings of the one picture a cooler without direct drawing got
        self._ahead: asyncio.Future | None = None    # the next picture, being taken while the last one is drawn

    # ------------------------------------------------------------ taking the picture
    def _find(self, program: str, kind: str, title: str) -> int | None:
        if self._hwnd is not None and self._windows.alive(self._hwnd):
            return self._hwnd
        self._hwnd = None
        now = self._clock()
        if now - self._sought >= SEEK:
            self._sought = now
            found = self._windows.find(program, kind, title)
            self._hwnd = found.hwnd if found else None
        return self._hwnd

    def _take(self, what: tuple) -> tuple[str, Image.Image | None]:
        """("", picture), or (what is the matter, None). Runs in a thread: it waits for the screen."""
        try:
            if what[0] == "part":
                return "", self._grabber.part(*what[1:])
            hwnd = self._find(*what[1:])
            if hwnd is None:
                return "closed", None
            if self._windows.minimised(hwnd):
                return "minimised", None
            picture = self._grabber.window(hwnd)
            return ("", picture) if picture is not None else ("closed", None)
        except OSError as error:
            return str(error), None

    def _work(self, what: tuple, fit: str, turn: str, wait: float = 0.0) -> tuple[str, Image.Image | None, tuple | None]:
        """Take a picture and make the screen's of it: (what is the matter, the picture or None when it is
        the one before, the size of what was taken). In a thread; never two at a time."""
        if wait:
            time.sleep(wait)
        matter, taken = self._take(what)
        if matter:
            return matter, None, None
        raw = getattr(self._grabber, "raw", None)            # what was taken, as bytes, if the grabber keeps it
        if raw is None:
            raw = taken.tobytes()
        if raw == self._raw:
            return "", None, taken.size
        self._raw = raw
        return "", shape(taken, fit, turn), taken.size

    async def _drop(self) -> None:
        """Wait for a picture that is being taken ahead, and forget it."""
        ahead, self._ahead = self._ahead, None
        if ahead is not None:
            try:
                await ahead
            except Exception:
                pass

    def _say(self, matter: str, name: str) -> Image.Image | None:
        """The picture that says why there is none; None when the cooler shows it already."""
        text = {"closed": "The window is closed. It is shown again when it is back.",
                "minimised": "The window is minimised: Windows does not draw it."}.get(matter, matter)
        self.status = f"{name} · {matter}"
        self._raw = None
        if (matter, name) == self._said:
            return None
        self._said = (matter, name)
        return render.message(name if len(name) <= 28 else name[:27] + "…", text)

    async def frame(self) -> Image.Image | None:
        self.tick = TICK if self.direct else None
        source, fit, turn = self.values["source"], self.values["fit"], self.values["turn"]
        what = parse(source)
        if what is None:
            self.status, self.placeholder = "Nothing chosen", ("Mirror", "Choose a window or a part of the screen")
            self._key = self._still = None
            return None
        key = (source, fit, turn)
        if not self.direct and self._still == key:           # that cooler has its one picture
            return None
        if key != self._key:                                 # another choice: look at once, and for the window anew
            await self._drop()
            if self._key is None or source != self._key[0]:
                self._hwnd, self._sought = None, float("-inf")
            self._key, self._raw, self._said, self._same = key, None, None, 0
        self._ticks += 1
        if self.direct and self._same >= STILL_AFTER and self._ticks % STILL_EVERY:
            return None
        ahead, self._ahead = self._ahead, None
        if ahead is None:
            ahead = asyncio.to_thread(self._work, what, fit, turn)
        matter, picture, size = await ahead
        name = describe(source)
        if matter:
            return self._say(matter, name)
        self._said = None
        if not self.direct:
            self._still = key
            self.status = "One picture: mirroring needs direct drawing"
            return picture
        self.status = f"{name} · {size[0]} × {size[1]}" if what[0] == "window" else name
        self._same = 0 if picture is not None else self._same + 1
        if self._same < STILL_AFTER:                         # something moves: the next one is taken meanwhile. Handed
            # to a thread here and now: the engine draws before it lets anything else of asyncio's run
            self._ahead = asyncio.get_running_loop().run_in_executor(None, self._work, what, fit, turn, LAG)
        return picture

    async def close(self) -> None:
        await self._drop()
        self._grabber.close()
