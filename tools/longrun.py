"""Run modes on the real cooler for hours and write down, every minute, how it goes (issue #50).

    python tools/longrun.py --log run.log video=7:source=https://www.youtube.com/watch?v=... tiktok=2 scene=2

Each part of the plan is `mode=hours`, with settings for the mode after a colon (`key=value`, more of
them with commas; a value may hold `=` and `:` but no comma). The settings file used is a copy of the
app's own, so nothing the run changes stays. A line a minute:

    time  mode  pictures a second  refused  longest wait between two pictures  errors  share of a CPU core
          memory (working set / private, MB)  handles  threads  what the mode says

and for a video how often its stream was opened, for TikTok how many videos it has played. The app must
not run at the same time: this holds the app's lock, so that it cannot be started by mistake. To end a
run early, make a file named like the log with `.stop` at its end: the cooler gets its own picture back.
Nothing is stored on the cooler and nothing is flashed.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QLockFile  # noqa: E402

from deephack import device, engine  # noqa: E402
from deephack.config import Config, config_dir  # noqa: E402
from deephack.modes import BY_ID  # noqa: E402


class _Memory(ctypes.Structure):                 # PROCESS_MEMORY_COUNTERS_EX
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("peak", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged",
                                             "pagefile", "peak_pagefile", "private")]


def vitals() -> tuple[float, float, int, int]:
    """Working set and private memory in MB, handles (all kinds, and those of the drawing sort), threads."""
    kernel32, psapi, user32 = ctypes.windll.kernel32, ctypes.windll.psapi, ctypes.windll.user32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    me = ctypes.c_void_p(kernel32.GetCurrentProcess())
    memory = _Memory(ctypes.sizeof(_Memory))
    psapi.GetProcessMemoryInfo(me, ctypes.byref(memory), memory.cb)
    handles = wintypes.DWORD()
    kernel32.GetProcessHandleCount(me, ctypes.byref(handles))
    gdi = user32.GetGuiResources(me, 0)
    return memory.working / 2**20, memory.private / 2**20, handles.value + gdi, threading.active_count()


class Count:
    """What happened to the pictures since the last line."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset(time.perf_counter())
        self.errors: list[str] = []

    def reset(self, now: float) -> None:
        self.drawn = self.refused = self.failed = self.bytes = 0
        self.last, self.wait, self.took = now, 0.0, 0.0

    def draw(self, real, dev, jpeg, *args, **kwargs):
        started = time.perf_counter()
        try:
            result = real(dev, jpeg, *args, **kwargs)
        except TimeoutError:
            with self.lock:
                self.refused += 1
            raise
        except Exception as error:
            with self.lock:
                self.failed += 1
                self.errors.append(f"draw: {type(error).__name__}: {error}")
            raise
        now = time.perf_counter()
        with self.lock:
            self.drawn += 1
            self.bytes += len(jpeg)
            self.took += now - started
            self.wait, self.last = max(self.wait, now - self.last), now
        return result


def parse(plan: list[str]) -> list[tuple[str, float, dict]]:
    parts = []
    for item in plan:
        head, _, rest = item.partition(":")
        mode, _, hours = head.partition("=")
        if mode not in BY_ID:
            raise SystemExit(f"no mode {mode!r}; there are: {', '.join(BY_ID)}")
        settings = dict(pair.split("=", 1) for pair in rest.split(",") if "=" in pair)
        parts.append((mode, float(hours or 1), settings))
    return parts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("plan", nargs="+", help="mode=hours[:key=value,...]")
    ap.add_argument("--log", required=True)
    ap.add_argument("--every", type=float, default=60.0, help="seconds between two lines")
    args = ap.parse_args()
    parts, log_path = parse(args.plan), Path(args.log)
    stop_file = Path(str(log_path) + ".stop")

    lock = QLockFile(str(config_dir() / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        raise SystemExit("deephack is running: quit it first (its tray icon), the cooler takes one program at a time")

    def say(text: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}"
        with log_path.open("a", encoding="utf8") as log:
            log.write(line + "\n")
        try:
            print(line, flush=True)
        except (OSError, ValueError):            # nobody reads the console any more: the log has it
            pass

    copy =Path(tempfile.mkdtemp(prefix="deephack-longrun-")) / "config.json"
    try:
        shutil.copy(config_dir() / "config.json", copy)
    except OSError:
        pass
    config = Config(copy)
    config.set("enabled", True)
    config.set("mode", parts[0][0])
    for key, value in parts[0][2].items():
        config.set_mode_setting(parts[0][0], key, value)
    for mode in ("video", "tiktok"):
        config.set_mode_setting(mode, "sound", "0")          # nobody is listening

    count = Count()
    real_draw = device.Mystique.draw_jpeg
    device.Mystique.draw_jpeg = lambda dev, jpeg, *a, **k: count.draw(real_draw, dev, jpeg, *a, **k)
    real_hook = sys.excepthook
    stderr_lines: list[str] = []

    class Tee:                                   # the engine prints a mode's errors: they belong in the log
        def write(self, text):
            sys.__stderr__.write(text)
            stderr_lines.append(text)

        def flush(self):
            sys.__stderr__.flush()

    sys.stderr = Tee()
    eng = engine.Engine(config)

    def watch() -> None:
        try:
            for index, (mode_id, hours, settings) in enumerate(parts):
                if index:                                    # a mode may come twice, with other settings
                    for key, value in settings.items():
                        config.set_mode_setting(mode_id, key, value)
                    config.set("mode", mode_id)
                    eng.reload_mode()
                say(f"== {mode_id} for {hours:g} h {settings or ''}")
                until, seen, changes, said = time.monotonic() + hours * 3600, set(), 0, None
                count.reset(time.perf_counter())
                total = lines = 0
                while time.monotonic() < until and not stop_file.exists():
                    started, cpu = time.perf_counter(), time.process_time()
                    slept_until = time.monotonic() + args.every
                    while time.monotonic() < min(slept_until, until) and not stop_file.exists():
                        time.sleep(0.5)
                        status = eng._mode.status if eng._mode is not None else ""
                        name = status.split(" · ")[0]
                        if name != said:
                            said, changes = name, changes + 1
                            seen.add(name)
                    wall, cpu = time.perf_counter() - started, time.process_time() - cpu
                    with count.lock:
                        drawn, refused, failed, size, wait, took = count.drawn, count.refused, count.failed, count.bytes, count.wait, count.took
                        errors, count.errors = count.errors, []
                        count.reset(time.perf_counter())
                    if stderr_lines:
                        errors.append("stderr: " + "".join(stderr_lines).strip().replace("\n", " | ")[-600:])
                        stderr_lines.clear()
                    working, private, handles, threads = vitals()
                    mode = eng._mode
                    more = ""
                    player = getattr(mode, "_player", None)
                    if player is not None:
                        more = f"  opened {player.opened}x {player.state}"
                    if mode_id == "tiktok":
                        more += f"  videos {changes} ({len(seen)} different)"
                    total, lines = total + drawn, lines + 1
                    say(f"{mode_id:9s} {drawn / wall:5.1f}/s  refused {refused:3d}  wait {wait:5.2f} s  "
                        f"{size / max(1, drawn) / 1024:4.0f} KB {1000 * took / max(1, drawn):3.0f} ms  errors {failed + len(errors):2d}  "
                        f"cpu {100 * cpu / wall:3.0f} %  mem {working:5.0f}/{private:5.0f} MB  handles {handles:5d}  threads {threads:3d}{more}  | "
                        f"{eng._dev_state} | {(mode.status if mode else '')[:70]}")
                    for error in errors:
                        say(f"   ! {error}")
                say(f"== {mode_id} ended: {total} pictures in {lines} lines")
                if stop_file.exists():
                    say("stopped by the stop file")
                    break
        except Exception:
            say("the watcher broke: " + traceback.format_exc().replace("\n", " | "))
        finally:
            say("DONE")
            eng.stop()
            threading.Timer(30.0, lambda: os._exit(3)).start()       # should the engine not come to its end

    say(f"long run: {' '.join(args.plan)} (pid {os.getpid()})")
    threading.Thread(target=watch, daemon=True).start()
    try:
        from winrt.runtime import ApartmentType, init_apartment

        init_apartment(ApartmentType.MULTI_THREADED)
    except Exception:
        pass
    asyncio.run(eng._main())
    sys.excepthook = real_hook
    lock.unlock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
