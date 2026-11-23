"""Album art of whatever is playing, via Windows' system media transport controls.

Works for every player that shows up in the Windows volume flyout: Spotify, browsers, foobar2000, ...

On DeepCool's own firmware every picture is a write to the cooler's flash, so the cover changes only when
a track has stayed for a moment. With the direct drawing firmware (`direct`, set by the engine) pictures
cost nothing: a new track is on screen at once, fading in over the last one, and a bar under the title
shows how far into the track the player is, redrawn every second.

**The track's music video** (a setting, off unless switched on; drawn pictures only). When a track
starts, its video is looked for on YouTube (musicvideo.py: one of the track's own length on the artist's
channel, or none) and played where the cover is, or over the whole screen, without its sound. The
picture is kept where the music is: Windows says how far into the track the player is, a clock runs on
from there (`_music_time`), and the video's player follows that clock, leaving frames out or waiting.
A jump in the music (seeking, the same track again) opens the video anew at that place. No video that
fits, no net, a video that will not play: the cover, as ever.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .. import musicvideo, render
from ..config import config_dir
from ..render import H, W
from . import video
from .base import Mode, Setting

FADE = 0.45            # seconds a new track's picture takes to fade in (drawn pictures only)
FADE_TICK = 0.05       # how often frame() is wanted while it fades
# The music video
POLL = 0.5             # seconds between two questions to Windows while a video plays (a video wants pictures more often)
WAIT_TICK = 0.1        # how often frame() is wanted while a video is looked for or being opened
JUMP = 0.35            # seconds the player's word may differ from our clock before the clock is set by it
OFF = 4.0              # seconds the video may be off the music before it is opened anew at the right place
OPENING = 2.0          # ... and how far ahead of the music it is opened: that is how long opening takes
ADDRESS_GOOD = 1200.0  # seconds a video's address is used again without asking YouTube
VIDEO_LINES = {"cover": 480, "screen": 720}        # the stream asked for: the cover's place is 392 pixels, the screen 640


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    art: bytes | None
    position: float | None = None     # seconds into the track now, None when the player does not say
    duration: float | None = None
    playing: bool = True

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


def timeline(line, playing: bool, now: datetime) -> tuple[float, float] | None:
    """(seconds into the track, its length) from a session's timeline properties, None when it has none.

    Players report the position as it was at `last_updated_time` (Spotify only when something changes),
    so while playing the time since then is added. A timeline that was never filled in has a length of 0;
    one that is out of date would put the position past the end, and is not believed."""
    try:
        length = (line.end_time - line.start_time).total_seconds()
        at = (line.position - line.start_time).total_seconds()
        if playing:
            at += (now - line.last_updated_time).total_seconds()
    except (AttributeError, TypeError, OverflowError):
        return None
    if length <= 0 or not -1.0 <= at <= length + 2.0:
        return None
    return min(max(at, 0.0), length), length


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def cover_box(layout: str, compact: bool) -> tuple[int, int, int]:
    """Where the cover is in a picture: (left, top, the length of its side)."""
    art_only = layout == "art"
    size = 448 if art_only else 392 if compact else 416
    return (W - size) // 2, (H - size) // 2 if art_only else 28 if compact else 40, size


@lru_cache(maxsize=4)
def _round_corners(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=22, fill=255)
    return mask


def put_clip(canvas: Image.Image, frame, layout: str, compact: bool) -> None:
    """A frame of the music video where the cover is: its middle, as a square with the cover's corners."""
    x, top, size = cover_box(layout, compact)
    canvas.paste(video.picture(frame, "fill", (size, size)), (x, top), _round_corners(size))


def render_base(track: Track, layout: str = "full", compact: bool = False) -> Image.Image:
    """The picture without the progress bar. `compact` leaves room for the bar under the artist."""
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
    x, top, size = cover_box(layout, compact)
    cover = ImageOps.fit(art, (size, size), Image.LANCZOS)
    mask = _round_corners(size)
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle([x, top + 8, x + size, top + size + 8], radius=22, fill=170)
    canvas.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14)))
    canvas.paste(cover, (x, top), mask)

    if not art_only:
        d = ImageDraw.Draw(canvas)
        title_font, artist_font = render.font(render.BOLD, 34), render.font(render.REGULAR, 26)
        y = top + size + (20 if compact else 26)
        for line in render.wrap(d, track.title or "Unknown title", title_font, W - 48, 2):
            d.text((W / 2, y), line, font=title_font, fill=(255, 255, 255), anchor="ma")
            y += 44
        d.text((W / 2, y + (2 if compact else 6)), render.fit(d, track.artist, artist_font, W - 48), font=artist_font,
               fill=(215, 215, 220), anchor="ma")
    return canvas


def draw_progress(canvas: Image.Image, elapsed: float, length: float, playing: bool) -> None:
    """The bar at the bottom of a compact picture: how far in, the two times, and a pause mark when paused."""
    d = ImageDraw.Draw(canvas, "RGBA")
    x0, x1, y = 36, W - 36, 592
    d.rounded_rectangle([x0, y, x1, y + 6], radius=3, fill=(255, 255, 255, 70))
    at = x0 + (x1 - x0) * min(1.0, max(0.0, elapsed / length))
    d.rounded_rectangle([x0, y, max(at, x0 + 6), y + 6], radius=3, fill=(255, 255, 255, 255))
    d.ellipse([at - 7, y - 4, at + 7, y + 10], fill=(255, 255, 255, 255))
    small = render.font(render.SEMIBOLD, 19)
    d.text((x0, y + 18), clock(elapsed), font=small, fill=(225, 225, 230, 255), anchor="la")
    d.text((x1, y + 18), clock(length), font=small, fill=(225, 225, 230, 255), anchor="ra")
    if not playing:
        for left in (W / 2 - 8, W / 2 + 3):
            d.rectangle([left, y + 20, left + 5, y + 37], fill=(225, 225, 230, 255))


def render_track(track: Track, layout: str = "full", progress: tuple[float, float, bool] | None = None) -> Image.Image:
    """`progress`: (seconds in, length, playing) adds the bar; not in the "art" layout, which is the cover alone."""
    with_bar = progress is not None and layout != "art"
    canvas = render_base(track, layout, compact=with_bar)
    if with_bar:
        draw_progress(canvas, *progress)
    return canvas


class Playback:
    """Whether the system's current media session is playing, and what: for modes that need no more."""

    ART_TRIES = 8                                # looks for a track's cover: players hand it out after the title

    def __init__(self):
        self._manager = None
        self.title = self.artist = ""            # of what plays, as far as the player says; kept while it pauses
        self.want_art = False                    # whoever wants the cover too says so
        self.art: bytes | None = None            # the cover of what plays (the one before, until this one's is there)
        self._art_for: tuple | None = None       # the track `art` is of
        self._art_asked: tuple | None = None     # ... and the one it is being looked for, how often so far
        self._art_tries = 0

    async def playing(self) -> bool:
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status

            if self._manager is None:
                self._manager = await Manager.request_async()
            session = self._manager.get_current_session()
            playing = session is not None and session.get_playback_info().playback_status == Status.PLAYING
            if playing:
                props = await session.try_get_media_properties_async()
                self.title, self.artist = ((props.title or "").strip(), (props.artist or "").strip()) if props else ("", "")
                if self.want_art:
                    await self._cover(props)
            return playing
        except (ImportError, OSError, AttributeError):
            return False

    async def _cover(self, props) -> None:
        """Read the cover of what plays, once per track. A track that has none after ART_TRIES looks has none."""
        track = (self.title, self.artist)
        if track == self._art_for:
            return
        if track != self._art_asked:
            self._art_asked, self._art_tries = track, 0
        self._art_tries += 1
        try:
            art = await _read_thumbnail(props.thumbnail) if props is not None else None
        except (OSError, AttributeError):
            art = None
        if art or self._art_tries >= self.ART_TRIES:
            self.art, self._art_for = art, track


class NowPlaying(Mode):
    id = "now_playing"
    title = "Now Playing"
    description = "Album art from any player"
    glyph = ""
    claims = "music playing"
    settings = (
        Setting("layout", "Layout", "choice", "full", (("full", "Cover with title and artist"), ("art", "Cover only")),
                hint="With the direct drawing firmware the first layout also shows how far into the track the player "
                     "is. On DeepCool's own firmware “Cover only” rewrites the display only when the album changes."),
        Setting("video", "Music video", "choice", "off",
                (("off", "Off"), ("cover", "Where the cover is"), ("screen", "Over the whole screen")),
                hint="The track's own music video from YouTube, without its sound, in step with the music. Only a "
                     "video of the track's exact length on the artist's own channel is taken, so that picture and "
                     "music fit: about one track in three has one, the others keep their cover. Artist and title go "
                     "to YouTube's search for it. Needs the direct drawing firmware."),
    )

    SETTLE = 2.5          # a track must stay current this long before it is drawn (skipping through a playlist)
    SETTLE_NO_ART = 5.0   # players often publish the thumbnail a moment after the title

    def __init__(self, values=None):
        super().__init__(values)
        self._manager = None
        self._shown = None
        self._pending = None
        self._pending_since = 0.0
        self._look = None                                  # drawn pictures: what the last one showed
        self._last: Image.Image | None = None              # ... and the picture itself, to fade from
        self._fade: tuple[Image.Image, float] | None = None
        self._base: tuple | None = None                    # (what it is of, the picture without its bar)
        self.placeholder = ("Nothing is playing", "Start some music and the cover shows up here.")
        # the music video:
        self._found = musicvideo.Found(config_dir() / "music-videos.json")
        self._find, self._resolve, self._make_player = musicvideo.find, video.resolve, video.Player
        self._clip = None                                  # the video's player, while a video plays
        self._clip_key: str | None = None                  # the track the rest of these is about
        self._link: str | None = None                      # its video: None not known yet, "" there is none
        self._finding: asyncio.Future | None = None        # the search for it, while it runs
        self._clip_over = False                            # the video ended, or would not play: the cover from here on
        self._polled: tuple[float, Track | None] = (float("-inf"), None)     # when Windows was last asked, and its answer
        self._offset: float | None = None                  # the music's clock: its position is this plus monotonic()
        self._frozen: float | None = None                  # ... or, while it is paused, this
        self._addresses: dict = {}                         # link -> (when, where its stream is)
        self._sought = float("-inf")                       # when the video was last opened anew to catch up

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

            playing = self.active = session.get_playback_info().playback_status == Status.PLAYING
        except (OSError, AttributeError):
            playing, self.active = False, True
        props = await session.try_get_media_properties_async()
        if props is None or not (props.title or props.artist):
            return None
        try:
            art = await _read_thumbnail(props.thumbnail)
        except OSError:
            art = None
        try:
            where = timeline(session.get_timeline_properties(), playing, datetime.now(timezone.utc))
        except OSError:
            where = None
        position, duration = where or (None, None)
        return Track(props.title or "", props.artist or "", props.album_title or "", art, position, duration, playing)

    # ------------------------------------------------------------ the music video
    def _music_time(self) -> float | None:
        """Where in the track the music is right now, by our clock. None: not known."""
        if self._frozen is not None:
            return self._frozen
        return None if self._offset is None else self._offset + time.monotonic()

    def _set_clock(self, track: Track, now: float) -> None:
        """Set the music's clock by what the player says. Players report a position now and then and not
        exactly, so a small difference moves the clock a little (it settles on their average), and only a
        large one, or the music starting again after a pause, sets it outright."""
        if track.position is None:
            self._offset = self._frozen = None
        elif not track.playing:
            self._frozen, self._offset = track.position, None
        else:
            said = track.position - now
            if self._offset is None or abs(said - self._offset) > JUMP:
                self._offset = said
            else:
                self._offset += (said - self._offset) * 0.25
            self._frozen = None

    def _address(self, link: str):
        """Where the video's stream is; asked of YouTube once in a while, not at every opening."""
        kept, now = self._addresses.get(link), time.monotonic()
        if kept is None or now - kept[0] > ADDRESS_GOOD:
            kept = (now, self._resolve(link, VIDEO_LINES.get(self.values["video"], 480)))
            self._addresses = {link: kept}
        return kept[1]

    def _drop_clip(self) -> None:
        clip, self._clip = self._clip, None
        if clip is not None:
            clip.stop()

    def _ended(self) -> None:                              # (in the player's thread)
        self._clip_over = True

    def _video(self, track: Track, now: float):
        """The frame of the track's music video that belongs on screen: (its number, the frame). None:
        there is no video (yet), and the cover is shown."""
        wanted = bool(track.title and track.artist and track.duration and track.duration >= 30)
        key = musicvideo.Found.key(track.artist, track.title, track.duration) if wanted else None
        if key != self._clip_key:                          # another track
            self._drop_clip()
            self._clip_key, self._link, self._finding, self._clip_over = key, None, None, False
            if key is not None:
                self._link = self._found.get(track.artist, track.title, track.duration)
                if self._link is None:
                    self._finding = asyncio.ensure_future(asyncio.to_thread(self._find, track.artist, track.title, track.duration))
        if self._finding is not None and self._finding.done():
            finding, self._finding = self._finding, None
            try:
                found = finding.result()
                self._link = found.link if found else ""
                self._found.put(track.artist, track.title, track.duration, self._link)
            except Exception:                              # noqa: BLE001  (no net: not kept, asked again another time)
                self._link = ""
        if not self._link or self._clip_over:
            return None
        at = self._music_time()
        if self._clip is None:
            if not track.playing or at is None:
                return None
            self._clip = self._make_player(self._link, resolve=self._address, on_end=self._ended, speaker=None,
                                           follow=self._music_time)
            self._clip.seek(at + OPENING)
            self._sought = now
        clip = self._clip
        if track.playing:
            clip.ask()                                     # (paused: it is not asked, and closes its stream after a while)
        if clip.state == "failed":
            self._clip_over = True
            self._drop_clip()
            return None
        seq, frame = clip.latest()
        if frame is None:
            return None
        if track.playing and at is not None and abs(clip.position - at) > OFF and now - self._sought > OPENING + 2.0:
            self._sought = now                             # the music jumped: so does the video
            clip.seek(at + OPENING)
        return seq, frame

    async def close(self) -> None:
        self._drop_clip()

    async def frame(self):
        now = time.monotonic()
        if self._clip is not None and self._polled[1] is not None and now - self._polled[0] < POLL:
            at = self._music_time()                        # no need to ask Windows again yet
            track = replace(self._polled[1], position=self._polled[1].position if at is None else at)
        else:
            track = await self._track()
            self._polled = (now, track)
            if track is not None:
                self._set_clock(track, now)
        if track is None:
            self.active = False
            self._drop_clip()
            self._clip_key = None
            if self._shown is None:
                self.status = "Nothing is playing"
            return None
        self.status = " — ".join(p for p in (track.artist, track.title) if p)
        layout = self.values["layout"]
        key = (track.art_hash,) if layout == "art" and track.art else (track.title, track.artist, track.art_hash)
        if self.direct:
            return self._drawn_frame(track, layout, key)
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

    def _drawn_frame(self, track: Track, layout: str, key) -> Image.Image | None:
        """Pictures that cost nothing: no waiting for a track to settle, the progress bar every second, and
        a new track (or a cover that arrives after its title) fades in over the picture before it."""
        now = time.monotonic()
        progress = None
        if layout != "art" and track.duration:
            progress = (int(track.position), int(track.duration), track.playing)
        how = self.values["video"]
        clip = self._video(track, now) if how != "off" else None
        if clip is not None:
            self.tick, self._fade = video.TICK, None
            self.status += " · music video"
            look = ("clip", clip[0], layout, how, progress)
            if look == self._look:
                return None
            self._look, self._shown = look, key
            if how == "screen":
                picture = video.picture(clip[1], "fill")
            else:
                of = (key, layout, progress is not None)
                if self._base is None or self._base[0] != of:
                    self._base = (of, render_base(track, layout, compact=progress is not None))
                picture = self._base[1].copy()
                put_clip(picture, clip[1], layout, progress is not None)
                if progress is not None:
                    draw_progress(picture, *progress)
            self._last = picture
            return picture
        if self._fade is None:                             # (a fade sets its own pace)
            self.tick = WAIT_TICK if how != "off" and (self._finding is not None or self._clip is not None) else None
        look = (key, layout, progress)
        if look == self._look and self._fade is None:
            return None
        of = (key, layout, progress is not None)
        if self._base is None or self._base[0] != of:
            self._base = (of, render_base(track, layout, compact=progress is not None))
        picture = self._base[1].copy()
        if progress is not None:
            draw_progress(picture, *progress)
        if key != self._shown and self._last is not None and self._fade is None:
            self._fade = (self._last, now)
        self._shown, self._look = key, look
        if self._fade is not None:
            done = (now - self._fade[1]) / FADE
            if done < 1:
                self.tick = FADE_TICK
                return Image.blend(self._fade[0], picture, done)
            self._fade, self.tick = None, None
        self._last = picture
        return picture
