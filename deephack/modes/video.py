"""Video: a YouTube link or a video file, playing on the cooler, with its sound from the PC if wanted.

Needs the direct drawing firmware: a cooler that stores every picture before it shows it cannot play
anything. That one gets a single picture of the video and a line that says why.

How it works:

- **Nothing is downloaded first.** The video is read from its address as it plays (PyAV, which is
  ffmpeg). For a YouTube link yt-dlp asks YouTube for that address: the stream of 720 lines when the
  picture fills the screen's 640, the one of 480 when it is shown whole (it is 270 lines tall then). A
  video file on the PC is read the same way, whatever its size.
- **The clock decides which frame shows.** A thread (`Player`) decodes at the video's own pace and keeps
  the newest frame; the mode hands the engine that one whenever it asks. The cooler takes some fifteen
  pictures a second, so about every other frame of a 30 a second video is left out. Nothing queues up,
  and the video never runs slow. When the connection cannot keep up, the picture stands until it can.
- **It reads only while somebody looks.** No frame asked for in three seconds (another mode has the
  screen, deephack is paused): the stream is closed, and opened again where it was when frames are wanted.
- At its end the video starts again. A stream that breaks off, or an address that has expired (YouTube's
  last some hours), is opened afresh at the same place.

- **Sound** is off unless switched on. It comes from the PC's speakers (sound.py), and the picture goes
  by it: the frame on screen is the one that belongs to what is being heard, chosen a tenth of a second
  ahead because that is how long a frame needs to get to the cooler's screen (`LEAD`). A YouTube video's sound is a
  stream of its own, read by a second thread; a file is opened a second time for it; where one address
  on the net holds both (TikTok), both are read from it at once, the sound a little ahead. Only the
  mode on the cooler sounds, not one that is merely looked at in the window.

Not done: the turn that phone videos ask for (one filmed upright plays on its side).
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

from PIL import Image

from .. import ytdlp
from ..render import H, W
from ..sound import FRAME, RATE, Speaker
from .base import Mode, Setting

TICK = 0.06                 # seconds between pictures asked of the mode: a little more often than the cooler takes them
QUALITY = 65                # JPEG quality of its pictures. A frame of a crowd is 65 KB at 85, which the cooler takes eleven
                            # of a second; at 65 it is 47 KB, and fifteen
IDLE = 3.0                  # seconds without anybody asking for a frame: the stream is closed
BEHIND = 0.3                # a frame this late means the reading cannot keep up: the clock is set anew, nothing is skipped
RETRY = 5.0                 # seconds before a video that would not open is tried again
RETRY_NEXT = 1.0            # ... and before the next one is tried, where there is a next one (`on_error`)
OPEN_TIMEOUT = (15.0, 15.0)     # seconds to open the address, and to wait for more of it
# what to ask YouTube for: H.264 when there is one (every ffmpeg decodes it, fast), and no more lines than needed
FORMATS = "bestvideo[height<={0}][vcodec^=avc1]/bestvideo[height<={0}]/best[height<={0}]/best"
# ... and with its sound, which YouTube keeps in a stream of its own
FORMATS_SOUND = ("bestvideo[height<={0}][vcodec^=avc1]+bestaudio/bestvideo[height<={0}]+bestaudio/"
                 "best[height<={0}]/best")
LINES = {"fill": 720, "fit": 480}            # on the cooler the whole of deephack took 13 % of a CPU core for the
                                             # first and 10 % for the second
NETWORK = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5"}
# Sound
LEAD = 0.12                 # seconds the picture is chosen ahead of what is heard. Measured on the cooler: a frame of a
                            # real video is 65 to 70 ms on its way from here until the cooler has taken it, and the
                            # speaker's count is some 15 ms behind what is heard. Then the cooler has to show it: that
                            # cannot be measured from here and is taken as 35 ms
SOUND_AHEAD = 0.4           # seconds of sound kept ready in the speaker
SOUND_WAIT = 3.0            # the picture waits this long at most for the sound to begin
HUSH = 0.5                  # seconds without anybody asking for a frame: the sound pauses (after IDLE it all closes)
PENDING, PENDING_BYTES = 45, 64 << 20        # frames that may wait while the reading runs ahead for the sound
SOUND_LEVELS = (("0", "Off"), ("20", "20 %"), ("40", "40 %"), ("60", "60 %"), ("80", "80 %"), ("100", "100 %"))


def clock_text(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def is_file(source: str) -> bool:
    return "://" not in source and Path(source).expanduser().is_file()


def read_twice(address: str) -> bool:
    """A file is opened a second time for its sound, which costs nothing. An address on the net is read
    once, picture and sound together."""
    return "://" not in address


class LinkError(RuntimeError):
    """The site would not say where the video is. (YouTube may have changed: a newer yt-dlp can help.)"""


def _ask(source: str, wanted: str) -> dict:
    """What yt-dlp finds out about a link."""
    yt_dlp = ytdlp.module()                                  # a second to load: only when a link is played
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "format": wanted}) as ydl:
        return ydl.extract_info(source, download=False)


def resolve(source: str, lines: int = 720, sound: bool = False) -> tuple[str, dict, str, str | None]:
    """Where to read the video from: (address, options for opening it, title, address of its sound). A
    file on this PC is its own address; for anything else yt-dlp asks the site (YouTube and a great many
    others) for a stream of at most that many lines. The sound's address is None when it was not asked
    for, and when it is in the same stream as the picture."""
    if is_file(source):
        path = Path(source).expanduser()
        return str(path), {}, path.stem, None
    wanted = (FORMATS_SOUND if sound else FORMATS).format(lines)
    ytdlp.keep_current()                                     # once a day, if that is switched on (Settings)
    try:
        info = _ask(source, wanted)
    except Exception as error:                               # noqa: BLE001  (whatever a site can do wrong)
        if not ytdlp.keep_current(ytdlp.HOUR):               # ... and now, since a link will not open
            raise LinkError(brief(error)) from error
        try:
            info = _ask(source, wanted)                      # with the newer one
        except Exception as again:                           # noqa: BLE001
            raise LinkError(brief(again)) from again
    if info.get("entries"):                                  # a playlist after all: its first video
        info = next(iter(info["entries"]))
    parts = info.get("requested_formats") or [info]          # picture and sound apart, or one stream with both
    picture_part = next((part for part in parts if part.get("vcodec") not in (None, "none")), parts[0])
    sound_part = next((part for part in parts if part is not picture_part and part.get("acodec") not in (None, "none")), None)
    headers = "".join(f"{name}: {value}\r\n" for name, value in
                      (picture_part.get("http_headers") or info.get("http_headers") or {}).items())
    return (picture_part["url"], {**NETWORK, "headers": headers}, info.get("title") or "",
            sound_part["url"] if sound_part else None)


_graphs: dict[tuple, tuple] = {}             # (width, height, pixel format, fit) -> ffmpeg's filters for such frames
_graphs_lock = threading.Lock()


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def picture(frame, fit: str = "fill") -> Image.Image:
    """A decoded frame as the screen's picture: filling it (the middle of a wider picture) or whole, with bars.

    Cut and scaled by ffmpeg's filters while it is still YUV, and only the 480 x 640 that are left are
    turned into RGB: 1.3 ms of CPU for a frame of 854 x 480. (Scaling the whole frame up first and
    cutting the picture out of that took 6 to 9.)"""
    import av

    key = (frame.width, frame.height, frame.format.name, fit)
    with _graphs_lock:
        if key not in _graphs:
            if len(_graphs) > 8:
                _graphs.clear()
            graph = av.filter.Graph()
            source = graph.add_buffer(width=frame.width, height=frame.height, format=frame.format,
                                      time_base=frame.time_base or Fraction(1, 1000))
            if fit == "fill":
                scale = max(W / frame.width, H / frame.height)
                steps = [("crop", f"{min(frame.width, _even(W / scale))}:{min(frame.height, _even(H / scale))}"),
                         ("scale", f"{W}:{H}:flags=bilinear")]
            else:
                scale = min(W / frame.width, H / frame.height)
                steps = [("scale", f"{min(W, _even(frame.width * scale))}:{min(H, _even(frame.height * scale))}:flags=bilinear"),
                         ("pad", f"{W}:{H}:(ow-iw)/2:(oh-ih)/2:black")]
            last = source
            for name, arguments in steps + [("buffersink", None)]:
                step = graph.add(name, arguments)
                last.link_to(step)
                last = step
            graph.configure()
            _graphs[key] = (graph,)
        graph = _graphs[key][0]
        graph.push(frame)
        return graph.pull().to_image()


def brief(error: Exception) -> str:
    """An error as one short line for the window."""
    text = str(error).strip() or type(error).__name__
    for start in ("ERROR: ", "[youtube] "):
        text = text.removeprefix(start)
    return text.splitlines()[0][:140]


class Player:
    """Reads a video in a thread of its own and keeps the frame that belongs on screen now.

    `ask()` whenever a frame is wanted (it starts the reading, and keeps it going); `latest()` is the
    newest frame and its number. `state`: "idle", "opening", "playing" or "failed" (then `error` says why).

    By itself it plays one video over and over. For a list of them: `resolve` is asked where the video
    is every time one is opened, so it can name another; `on_end` is called at a video's end and
    `on_error` when one would not play, before the next is opened; `skip()` opens the next at once.

    Sound: `set_sound(True)` and the video's sound is played (`speaker` makes what plays it, see
    sound.py), as loud as `volume` says. The picture then goes by what is being heard. `silent` says
    why there is none when there should be."""

    def __init__(self, source: str, resolve=resolve, clock=time.monotonic, on_end=None, on_error=None, speaker=Speaker):
        self.source, self._resolve, self._clock = source, resolve, clock
        self._on_end, self._on_error, self._skipped = on_end, on_error, False
        self.title, self.duration, self.position = "", 0.0, 0.0
        self.state, self.error = "idle", ""
        self.link_failed = False                 # the error is the site's, not the stream's: see LinkError
        self.opened = 0                          # times the video was opened: 1, and one more for every new start
        self._frame, self._seq = None, 0
        self._asked = float("-inf")
        self._resume = 0.0                       # seconds into the video to go on from
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.sound, self.silent = False, ""
        self._make_speaker, self._speaker, self._volume = speaker, None, 1.0
        self._again = False                      # open the video anew where it is (its sound was switched on or off)

    def ask(self) -> None:
        with self._lock:
            self._asked = self._clock()
            if self._speaker is not None and self._speaker.paused:
                self._speaker.resume()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="video", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._asked = float("-inf")
        self._hush()

    def skip(self) -> None:
        """Leave the video that is playing and open what `resolve` names now, from its beginning."""
        self._skipped = True
        self._hush()

    def set_sound(self, on: bool) -> None:
        """With sound or without. A video that is playing is opened anew for it, where it is."""
        if on != self.sound:
            self.sound, self._again = on, True

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, volume: float) -> None:
        self._volume = volume
        speaker = self._speaker
        if speaker is not None:
            speaker.volume = volume

    def latest(self):
        return self._seq, self._frame

    def _wanted(self) -> bool:
        return self._clock() - self._asked < IDLE

    def _over(self) -> bool:
        """The video that is being read is not to be played any further."""
        return not self._wanted() or self._skipped or self._again

    def _hush(self) -> None:
        speaker = self._speaker
        if speaker is not None:
            speaker.reset()

    def _run(self) -> None:
        import av

        try:
            while self._wanted():
                wait = RETRY
                try:
                    self.state = "opening"
                    if self._skipped:
                        self._skipped, self._resume = False, 0.0
                    self._again = False
                    address, options, title, *more = self._resolve(self.source)
                    self.title = title or self.title
                    try:
                        with av.open(address, options=options, timeout=OPEN_TIMEOUT) as container:
                            self.opened += 1
                            if self._play(container, address, options, more[0] if more else None):
                                self._resume = 0.0           # its end: from the beginning again, or the next one
                                if self._on_end:
                                    self._on_end()
                    except Exception:                        # noqa: BLE001
                        if self._on_error:                   # this one will not play: on to the next
                            self._on_error()
                            self._resume, wait = 0.0, RETRY_NEXT
                        raise
                except Exception as e:                       # noqa: BLE001  (whatever a site or a file can do wrong)
                    self.state, self.error, self.link_failed = "failed", brief(e), isinstance(e, LinkError)
                    until = self._clock() + wait
                    while self._wanted() and not self._skipped and self._clock() < until:
                        time.sleep(0.05)
        finally:
            speaker, self._speaker = self._speaker, None
            if speaker is not None:
                speaker.close()
            self.state = "idle"

    def _speaker_now(self):
        """What plays the sound, opened when it is first wanted. None: this video plays without."""
        if not self.sound or self._make_speaker is None:
            speaker, self._speaker = self._speaker, None
            if speaker is not None:                          # no longer wanted: Windows gets it back
                speaker.close()
            return None
        if self._speaker is None or self._speaker.broken:
            if self._speaker is not None:                    # its device failed (headphones pulled out): a new one
                self._speaker.close()
            try:
                self._speaker = self._make_speaker()
            except OSError as e:                             # a PC with nothing to play sound on
                self._speaker, self.silent = None, brief(e)
                return None
        self._speaker.reset()
        self._speaker.volume = self._volume
        return self._speaker

    def _feed(self, address: str, options: dict, start: float, speaker, epoch: int, feeding: list) -> None:
        """Thread: read the sound from an address of its own and hand it to the speaker, a little ahead of
        what is heard. Ends with the video (the speaker is reset then, which ends this `epoch`)."""
        import av

        try:
            with av.open(address, options=options, timeout=OPEN_TIMEOUT) as container:
                if not container.streams.audio:
                    self.silent = "this video has no sound"
                    return
                if start > 1.0:
                    container.seek(int(start * 1_000_000))
                voice = _Voice(speaker, epoch, start)
                for frame in container.decode(container.streams.audio[0]):
                    if not voice.say(frame):
                        return
                    while speaker.ahead() > SOUND_AHEAD and speaker.epoch == epoch:
                        time.sleep(0.02)
                voice.say(None)
        except Exception as e:                               # noqa: BLE001
            self.silent = brief(e)
        finally:
            feeding.clear()

    def _play(self, container, address: str, options: dict, sound_address: str | None) -> bool:
        """Play from where we were. True at the video's end, False when nobody wants frames any more or
        another video was asked for."""
        import av

        video = container.streams.video[0]
        video.thread_type = "AUTO"
        self.duration = float(container.duration or 0) / 1_000_000
        start = self._resume
        speaker, voice, feeding = self._speaker_now(), None, []
        streams = [video]
        if speaker is not None:
            self.silent = ""
            if sound_address is None and not container.streams.audio:
                self.silent, speaker = "this video has no sound", None
            elif sound_address is None and not read_twice(address):
                # the sound is in the same stream as the picture and is read with it: to have enough of it
                # ready, the reading runs ahead of the picture, and the frames wait in `pending`
                voice = _Voice(speaker, speaker.epoch, start)
                streams.append(container.streams.audio[0])
            else:                                            # an address of its own, or the same file once more
                feeding.append(threading.Thread(target=self._feed, name="video sound", daemon=True, args=(
                    sound_address or address, options if sound_address else {}, start, speaker, speaker.epoch, feeding)))
                feeding[0].start()
        if start > 1.0:
            container.seek(int(start * 1_000_000))           # lands on the key frame before; the frames up to
        pending: deque = deque()                             # `start` are decoded and not shown
        room = max(4, min(PENDING, PENDING_BYTES // max(1, video.width * video.height * 3 // 2)))
        base, began, shown = None, self._clock(), False

        def show(frame) -> None:
            nonlocal shown
            self._frame, self._seq, shown = frame, self._seq + 1, True
            self.position = self._resume = frame.time
            self.state, self.error = "playing", ""

        def present(final: bool) -> bool:
            """Show the frames that are due. True: read on; False: this video is over for us."""
            nonlocal base
            while pending:
                if self._over():
                    return False
                now, at = self._clock(), pending[0].time
                if speaker is not None and now - self._asked > HUSH:
                    speaker.pause()                          # nobody is looking: the sound waits, and with it the picture
                heard = speaker.position() if speaker is not None else None
                if heard is not None:
                    base = (now, heard + LEAD)
                elif base is None:
                    if (voice is not None or feeding) and now - began < SOUND_WAIT:
                        # the sound has not begun yet: the picture waits for it, so that they start together
                        if not shown:
                            show(pending[0])
                        if voice is not None and not final and len(pending) < room:
                            return True
                        time.sleep(0.01)
                        continue
                    base = (now, at)
                wait = at - (base[1] + now - base[0])        # seconds until the frame is due
                if wait > 0:
                    if voice is not None and not final and len(pending) < room and speaker.ahead() < SOUND_AHEAD:
                        return True                          # time to read more of the sound
                    time.sleep(min(wait, 0.05))
                    continue
                if wait < -BEHIND:
                    if heard is None:
                        base = (now, at)                     # the reading could not keep up: the clock is set anew
                    else:                                    # behind the sound: leave out what is no longer due
                        while len(pending) > 1 and pending[1].time <= base[1]:
                            pending.popleft()
                show(pending.popleft())
            return True

        try:
            for frame in container.decode(*streams):
                if self._over():
                    return False
                if isinstance(frame, av.AudioFrame):
                    voice.say(frame)
                    continue
                at = frame.time
                if at is None or at < start - 0.05:
                    continue
                pending.append(frame)
                if not present(final=False):
                    return False
            if voice is not None:
                voice.say(None)
            if not present(final=True):
                return False
            until = self._clock() + 2.0                      # the last of the sound
            while speaker is not None and speaker.ahead() > 0.02 and not self._over() and self._clock() < until:
                time.sleep(0.02)
            return True
        finally:
            if speaker is not None:
                speaker.reset()


class _Voice:
    """Decoded sound on its way to the speaker: brought to the speaker's form, each piece with its time."""

    def __init__(self, speaker, epoch: int, start: float):
        import av

        self._speaker, self._epoch, self._start = speaker, epoch, start
        self._form = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
        self._at: float | None = None            # the time the next piece belongs to

    def say(self, frame) -> bool:
        """Hand a frame of sound over (None: what the resampler still holds). False: nobody listens any more."""
        if frame is not None:
            if frame.time is None or frame.time < self._start - 0.05:
                return self._speaker.epoch == self._epoch
            if self._at is None or abs(frame.time - self._at) > 0.1:
                self._at = frame.time            # the beginning, or a gap in the sound
        if self._at is None:
            return self._speaker.epoch == self._epoch
        for piece in self._form.resample(frame):
            data = bytes(piece.planes[0])[:piece.samples * FRAME]
            if not self._speaker.write(data, self._at, self._epoch):
                return False
            self._at += piece.samples / RATE
        return True


SOUND = Setting("sound", "Sound", "choice", "0", SOUND_LEVELS, live=True,
                hint="From the PC's speakers, in time with the picture, while the mode is on the cooler. How loud is "
                     "deephack's own volume in Windows: the rest of the PC stays as loud as it is.")
MUTE_KEY = Setting("mute_key", "Key to mute", "shortcut", "", action="mute",
                   hint="Sound off and on again, whatever window is in front. The Mute button does the same.")


class Video(Mode):
    id = "video"
    title = "Video"
    description = "Plays YouTube or a video file"
    glyph = ""
    quality = QUALITY
    needs_direct = True
    actions = (("mute", "Mute"),)
    settings = (
        Setting("source", "Video", "file", "", placeholder="https://www.youtube.com/watch?v=…",
                hint="A YouTube link (most other video sites work too), or a video file on this PC. It is read as it "
                     "plays, not downloaded, and starts again at its end."),
        Setting("fit", "Picture", "choice", "fill", (("fill", "Fill the screen"), ("fit", "The whole picture, with bars")),
                hint="A wide picture fills the screen with its middle."),
        SOUND, MUTE_KEY,
    )

    def __init__(self, values: dict | None = None, player=Player):
        super().__init__(values)
        self._make_player = player
        self._player: Player | None = None
        self._shown = 0                          # number of the frame handed over last
        self._still = False                      # the one picture for a cooler that cannot be drawn on has been made
        self._muted = False                      # by the Mute button or key: until it is pressed again

    def _source(self) -> str:
        """What to play; nothing: the mode says what it wants instead."""
        source = self.values["source"].strip().strip('"')
        if not source:
            self.status, self.placeholder = "No video chosen", ("Video", "Paste a YouTube link or choose a file")
        return source

    def _new_player(self, source: str) -> Player:
        lines = LINES.get(self.values["fit"], 720)
        return self._make_player(source, lambda address: resolve(address, lines, bool(self._player and self._player.sound)))

    def _level(self) -> float:
        """How loud, 0 to 1, as the setting says. 0: no sound is wanted."""
        try:
            return min(100, max(0, int(self.values.get("sound") or 0))) / 100
        except ValueError:
            return 0.0

    def _sound_note(self) -> str:
        """What the status line says about the sound, when there is something to say."""
        player, level = self._player, self._level()
        if player is None or not level or not player.sound:
            return ""
        if player.silent:
            return f" · no sound ({player.silent})"
        return " · muted" if self._muted else ""

    def act(self, action: str) -> None:
        if action == "mute":
            self._muted = not self._muted
            if self._player is not None:
                self._player.volume = 0.0 if self._muted else self._level()

    def _name(self, source: str) -> str:
        return self._player.title or ("Video" if "://" in source else Path(source).stem)

    async def frame(self) -> Image.Image | None:
        source = self._source()
        self.tick = TICK if self.direct else None
        if not source:
            return None
        if self._player is None:
            self._player = self._new_player(source)
        player = self._player
        if not self.direct and self._still:
            return None
        level = self._level()
        player.set_sound(bool(level) and self.direct and self.audible)
        player.volume = 0.0 if self._muted else level
        player.ask()
        seq, frame = player.latest()
        name = self._name(source)
        if player.state == "failed" and (frame is None or seq == self._shown):
            hint = ". If links have stopped working: Settings, YouTube links, Update now" if player.link_failed else ""
            self.status, self.placeholder = f"Cannot play it: {player.error}", (name, f"Cannot play it: {player.error}{hint}")
            return None
        if frame is None:
            self.status, self.placeholder = f"Opening {name}…", (name, "Opening…")
            return None
        if not self.direct:                      # one picture, stored once: that firmware cannot be drawn on
            self._still = True
            player.stop()
            self.status = "One picture: playing it needs direct drawing"
            return await asyncio.to_thread(picture, frame, self.values["fit"])
        self._still = False
        length = f" of {clock_text(player.duration)}" if player.duration else ""
        self.status = f"{name} · {clock_text(player.position)}{length}{self._sound_note()}"
        if seq == self._shown:
            return None
        self._shown = seq
        return await asyncio.to_thread(picture, frame, self.values["fit"])

    async def close(self) -> None:
        if self._player is not None:
            self._player.stop()
