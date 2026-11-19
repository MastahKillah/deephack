"""What the PC is playing right now, as two numbers: how strong the bass is and how strong the rest is.

Windows hands any program a copy of what a sound device plays ("loopback capture", WASAPI). No
microphone, no permission prompt and no extra package: a few COM calls through ctypes. The samples are
split into bass (below about 150 Hz, where kick drums live) and everything above, and each part is
followed by an envelope that jumps up with a beat and falls back quickly.

Music is mastered at very different volumes and the PC's volume changes too, so both envelopes are
given relative to their own recent maximum (the last several seconds): 0.0 is silence, 1.0 as loud as
it lately got. The system's peak meter would be simpler but is useless here: with modern music it
reads 1.00 nearly all the time.

Listener runs on a small thread while someone keeps asking, and stops a few seconds after.
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
import uuid
from ctypes import POINTER, c_void_p, pointer

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
                if ch == 1:
                    out.extend(s * scale for s in samples)
                else:                                  # the first two channels are left and right
                    out.extend((samples[i] + samples[i + 1]) * 0.5 * scale for i in range(0, n * ch, ch))
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


class Listener:
    """The current (bass, rest) levels, kept up to date in the background while someone keeps asking."""

    def __init__(self, capture=Loopback):
        self._capture = capture
        self._lock = threading.Lock()
        self._levels = (0.0, 0.0)
        self._asked = 0.0
        self._thread: threading.Thread | None = None
        self.available = True                    # False while there is no sound output to listen to

    def levels(self) -> tuple[float, float]:
        with self._lock:
            self._asked = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="deephack-listener")
                self._thread.start()
            return self._levels

    def _run(self) -> None:
        capture, opened, bands, last = None, 0.0, Bands(), time.monotonic()
        try:
            while time.monotonic() - self._asked < IDLE_SECONDS:
                now = time.monotonic()
                try:
                    if capture is None or (now - opened > REOPEN_SECONDS and self._levels == (0.0, 0.0)):
                        if capture is not None:          # only while silent, so a song is never interrupted
                            capture.close()
                        capture, opened = self._capture(), now
                    levels = bands.feed(capture.read(), capture.rate, now - last)
                    last, self.available = now, True
                    with self._lock:
                        self._levels = levels
                except OSError:
                    capture, self.available = None, False
                    with self._lock:
                        self._levels = (0.0, 0.0)
                    time.sleep(1.0)
                time.sleep(POLL_SECONDS)
        finally:
            if capture is not None:
                capture.close()


LISTENER = Listener()
