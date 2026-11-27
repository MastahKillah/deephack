"""Where in a music video the song is, found by its sound.

A music video is often longer than its track: a scene before the song, a pause in the middle, credits
after it. Played from the track's position it would run beside the music. So deephack listens: it
knows what the PC plays (audio.py, the sound output's own copy, no microphone) and it can read the
video's sound, and it looks for the last seconds it heard in the video. Where they are says by how much
the video is ahead of the music: its `lead`.

What is compared is not the sound but its **loudness in eight bands** between 100 and 3600 Hz, fifty
times a second (`Loudness`): which notes are loud when. ffmpeg's filters make it (a band-pass for each
band, its square, a low-pass of 6 Hz), so no Fourier transform is needed here, and a video's whole
sound is done in under a second. Each band is taken as its logarithm less its average over the second
before, which leaves how it rises and falls and drops how loud the PC is set.

A piece that was heard is laid over every place in the video it could be (`Whole`): at each, how alike
the two are is one number from -1 to 1. The place counts when it is alike enough (`ALIKE`) and clearly
more so than any other place (`OVER`): music repeats itself, and a chorus looks like the chorus before.
And it is believed only when three looks in a row have found the same place (`LOOKS`).

`Follower` does that every two seconds while a track plays. Once it knows the place it only looks near
where the place should be by now, so a video whose sound runs a little slower than the track is
followed as it drifts; where the video has a scene put in, or leaves a part out, the place is looked for
close by first. A video whose sound is another recording (a live cut, another mix) is never found, and
is not shown.

Tried on fifty well-known tracks (issue #56), with an upload of the track standing in for what the PC
plays: 37 had a video and an upload to compare, and 29 of them were followed through half the track or
more. Of the 5,742 seconds where a second way of looking knew the place, this one had it right for
5,566 and wrong for 12, at cuts inside videos. Played against another track's video, 4,479 looks
believed nothing. The place was first known 16 to 24 seconds into a track, for four tracks in five.
Videos of the track's own length turned out to be half a second to two seconds off, and two of them
slower than their track by a third of a percent: following by sound puts those right too. What has not
been tried is a real player playing: the uploads went in as samples.
"""
from __future__ import annotations

import math
import threading
import time
from array import array
from collections import deque
from fractions import Fraction
from itertools import accumulate

from . import filters, ytdlp

RATE = 8000                 # samples a second the sound is brought to before its loudness is taken
PER = 50                    # rows of loudness a second
BANDS, LOW, HIGH = 8, 100.0, 3600.0          # the bands, evenly spaced by pitch
SMOOTH = 6.0                # Hz: how quickly a band's loudness may change (ffmpeg's low-pass on its square)
FLOOR = 1e-9                # no band is quieter than this (its logarithm must be a number)
WINDOW = 12.0               # seconds of what was heard that are looked for in the video
EVERY = 2.0                 # seconds from one look to the next
ALIKE = 0.6                 # how alike a piece and a place must be for the place to count ...
OVER = 0.15                 # ... and by how much more alike than the best other place
KEEP = 0.5                  # how alike they must be to stay at the place that is known
NEAR = 1.0                  # seconds around where the place should be that are looked at first
WIDE = 20.0                 # ... and then, when it is not there (the video put a scene in, or left a part out)
HOLD = 8.0                  # seconds the last place is kept while it is looked for again
PATIENCE, SELDOM = 15, 3    # looks without a place after which they come more seldom, and how many times more
GUARD = 0.5                 # places within this many seconds of the best one are the same place, not another
LOOKS = 3                   # looks in a row that must say the same place before it is believed. (Two are not
                            # enough: their pieces are ten seconds the same. In the trial two tracks were "found"
                            # in another track's video that way, and none with three.)
SAME = 0.5                  # ... the same to within this many seconds
SLACK = 10.0                # the song is looked for no earlier in a video than this before its beginning, and no later
                            # than this after where it would begin if all that the video is longer by came first
GAP = 0.25                  # seconds without sound from the PC: what was heard before is no longer one piece with it
ANCHORS = 400               # reads of the capture by which the time of what is heard is known (some eight seconds)
NETWORK = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5"}
OPEN_TIMEOUT = (15.0, 15.0)


class Loudness:
    """Sound in, rows of loudness out: PER rows a second, BANDS numbers a row, each a band's level as it is
    compared (see the module's text). Feed it decoded frames (`template`: the stream they come from) or
    samples as they were captured (mono floats at `rate`)."""

    def __init__(self, template=None, rate: int = 48000):
        import av

        self._av, self._rate, self._fed = av, rate, 0
        graph = av.filter.Graph()
        if template is not None:
            source = graph.add_abuffer(template=template)
        else:
            source = graph.add_abuffer(format="flt", sample_rate=rate, layout="mono", time_base=Fraction(1, rate))
        head = [source, graph.add("aresample", str(RATE)), graph.add("aformat", "sample_fmts=flt:channel_layouts=mono"),
                graph.add("asplit", str(BANDS))]
        for a, b in zip(head, head[1:]):
            a.link_to(b)
        edges = [LOW * (HIGH / LOW) ** (i / BANDS) for i in range(BANDS + 1)]
        self._sinks = []
        for i, (low, high) in enumerate(zip(edges, edges[1:])):
            chain = [graph.add("bandpass", f"f={math.sqrt(low * high):.1f}:width_type=h:w={high - low:.1f}"),
                     graph.add("aeval", "val(0)*val(0):c=same"),       # (c=same: or the filters after it pass it by)
                     graph.add("lowpass", f"f={SMOOTH}:poles=2"),
                     graph.add("aresample", str(PER)),
                     graph.add("aformat", "sample_fmts=flt"),
                     graph.add("abuffersink")]
            head[-1].link_to(chain[0], i, 0)
            for a, b in zip(chain, chain[1:]):
                a.link_to(b)
            self._sinks.append(chain[-1])
        graph.configure()
        self._graph, self._source = graph, source
        self._made = True
        self._bands = [array("f") for _ in range(BANDS)]         # what has come out of each band and is not a row yet
        self._last: deque[list[float]] = deque()                 # the logarithms of the last second's rows ...
        self._sums = [0.0] * BANDS                               # ... and their sum, band by band

    def __del__(self):
        if getattr(self, "_made", False):        # its graph is not freed with it (see filters.py): 1.1 MB, and
            filters.dropped()                    # the Ear makes a new one after every silence

    def _rows(self) -> list[list[float]]:
        for band, sink in zip(self._bands, self._sinks):
            while True:
                try:
                    frame = sink.pull()
                except (BlockingIOError, EOFError):              # nothing more for now, or for good
                    break
                band.frombytes(bytes(frame.planes[0])[:frame.samples * 4])
        ready = min(len(band) for band in self._bands)
        out = []
        for values in zip(*(band[:ready] for band in self._bands)):
            logs = [math.log10(v if v > FLOOR else FLOOR) for v in values]
            self._last.append(logs)
            self._sums = [s + v for s, v in zip(self._sums, logs)]
            if len(self._last) > PER + 1:
                self._sums = [s - v for s, v in zip(self._sums, self._last.popleft())]
            n = len(self._last)
            out.append([v - s / n for v, s in zip(logs, self._sums)])
        for band in self._bands:
            del band[:ready]
        return out

    def feed(self, frame) -> list[list[float]]:
        """A decoded frame of sound; the rows that are ready."""
        self._source.push(frame)
        return self._rows()

    def hear(self, samples: list[float]) -> list[list[float]]:
        """Captured samples (mono floats at the rate given); the rows that are ready."""
        if not samples:
            return []
        frame = self._av.AudioFrame(format="flt", layout="mono", samples=len(samples))
        frame.planes[0].update(array("f", samples).tobytes())
        frame.sample_rate, frame.pts, frame.time_base = self._rate, self._fed, Fraction(1, self._rate)
        self._fed += len(samples)
        return self.feed(frame)

    def end(self) -> list[list[float]]:
        """No more sound: the rows that were still on their way."""
        self._source.push(None)
        return self._rows()


def _piece(rows: list[list[float]]) -> tuple[list[float], float]:
    """Rows as one list, each band less its mean over them, and that list's length (as a vector)."""
    n = len(rows)
    means = [sum(band) / n for band in zip(*rows)]
    flat = [v - m for row in rows for v, m in zip(row, means)]
    return flat, math.sqrt(math.sumprod(flat, flat))


def _spread(rows: list[list[float]], n: int) -> list[float]:
    """For every place a piece of n rows could lie in these rows: how much the rows there differ from their
    mean, all bands together (what a piece's likeness to them is measured against)."""
    total = [0.0] * (len(rows) - n + 1)
    for band in zip(*rows):
        sums = [0.0, *accumulate(band)]
        squares = [0.0, *accumulate(v * v for v in band)]
        total = [t + (q1 - q0) - (s1 - s0) ** 2 / n
                 for t, s0, s1, q0, q1 in zip(total, sums, sums[n:], squares, squares[n:])]
    return total


class Whole:
    """A video's sound as rows of loudness: what a piece that was heard is looked for in."""

    def __init__(self, rows: list[list[float]]):
        self.rows = rows
        self._flat = [v for row in rows for v in row]
        self._half = [v for row in rows[::2] for v in row]       # every other row: for the look at every place
        self._spreads: dict[tuple[int, int], list[float]] = {}

    @property
    def seconds(self) -> float:
        return len(self.rows) / PER

    def _spread(self, n: int, step: int) -> list[float]:
        if (n, step) not in self._spreads:
            if len(self._spreads) > 6:
                self._spreads.clear()
            self._spreads[(n, step)] = _spread(self.rows[::step], n)
        return self._spreads[(n, step)]

    def _alike(self, flat: list[float], length: float, n: int, places, step: int = 1) -> list[float]:
        """How alike the piece is to the rows at each of the places, -1 to 1."""
        whole, spread, width = (self._flat if step == 1 else self._half), self._spread(n, step), n * BANDS
        if length <= 0:
            return [0.0 for _ in places]
        return [math.sumprod(flat, whole[at * BANDS:at * BANDS + width]) / (length * math.sqrt(spread[at]) or 1.0)
                if spread[at] > 1e-9 else 0.0 for at in places]

    def around(self, rows: list[list[float]], at: int, reach: int) -> tuple[int, float, float] | None:
        """The best place for the piece within `reach` rows of `at`: (the row it begins at, how alike, how
        alike the best place is that is not the same one). None where the piece cannot lie there at all."""
        n = len(rows)
        low, high = max(0, at - reach), min(len(self.rows) - n, at + reach)
        if n < 2 or low > high:
            return None
        flat, length = _piece(rows)
        scores = self._alike(flat, length, n, range(low, high + 1))
        best = max(range(len(scores)), key=scores.__getitem__)
        guard = round(GUARD * PER)
        others = scores[:max(0, best - guard)] + scores[best + guard + 1:]
        return low + best, scores[best], max(others, default=-1.0)

    def everywhere(self, rows: list[list[float]], first: int = 0, last: int | None = None) -> tuple[int, float, float] | None:
        """The best place for the piece in the whole video (or from row `first` to row `last` of it), as
        `around` gives it. Every other row of both is compared first, at every other place, and the best
        place and the best other one are then looked at closely: a quarter of the work, for the same answer."""
        n = len(rows)
        if n < 4 or len(self.rows) < n:
            return None
        half = rows[::2]
        low = max(0, first) // 2
        high = (len(self.rows) + 1) // 2 - len(half)
        high = high if last is None else min(high, last // 2)
        if low > high:
            return None
        flat, length = _piece(half)
        scores = self._alike(flat, length, len(half), range(low, high + 1), step=2)
        best = max(range(len(scores)), key=scores.__getitem__)
        guard = round(GUARD * PER / 2)
        rest = [(score, i) for i, score in enumerate(scores) if abs(i - best) > guard]
        found = self.around(rows, 2 * (low + best), 2)
        if found is None:
            return None
        other = self.around(rows, 2 * (low + max(rest)[1]), 2) if rest else None
        return found[0], found[1], other[1] if other else -1.0


class Follower:
    """Keeps `lead`: by how many seconds the video is ahead of the music (the video's time less the
    track's), None while that is not known. `look` at what was heard every EVERY seconds. `seconds`: the
    track's length, if it is known: a video that is half a minute longer has its song begin somewhere in
    that half minute, and it is not looked for anywhere else (give or take SLACK)."""

    def __init__(self, whole: Whole, seconds: float | None = None):
        self.whole = whole
        self.lead: float | None = None
        self._leads = (-SLACK, max(0.0, whole.seconds - seconds) + SLACK) if seconds else None
        self._maybe: list[float] = []            # the place as the last looks found it: it counts after LOOKS of them
        self._missed = 0                         # looks in a row that did not find the place that was known

    def look(self, rows: list[list[float]], at: float) -> float | None:
        """`rows`: the last WINDOW seconds that were heard; `at`: where in the track the music was when the
        last of them was heard. The lead as it is known after this look."""
        whole, start = self.whole, at - len(rows) / PER
        if self.lead is not None:
            expected = round((start + self.lead) * PER)
            near = whole.around(rows, expected, round(NEAR * PER))
            if near is not None and near[1] >= KEEP:
                self.lead, self._missed = near[0] / PER - start, 0
                return self.lead
            wide = whole.around(rows, expected, round(WIDE * PER))
            if wide is not None and wide[1] >= ALIKE and wide[1] - wide[2] >= OVER:
                self.lead, self._missed = wide[0] / PER - start, 0
                return self.lead
            self._missed += 1
            if self._missed * EVERY < HOLD:
                return self.lead
            self.lead, self._maybe = None, []
        if self._leads is None:
            found = whole.everywhere(rows)
        else:
            found = whole.everywhere(rows, round((start + self._leads[0]) * PER), round((start + self._leads[1]) * PER))
        if found is not None and found[1] >= ALIKE and found[1] - found[2] >= OVER:
            lead = found[0] / PER - start
            self._maybe = [was for was in self._maybe if abs(lead - was) <= SAME] + [lead]
            if len(self._maybe) >= LOOKS:
                self.lead, self._maybe, self._missed = lead, [], 0
        else:
            self._maybe = []
        return self.lead


def rows_of(address: str, options: dict | None = None) -> list[list[float]]:
    """The loudness of all the sound at an address ffmpeg can open (a file, a stream)."""
    import av

    with av.open(address, options=options or {}, timeout=OPEN_TIMEOUT) as container:
        if not container.streams.audio:
            raise ValueError("this video has no sound")
        stream = container.streams.audio[0]
        loudness, rows = Loudness(template=stream), []
        for frame in container.decode(stream):
            rows += loudness.feed(frame)
        return rows + loudness.end()


def sound_of(link: str) -> Whole:
    """The sound of a video on the net, read in one go: yt-dlp says where its smallest sound stream is.
    Five or six seconds for a video of four minutes (a second to ask, the rest to read it)."""
    yt_dlp = ytdlp.module()
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "format": "worstaudio/worst"}) as ydl:
        info = ydl.extract_info(link, download=False)
    if info.get("entries"):
        info = next(iter(info["entries"]))
    part = (info.get("requested_formats") or [info])[0]
    headers = "".join(f"{name}: {value}\r\n" for name, value in (part.get("http_headers") or info.get("http_headers") or {}).items())
    return Whole(rows_of(part["url"], {**NETWORK, "headers": headers}))


class Ear:
    """The last seconds of what the PC played, as rows of loudness, each with the time it was heard.
    `hear` is called with every handful of samples as they are captured (audio.Listener does, from its
    thread); `last` hands out the newest rows. A silence breaks it off: what comes after starts anew, so
    that a piece is always one stretch of sound."""

    def __init__(self, listener=None, keep: float = WINDOW + 2.0):
        self._listener, self._tapped = listener, False
        self._lock = threading.Lock()
        self._rows: deque[list[float]] = deque(maxlen=round(keep * PER))
        self._loudness: Loudness | None = None
        self._rate, self._heard = 0, float("-inf")
        self._count, self._samples = 0, 0                        # rows and samples since it began anew
        # ... and when that was, as each of the last reads says it (the time of the read less the sound up to it).
        # A read that comes late says a later time, so the earliest is taken; and only of the last reads, since a
        # sound card's clock and the PC's drift apart over an hour of music.
        self._began: deque[float] = deque(maxlen=ANCHORS)

    def hear(self, samples: list[float], rate: int, now: float) -> None:
        if not samples:
            return
        with self._lock:
            if self._loudness is None or rate != self._rate or now - self._heard > GAP:
                self._loudness, self._rate = Loudness(rate=rate), rate
                self._rows.clear()
                self._began.clear()
                self._count, self._samples = 0, 0
            self._heard = now
            self._samples += len(samples)
            self._began.append(now - self._samples / rate)
            rows = self._loudness.hear(samples)
            self._rows.extend(rows)
            self._count += len(rows)

    def ask(self) -> None:
        """Keep listening (call it now and then: the listener stops a few seconds after nobody asks)."""
        listener = self._listener
        if listener is None:
            from . import audio

            listener = self._listener = audio.LISTENER
        if not self._tapped:
            listener.taps.append(self.hear)
            self._tapped = True
        listener.ask()

    def last(self, seconds: float = WINDOW) -> tuple[list[list[float]], float] | None:
        """(the rows of the last `seconds`, the time the last of them was heard, by time.monotonic()). None
        while there is not that much sound in one stretch."""
        n = round(seconds * PER)
        with self._lock:
            if len(self._rows) < n:
                return None
            rows = list(self._rows)[-n:] if n else []
            return rows, min(self._began, default=0.0) + self._count / PER


EAR = Ear()


class Place:
    """Follows one track in its video, in a thread of its own: reads the video's sound, then looks every
    EVERY seconds for what the PC played last. `lead`: by how many seconds the video is ahead of the music
    now, None while that is not known. `clock(when)`: where in the track the music was at that time
    (time.monotonic()), None when it was not playing. `seconds`: the track's length."""

    def __init__(self, link: str, clock, seconds: float | None = None, ear: Ear | None = None, sound=sound_of, pause=time.sleep):
        self.link, self._clock, self._ear, self._sound, self._pause = link, clock, ear or EAR, sound, pause
        self._seconds = seconds                  # the track's length (see Follower)
        self.lead: float | None = None
        self.state = "reading"                   # "reading" the video's sound, "listening", "failed" (then `error`)
        self.error = ""
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="song place", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed = True

    def _run(self) -> None:
        try:
            follower = Follower(self._sound(self.link), self._seconds)
        except Exception as e:                   # noqa: BLE001  (no net, no sound, YouTube saying no)
            self.state, self.error = "failed", str(e).splitlines()[0][:140] if str(e) else type(e).__name__
            return
        self.state, missed = "listening", 0
        while not self._closed:
            self._ear.ask()
            heard = self._ear.last()
            if heard is not None:
                rows, when = heard
                at = self._clock(when)
                if at is not None:
                    self.lead = follower.look(rows, at)
                    missed = 0 if self.lead is not None else missed + 1
            # (a look at every place in the video is a fifth of a second of work: a video that is not found
            # in half a minute is most likely another recording, and is looked into less often from then on)
            for tenth in range(round(EVERY * (SELDOM if missed >= PATIENCE else 1) / 0.1)):
                if self._closed:
                    return
                if tenth % 10 == 9:
                    self._ear.ask()              # (the listener stops when nobody has asked for some seconds)
                self._pause(0.1)
