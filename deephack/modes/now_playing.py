"""Album art of whatever is playing, via Windows' system media transport controls.

Works for every player that shows up in the Windows volume flyout: Spotify, browsers, foobar2000, ...
"""
from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .. import render
from ..render import H, W
from .base import Mode, Setting


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    art: bytes | None

    @property
    def art_hash(self) -> str:
        return hashlib.sha1(self.art or b"").hexdigest()


async def _read_thumbnail(ref) -> bytes | None:
    from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    if ref is None:
        return None
    stream = await ref.open_read_async()
    size = stream.size
    if not size:
        return None
    buf = Buffer(size)
    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    try:
        return bytes(memoryview(buf))[: buf.length]
    except TypeError:
        out = bytearray(buf.length)
        DataReader.from_buffer(buf).read_bytes(out)
        return bytes(out)


def render_track(track: Track, layout: str = "full") -> Image.Image:
    art = None
    if track.art:
        try:
            art = Image.open(io.BytesIO(track.art)).convert("RGB")
        except OSError:
            art = None
    if art is None:
        art = render.vertical_gradient((58, 60, 82), (24, 25, 36), (400, 400))

    # background: the cover zoomed to fill, blurred and dimmed
    bg = ImageOps.fit(art, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    canvas = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.45)

    art_only = layout == "art"
    size = 448 if art_only else 416
    top = (H - size) // 2 if art_only else 40
    x = (W - size) // 2
    cover = ImageOps.fit(art, (size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=22, fill=255)
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle([x, top + 8, x + size, top + size + 8], radius=22, fill=170)
    canvas.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14)))
    canvas.paste(cover, (x, top), mask)

    if not art_only:
        d = ImageDraw.Draw(canvas)
        title_font, artist_font = render.font(render.BOLD, 34), render.font(render.REGULAR, 26)
        y = top + size + 26
        for line in render.wrap(d, track.title or "Unknown title", title_font, W - 48, 2):
            d.text((W / 2, y), line, font=title_font, fill=(255, 255, 255), anchor="ma")
            y += 44
        d.text((W / 2, y + 6), render.fit(d, track.artist, artist_font, W - 48), font=artist_font, fill=(215, 215, 220), anchor="ma")
    return canvas


class NowPlaying(Mode):
    id = "now_playing"
    title = "Now Playing"
    description = "Album art from any player"
    glyph = ""
    claims = "music playing"
    settings = (
        Setting("layout", "Layout", "choice", "full", (("full", "Cover with title and artist"), ("art", "Cover only")),
                hint="“Cover only” rewrites the display only when the album changes."),
    )

    SETTLE = 2.5          # a track must stay current this long before it is drawn (skipping through a playlist)
    SETTLE_NO_ART = 5.0   # players often publish the thumbnail a moment after the title

    def __init__(self, values=None):
        super().__init__(values)
        self._manager = None
        self._shown = None
        self._pending = None
        self._pending_since = 0.0
        self.placeholder = ("Nothing is playing", "Start some music and the cover shows up here.")

    async def _track(self) -> Track | None:
        if self._manager is None:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

            self._manager = await Manager.request_async()
        session = self._manager.get_current_session()
        if session is None:
            self.active = False
            return None
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status

            self.active = session.get_playback_info().playback_status == Status.PLAYING
        except (OSError, AttributeError):
            self.active = True
        props = await session.try_get_media_properties_async()
        if props is None or not (props.title or props.artist):
            return None
        try:
            art = await _read_thumbnail(props.thumbnail)
        except OSError:
            art = None
        return Track(props.title or "", props.artist or "", props.album_title or "", art)

    async def frame(self):
        track = await self._track()
        if track is None:
            self.active = False
            if self._shown is None:
                self.status = "Nothing is playing"
            return None
        self.status = " — ".join(p for p in (track.artist, track.title) if p)
        layout = self.values["layout"]
        key = (track.art_hash,) if layout == "art" and track.art else (track.title, track.artist, track.art_hash)
        if key == self._shown:
            return None
        if key != self._pending:
            self._pending, self._pending_since = key, time.monotonic()
        waited = time.monotonic() - self._pending_since
        needed = self.SETTLE_NO_ART if not track.art else (0.0 if self._shown is None else self.SETTLE)
        if waited < needed:
            return None
        self._shown = key
        return render_track(track, layout)
