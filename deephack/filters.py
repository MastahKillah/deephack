"""Letting go of ffmpeg's filter graphs (issue #58).

A graph of PyAV's and the filters in it hold each other (`FilterContext.graph`, which cannot be set), so
the last name for a graph can go and nothing is freed: graph, filters and the frames they keep wait for
Python's collector. Its full run, the only one that looks at things that have lived for a while, comes
seldom in a program that does the same for hours. A picture's graph (`video.picture`) is 0.7 MB while it
waits, the sound's (`songplace.Loudness`) 1.1: three hours of TikTok, whose videos come in many sizes,
were 66 MB of them.

So whoever drops a graph says so, and the collector is sent for: a little later, so that several drops are
one run, and in a thread of its own, since a run is 20 to 100 ms in which nothing else of Python's moves.
"""
from __future__ import annotations

import gc
import threading

AFTER = 2.0                 # seconds from a drop to the collector's run: what is dropped until then goes with it

_due = threading.Lock()     # held from a drop until its run begins


def dropped() -> None:
    """A filter graph was let go of: have it freed soon."""
    if not _due.acquire(blocking=False):
        return                                   # a run is on its way already
    try:
        timer = threading.Timer(AFTER, _collect)
        timer.daemon = True
        timer.start()
    except RuntimeError:                         # Python is closing: no threads any more, and nothing left to free
        _due.release()


def _collect() -> None:
    _due.release()                               # first: a drop during the run is for the next one
    gc.collect()
