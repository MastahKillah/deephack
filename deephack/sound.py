"""Sound from the PC's speakers, for the modes that play a video (modes/video.py).

`Speaker` takes sound as it is decoded (48 kHz, two channels, 16 bits) together with the time in the
video each piece belongs to, and knows which moment of the video is being heard right now
(`position()`): that is the clock the picture follows, so the two stay together however long it plays.

The playing itself is done by `WaveOut`: Windows' oldest way of making sound (winmm), which every
Windows has, needs nothing installed, and counts the samples it has played. It goes to whatever is the
standard output device. Its loudness (`volume`) is the one Windows keeps for deephack in its volume
mixer, so it changes at once, also for sound that is already on its way.

A PC without anything to play sound on: opening the `Speaker` raises OSError, and the video plays silent.
"""
from __future__ import annotations

import ctypes
import threading
from collections import deque

RATE, CHANNELS = 48_000, 2
FRAME = CHANNELS * 2                # bytes of one moment of sound: a 16 bit number for each channel
_DONE = 1                           # WHDR_DONE: a block has been played (or given back by a reset)
_MS, _SAMPLES, _BYTES = 1, 2, 4     # how waveOutGetPosition can tell the time


class _Format(ctypes.Structure):    # WAVEFORMATEX
    _pack_ = 1
    _fields_ = [("tag", ctypes.c_ushort), ("channels", ctypes.c_ushort), ("rate", ctypes.c_uint),
                ("bytes_a_second", ctypes.c_uint), ("frame", ctypes.c_ushort), ("bits", ctypes.c_ushort),
                ("extra", ctypes.c_ushort)]


class _Block(ctypes.Structure):     # WAVEHDR
    _fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_uint), ("recorded", ctypes.c_uint),
                ("user", ctypes.c_size_t), ("flags", ctypes.c_uint), ("loops", ctypes.c_uint),
                ("next", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class _Time(ctypes.Structure):      # MMTIME
    _pack_ = 1
    _fields_ = [("kind", ctypes.c_uint), ("value", ctypes.c_uint), ("more", ctypes.c_uint)]


class WaveOut:
    """The standard output device, through winmm. Not safe to use from two threads at once: `Speaker`
    sees to that."""

    def __init__(self, rate: int = RATE, channels: int = CHANNELS):
        self._mm = mm = ctypes.WinDLL("winmm")
        handle, pointer, number = ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint
        for name, arguments in (("waveOutOpen", (pointer, number, pointer, ctypes.c_size_t, ctypes.c_size_t, number)),
                                ("waveOutPrepareHeader", (handle, pointer, number)),
                                ("waveOutUnprepareHeader", (handle, pointer, number)),
                                ("waveOutWrite", (handle, pointer, number)),
                                ("waveOutGetPosition", (handle, pointer, number)),
                                ("waveOutSetVolume", (handle, number)),
                                ("waveOutReset", (handle,)), ("waveOutPause", (handle,)),
                                ("waveOutRestart", (handle,)), ("waveOutClose", (handle,))):
            getattr(mm, name).argtypes, getattr(mm, name).restype = arguments, number
        self._rate, self._frame = rate, channels * 2
        self._handle = ctypes.c_void_p()
        form = _Format(1, channels, rate, rate * self._frame, self._frame, 16, 0)          # 1: plain PCM
        self._do("waveOutOpen", ctypes.byref(self._handle), 0xFFFFFFFF, ctypes.byref(form), 0, 0, 0, own=False)
        self._blocks: deque = deque()            # handed to Windows and not given back yet: they must stay where they are
        self._seen = self._count = 0

    def _do(self, name: str, *arguments, own: bool = True) -> None:
        code = getattr(self._mm, name)(*((self._handle,) if own else ()), *arguments)
        if code:
            raise OSError(f"no sound: {name} failed ({code})")

    def _tidy(self) -> None:
        while self._blocks and self._blocks[0][0].flags & _DONE:
            block, _data = self._blocks.popleft()
            self._mm.waveOutUnprepareHeader(self._handle, ctypes.byref(block), ctypes.sizeof(block))

    def write(self, data: bytes) -> None:
        self._tidy()
        held = ctypes.create_string_buffer(data, len(data))
        block = _Block(ctypes.cast(held, ctypes.c_void_p), len(data), 0, 0, 0, 0, None, 0)
        self._do("waveOutPrepareHeader", ctypes.byref(block), ctypes.sizeof(block))
        self._blocks.append((block, held))
        self._do("waveOutWrite", ctypes.byref(block), ctypes.sizeof(block))

    def played(self) -> int:
        """Samples played since the last reset."""
        now = _Time(_SAMPLES, 0, 0)
        self._do("waveOutGetPosition", ctypes.byref(now), ctypes.sizeof(now))
        value = {_BYTES: now.value // self._frame, _MS: now.value * self._rate // 1000}.get(now.kind, now.value)
        self._count += (value - self._seen) & 0xFFFFFFFF         # the counter is 32 bits wide: a day of sound
        self._seen = value
        return self._count

    def reset(self) -> None:
        """Stop, and forget what was still to be played."""
        self._do("waveOutReset")
        self._tidy()
        self._seen = self._count = 0

    def pause(self) -> None:
        self._do("waveOutPause")

    def restart(self) -> None:
        self._do("waveOutRestart")

    def set_volume(self, volume: float) -> None:
        level = round(0xFFFF * min(1.0, max(0.0, volume)))
        self._do("waveOutSetVolume", level | level << 16)

    def close(self) -> None:
        self._mm.waveOutReset(self._handle)
        self._tidy()
        self._mm.waveOutClose(self._handle)


class Speaker:
    """Plays the sound it is given, and knows what of it is being heard.

    `write(data, at, epoch)`: sound that belongs `at` seconds into the video; `position()`: the moment of
    the video heard now, None while nothing sounds. `reset()` stops at once and starts a new `epoch`:
    what is written for an older one is not played, so a thread that has not yet noticed that its video
    is over cannot add to the next. May be used from several threads.

    When the device fails while playing (headphones pulled out), `broken` says how, and it is silent."""

    def __init__(self, device=None):
        self._device = device if device is not None else WaveOut()
        self._lock = threading.RLock()
        self.epoch, self.paused, self.broken = 0, False, ""
        self._written = 0                        # samples handed over since the last reset
        self._marks: deque = deque()             # (sample, seconds into the video): where the time made a jump
        self._until = 0.0                        # the time at which the sound written last ends
        self._volume = 1.0
        self._attempt(lambda: self._device.set_volume(1.0))

    def _attempt(self, call, otherwise=None):
        if self.broken:
            return otherwise
        try:
            return call()
        except OSError as e:
            self.broken = str(e)
            return otherwise

    def write(self, data: bytes, at: float, epoch: int) -> bool:
        with self._lock:
            if epoch != self.epoch or not data or self.broken:
                return False
            if not self._marks or abs(at - self._until) > 0.02:
                self._marks.append((self._written, at))
            self._attempt(lambda: self._device.write(data))
            samples = len(data) // FRAME
            self._written, self._until = self._written + samples, at + samples / RATE
            return not self.broken

    def _played(self) -> int:
        return min(self._written, self._attempt(self._device.played, self._written))

    def position(self) -> float | None:
        with self._lock:
            played = self._played()
            if played >= self._written:
                return None
            while len(self._marks) > 1 and self._marks[1][0] <= played:
                self._marks.popleft()
            sample, at = self._marks[0]
            return at + (played - sample) / RATE

    def ahead(self) -> float:
        """Seconds of sound handed over and not played yet."""
        with self._lock:
            return (self._written - self._played()) / RATE

    def reset(self) -> None:
        with self._lock:
            self.epoch += 1
            self._attempt(self._device.reset)
            self._written, self._until = 0, 0.0
            self._marks.clear()
            if self.paused:
                self.paused = False
                self._attempt(self._device.restart)

    def pause(self) -> None:
        with self._lock:
            if not self.paused:
                self.paused = True
                self._attempt(self._device.pause)

    def resume(self) -> None:
        with self._lock:
            if self.paused:
                self.paused = False
                self._attempt(self._device.restart)

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, volume: float) -> None:
        with self._lock:
            if volume != self._volume:
                self._volume = volume
                self._attempt(lambda: self._device.set_volume(volume))

    def close(self) -> None:
        with self._lock:
            self.epoch += 1
            self._attempt(self._device.close)
            self.broken = self.broken or "closed"
