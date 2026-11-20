"""What the PC is playing right now, as two numbers: how strong the bass is and how strong the rest is.

Windows hands any program a copy of what a sound device plays ("loopback capture", WASAPI). No
microphone, no permission prompt and no extra package: a few COM calls through ctypes. The samples are
split into bass (below about 150 Hz, where kick drums live) and everything above, and each part is
followed by an envelope that jumps up with a beat and falls back quickly.

Music is mastered at very different volumes and the PC's volume changes too, so both envelopes are
given relative to their own recent maximum (the last several seconds): 0.0 is silence, 1.0 as loud as
it lately got. The system's peak meter would be simpler but is useless here: with modern music it
reads 1.00 nearly all the time.

For a picture of the sound itself there is Spectrum: how loud each of 24 frequency bands is, from the
last 43 milliseconds of samples. It needs no FFT library: a band's strength is the size of two dot
products of the samples with a windowed sine and cosine at the band's frequency, and math.sumprod
does those in C (half a millisecond for all 24 bands).

Listener runs on a small thread while someone keeps asking, and stops a few seconds after.
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
import uuid
from collections import deque
from ctypes import POINTER, c_void_p, pointer
from itertools import repeat
from operator import add, mul

CLSID_ENUMERATOR = uuid.UUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_ENUMERATOR = uuid.UUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_AUDIO_CLIENT = uuid.UUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_CAPTURE_CLIENT = uuid.UUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
CLSCTX_ALL, E_RENDER, E_CONSOLE = 23, 0, 0
STREAM_LOOPBACK, BUFFER_SILENT = 0x00020000, 0x2
POLL_SECONDS = 0.02
IDLE_SECONDS = 5.0
REOPEN_SECONDS = 15.0          # the default output can change (headphones plugged in): look it up again
BASS_HZ = 150.0
FALL_SECONDS = 0.12            # an envelope halves in this time
RANGE_SECONDS = 6.0            # "as loud as it lately got" forgets at this rate
SILENCE = 1e-4
BANDS, BAND_LOW, BAND_HIGH = 24, 50.0, 12000.0   # the spectrum's bands, evenly spaced by pitch
WINDOW = 2048                  # samples the spectrum looks at: 43 ms at 48 kHz
WINDOW_CYCLES = 8              # a band looks at this many cycles of its frequency (fewer in the bass: not more
                               # than WINDOW samples; more in the treble: not fewer than 64), which makes each
                               # about as wide as the step to the next band
TILT = 0.3                     # music is quieter the higher it goes: a band counts for (its frequency) ** TILT more
CURVE = 0.5                    # ... and a band's level is (its share of the loudest) ** CURVE, so quiet ones show
PEAK_FALL_SECONDS = 0.5        # the mark a band's peak leaves halves its height in this time
QUIET_SECONDS = 0.06           # no samples for this long: nothing is playing, and the window fills with silence


class _WaveFormat(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("tag", ctypes.c_uint16), ("channels", ctypes.c_uint16), ("rate", ctypes.c_uint32),
                ("bytes_per_second", ctypes.c_uint32), ("block", ctypes.c_uint16), ("bits", ctypes.c_uint16),
                ("extra", ctypes.c_uint16)]


def _guid(value: uuid.UUID):
    return (ctypes.c_byte * 16).from_buffer_copy(value.bytes_le)


def _call(obj: c_void_p, index: int, *args) -> int:
    """Call method number `index` of a COM object (0 to 2 are IUnknown's); the arguments are ctypes values."""
    vtable = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *(type(a) for a in args))(vtable[index])(obj, *args)


def _release(obj: c_void_p | None) -> None:
    if obj:
        _call(obj, 2)


class Loopback:
    """A capture of the default sound output. read() returns the frames played since the last call,
    as mono floats, and the sample rate. Must be used from one thread."""

    def __init__(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)                 # fine if the thread already has COM set up
        ole32.CoTaskMemFree.argtypes = [c_void_p]
        self._client, self._capture = c_void_p(), c_void_p()
        enumerator, endpoint, fmt = c_void_p(), c_void_p(), POINTER(_WaveFormat)()
        try:
            if ole32.CoCreateInstance(pointer(_guid(CLSID_ENUMERATOR)), None, CLSCTX_ALL, pointer(_guid(IID_ENUMERATOR)),
                                      pointer(enumerator)) or not enumerator:
                raise OSError("no audio device enumerator")
            if _call(enumerator, 4, ctypes.c_int(E_RENDER), ctypes.c_int(E_CONSOLE), pointer(endpoint)) or not endpoint:
                raise OSError("no sound output device")
            if _call(endpoint, 3, pointer(_guid(IID_AUDIO_CLIENT)), ctypes.c_ulong(CLSCTX_ALL), c_void_p(),
                     pointer(self._client)) or not self._client:
                raise OSError("the sound output cannot be opened")
            if _call(self._client, 8, pointer(fmt)) or not fmt:                       # GetMixFormat
                raise OSError("the sound output has no format")
            self.rate, self.channels, self.bits = fmt.contents.rate, fmt.contents.channels, fmt.contents.bits
            self._block = fmt.contents.block
            if self.bits not in (16, 32):
                raise OSError(f"unsupported sample size {self.bits}")
            hr = _call(self._client, 3, ctypes.c_int(0), ctypes.c_ulong(STREAM_LOOPBACK), ctypes.c_longlong(2_000_000),
                       ctypes.c_longlong(0), fmt, c_void_p())                          # Initialize: shared, 0.2 s buffer
            if hr:
                raise OSError(f"loopback capture refused ({hr & 0xFFFFFFFF:#x})")
            if _call(self._client, 14, pointer(_guid(IID_CAPTURE_CLIENT)), pointer(self._capture)) or not self._capture:
                raise OSError("no capture service")
            if _call(self._client, 10):                                                # Start
                raise OSError("capture did not start")
        except OSError:
            self.close()
            raise
        finally:
            if fmt:
                ole32.CoTaskMemFree(ctypes.cast(fmt, c_void_p))
            _release(endpoint)
            _release(enumerator)

    def read(self) -> list[float]:
        out: list[float] = []
        while True:
            size = ctypes.c_uint32()
            if _call(self._capture, 5, pointer(size)):                                # GetNextPacketSize
                raise OSError("capture lost")
            if not size.value:
                return out
            data, frames, flags = POINTER(ctypes.c_byte)(), ctypes.c_uint32(), ctypes.c_ulong()
            if _call(self._capture, 3, pointer(data), pointer(frames), pointer(flags), c_void_p(), c_void_p()):
                raise OSError("capture lost")
            n = frames.value
            if flags.value & BUFFER_SILENT or not data:
                out.extend([0.0] * n)
            else:
                raw = ctypes.string_at(data, n * self._block)
                view = memoryview(raw).cast("f" if self.bits == 32 else "h")
                scale = 1.0 if self.bits == 32 else 1 / 32768
                ch = self.channels
                samples = view.tolist()
                if ch == 1:                            # (map, not a loop: this runs for every sample played)
                    out.extend(map(mul, samples, repeat(scale)))
                else:                                  # the first two channels are left and right
                    out.extend(map(mul, map(add, samples[0::ch], samples[1::ch]), repeat(0.5 * scale)))
            _call(self._capture, 4, ctypes.c_uint32(n))                               # ReleaseBuffer

    def close(self) -> None:
        if self._client:
            _call(self._client, 11)                                                    # Stop
        _release(self._capture)
        _release(self._client)
        self._capture = self._client = None


class Bands:
    """Splits samples into bass and the rest and follows both: (bass, rest), each 0.0 to 1.0 of its recent maximum."""

    def __init__(self):
        self._low = 0.0
        self._env = [0.0, 0.0]
        self._top = [SILENCE, SILENCE]
        self.levels = (0.0, 0.0)

    def feed(self, samples: list[float], rate: int, seconds: float) -> tuple[float, float]:
        """`seconds` is the time since the last call (the envelopes also fall while nothing plays)."""
        fall = 0.5 ** (seconds / FALL_SECONDS)
        forget = 0.5 ** (seconds / RANGE_SECONDS)
        energy = [0.0, 0.0]
        if samples:
            k = 1 - math.exp(-2 * math.pi * BASS_HZ / rate)       # one-pole low-pass
            low, bass, rest = self._low, 0.0, 0.0
            for s in samples:
                low += k * (s - low)
                high = s - low
                bass += low * low
                rest += high * high
            self._low = low
            energy = [math.sqrt(bass / len(samples)), math.sqrt(rest / len(samples))]
        out = []
        for i in (0, 1):
            self._env[i] = max(energy[i], self._env[i] * fall)
            self._top[i] = max(self._env[i], self._top[i] * forget, SILENCE)
            out.append(0.0 if self._env[i] < SILENCE else min(1.0, self._env[i] / self._top[i]))
        self.levels = (out[0], out[1])
        return self.levels


try:
    _dot = math.sumprod                          # Python 3.12
except AttributeError:
    def _dot(a, b):
        return sum(map(mul, a, b))


def band_frequencies(count: int = BANDS, low: float = BAND_LOW, high: float = BAND_HIGH) -> list[float]:
    return [low * (high / low) ** (i / (count - 1)) for i in range(count)]


class Spectrum:
    """How loud each frequency band is, 0.0 to 1.0, and where its peak mark is.

    Like Bands, the levels are relative to what was loudest lately, so quiet and loud music both fill the
    picture; a level jumps up with the sound and falls back quickly, its peak mark falls slowly."""

    def __init__(self, bands: int = BANDS):
        self.frequencies = band_frequencies(bands)
        self._rate = 0
        self._probes: list[tuple[int, list[float], list[float], float]] = []
        self._top = SILENCE
        self.levels = [0.0] * bands
        self.peaks = [0.0] * bands

    def _prepare(self, rate: int) -> None:
        """Per band: how many samples it looks at, a Hann-windowed cosine and sine of its frequency over
        them (scaled so that a full-scale tone gives 1), and what the band counts for."""
        self._rate, self._probes = rate, []
        for f in self.frequencies:
            n = min(WINDOW, max(64, round(WINDOW_CYCLES * rate / f)))
            window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
            scale = 2 / sum(window)
            turn = 2 * math.pi * f / rate
            self._probes.append((n, [w * scale * math.cos(turn * i) for i, w in enumerate(window)],
                                 [w * scale * math.sin(turn * i) for i, w in enumerate(window)], (f / BAND_LOW) ** TILT))

    def strengths(self, samples: list[float], rate: int) -> list[float]:
        """Each band's strength in the newest samples (at least WINDOW of them; fewer count as silence)."""
        if rate != self._rate:
            self._prepare(rate)
        if len(samples) < WINDOW:
            return [0.0] * len(self._probes)
        return [math.hypot(_dot(samples[-n:], cos), _dot(samples[-n:], sin)) * weight for n, cos, sin, weight in self._probes]

    def feed(self, samples: list[float], rate: int, seconds: float) -> tuple[list[float], list[float]]:
        """(levels, peaks) for the newest samples; `seconds` is the time since the last call."""
        strengths = self.strengths(samples, rate)
        loudest = max(strengths)
        self._top = max(loudest, self._top * 0.5 ** (seconds / RANGE_SECONDS), SILENCE)
        fall, peak_fall = 0.5 ** (seconds / FALL_SECONDS), 0.5 ** (seconds / PEAK_FALL_SECONDS)
        quiet = loudest < SILENCE
        for i, strength in enumerate(strengths):
            now = 0.0 if quiet else min(1.0, strength / self._top) ** CURVE
            level = max(now, self.levels[i] * fall)
            self.levels[i] = level if level > 0.004 else 0.0
            peak = max(level, self.peaks[i] * peak_fall)
            self.peaks[i] = peak if peak > 0.004 else 0.0
        return list(self.levels), list(self.peaks)


class Listener:
    """The current (bass, rest) levels, kept up to date in the background while someone keeps asking, and
    the newest samples for whoever wants the spectrum."""

    def __init__(self, capture=Loopback):
        self._capture = capture
        self._lock = threading.Lock()
        self._levels = (0.0, 0.0)
        self._asked = 0.0
        self._thread: threading.Thread | None = None
        self._samples: deque[float] = deque(maxlen=WINDOW)
        self._rate = 48000
        self._spectrum = Spectrum()
        self._spectrum_at = 0.0
        self.available = True                    # False while there is no sound output to listen to

    def _ask(self) -> None:
        self._asked = time.monotonic()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="deephack-listener")
            self._thread.start()

    def levels(self) -> tuple[float, float]:
        with self._lock:
            self._ask()
            return self._levels

    def spectrum(self) -> tuple[list[float], list[float]]:
        """(levels, peaks) of the frequency bands right now, each 0.0 to 1.0 (see Spectrum)."""
        with self._lock:
            self._ask()
            samples, rate = list(self._samples), self._rate
        now = time.monotonic()
        seconds, self._spectrum_at = min(1.0, now - self._spectrum_at), now
        return self._spectrum.feed(samples, rate, seconds)

    def _run(self) -> None:
        capture, opened, bands, last = None, 0.0, Bands(), time.monotonic()
        heard = last
        try:
            while time.monotonic() - self._asked < IDLE_SECONDS:
                now = time.monotonic()
                try:
                    if capture is None or (now - opened > REOPEN_SECONDS and self._levels == (0.0, 0.0)):
                        if capture is not None:          # only while silent, so a song is never interrupted
                            capture.close()
                        capture, opened = self._capture(), now
                    samples = capture.read()
                    levels = bands.feed(samples, capture.rate, now - last)
                    if samples:
                        heard = now
                    elif now - heard > QUIET_SECONDS:     # a paused player sends nothing at all: the window
                        samples = [0.0] * int((now - last) * capture.rate)   # must not keep its last sound
                    last, self.available = now, True
                    with self._lock:
                        self._levels, self._rate = levels, capture.rate
                        self._samples.extend(samples)
                except OSError:
                    capture, self.available = None, False
                    with self._lock:
                        self._levels = (0.0, 0.0)
                        self._samples.clear()
                    time.sleep(1.0)
                time.sleep(POLL_SECONDS)
        finally:
            if capture is not None:
                capture.close()


LISTENER = Listener()
