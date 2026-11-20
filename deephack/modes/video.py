"""Video: a YouTube link or a video file, playing on the cooler. Without sound: it is a screen.

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

Not done: sound, and the turn that phone videos ask for (one filmed upright plays on its side).
"""
from __future__ import annotations

import asyncio
import threading
import time
from fractions import Fraction
from pathlib import Path

from PIL import Image

from ..render import H, W
from .base import Mode, Setting

TICK = 0.06                 # seconds between pictures asked of the mode: a little more often than the cooler takes them
QUALITY = 65                # JPEG quality of its pictures. A frame of a crowd is 65 KB at 85, which the cooler takes eleven
                            # of a second; at 65 it is 47 KB, and fifteen
IDLE = 3.0                  # seconds without anybody asking for a frame: the stream is closed
BEHIND = 0.3                # a frame this late means the reading cannot keep up: the clock is set anew, nothing is skipped
RETRY = 5.0                 # seconds before a video that would not open is tried again
OPEN_TIMEOUT = (15.0, 15.0)     # seconds to open the address, and to wait for more of it
# what to ask YouTube for: H.264 when there is one (every ffmpeg decodes it, fast), and no more lines than needed
FORMATS = "bestvideo[height<={0}][vcodec^=avc1]/bestvideo[height<={0}]/best[height<={0}]/best"
LINES = {"fill": 720, "fit": 480}            # on the cooler the whole of deephack took 13 % of a CPU core for the
                                             # first and 10 % for the second
NETWORK = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5"}


def clock_text(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def is_file(source: str) -> bool:
    return "://" not in source and Path(source).expanduser().is_file()


def resolve(source: str, lines: int = 720) -> tuple[str, dict, str]:
    """Where to read the video from: (address, options for opening it, title). A file on this PC is its
    own address; for anything else yt-dlp asks the site (YouTube and a great many others) for a stream
    of at most that many lines."""
    if is_file(source):
        path = Path(source).expanduser()
        return str(path), {}, path.stem
    import yt_dlp                                            # a second to load: only when a link is played

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "format": FORMATS.format(lines)}) as ydl:
        info = ydl.extract_info(source, download=False)
    if info.get("entries"):                                  # a playlist after all: its first video
        info = next(iter(info["entries"]))
    headers = "".join(f"{name}: {value}\r\n" for name, value in (info.get("http_headers") or {}).items())
    return info["url"], {**NETWORK, "headers": headers}, info.get("title") or ""


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
    newest frame and its number. `state`: "idle", "opening", "playing" or "failed" (then `error` says why)."""

    def __init__(self, source: str, resolve=resolve, clock=time.monotonic):
        self.source, self._resolve, self._clock = source, resolve, clock
        self.title, self.duration, self.position = "", 0.0, 0.0
        self.state, self.error = "idle", ""
        self.opened = 0                          # times the video was opened: 1, and one more for every new start
        self._frame, self._seq = None, 0
        self._asked = float("-inf")
        self._resume = 0.0                       # seconds into the video to go on from
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def ask(self) -> None:
        with self._lock:
            self._asked = self._clock()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="video", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._asked = float("-inf")

    def latest(self):
        return self._seq, self._frame

    def _wanted(self) -> bool:
        return self._clock() - self._asked < IDLE

    def _run(self) -> None:
        import av

        while self._wanted():
            try:
                self.state = "opening"
                address, options, title = self._resolve(self.source)
                self.title = title or self.title
                with av.open(address, options=options, timeout=OPEN_TIMEOUT) as container:
                    self.opened += 1
                    if self._play(container):
                        self._resume = 0.0                   # its end: from the beginning again
            except Exception as e:                           # noqa: BLE001  (whatever a site or a file can do wrong)
                self.state, self.error = "failed", brief(e)
                until = self._clock() + RETRY
                while self._wanted() and self._clock() < until:
                    time.sleep(0.1)
        self.state = "idle"

    def _play(self, container) -> bool:
        """Play from where we were. True at the video's end, False when nobody wants frames any more."""
        video = container.streams.video[0]
        video.thread_type = "AUTO"
        self.duration = float(container.duration or 0) / 1_000_000
        start = self._resume
        if start > 1.0:
            container.seek(int(start * 1_000_000))           # lands on the key frame before; the frames up to
        base = None                                          # `start` are decoded and not shown
        for frame in container.decode(video):
            if not self._wanted():
                return False
            at = frame.time
            if at is None or at < start - 0.05:
                continue
            now = self._clock()
            if base is None:
                base = (now, at)
            wait = base[0] + (at - base[1]) - now
            if wait < -BEHIND:
                base = (now, at)
            elif wait > 0:
                time.sleep(min(wait, 1.0))
            self._frame, self._seq = frame, self._seq + 1
            self.position = self._resume = at
            self.state, self.error = "playing", ""
        return True


class Video(Mode):
    id = "video"
    title = "Video"
    description = "Plays YouTube or a video file"
    glyph = ""
    quality = QUALITY
    settings = (
        Setting("source", "Video", "file", "", placeholder="https://www.youtube.com/watch?v=…",
                hint="A YouTube link (most other video sites work too), or a video file on this PC. It is read as it "
                     "plays, not downloaded, and starts again at its end."),
        Setting("fit", "Picture", "choice", "fill", (("fill", "Fill the screen: the middle of a wide picture"),
                                                    ("fit", "The whole picture, with bars"))),
    )

    def __init__(self, values: dict | None = None, player=Player):
        super().__init__(values)
        self._make_player = player
        self._player: Player | None = None
        self._shown = 0                          # number of the frame handed over last
        self._still = False                      # the one picture for a cooler that cannot be drawn on has been made

    async def frame(self) -> Image.Image | None:
        source = self.values["source"].strip().strip('"')
        self.tick = TICK if self.direct else None
        if not source:
            self.status, self.placeholder = "No video chosen", ("Video", "Paste a YouTube link or choose a file")
            return None
        if self._player is None:
            lines = LINES.get(self.values["fit"], 720)
            self._player = self._make_player(source, lambda address: resolve(address, lines))
        player = self._player
        if not self.direct and self._still:
            return None
        player.ask()
        seq, frame = player.latest()
        name = player.title or ("Video" if "://" in source else Path(source).stem)
        if player.state == "failed" and (frame is None or seq == self._shown):
            self.status, self.placeholder = f"Cannot play it: {player.error}", (name, f"Cannot play it: {player.error}")
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
        self.status = f"{name} · {clock_text(player.position)}{length}"
        if seq == self._shown:
            return None
        self._shown = seq
        return await asyncio.to_thread(picture, frame, self.values["fit"])

    async def close(self) -> None:
        if self._player is not None:
            self._player.stop()
