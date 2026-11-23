"""Where a drawn frame's time goes (issue #47): frames of several sizes sent to the cooler as fast as it
takes them, with the clock read at every step.

    python tools/drawbench.py [--frames 60] [--sizes 3,8,15,25,40,60,90]

For a frame the host asks (command 20), the cooler says "go ahead" or "later", the host sends the JPEG,
and asks for the next: "later" until the cooler has shown this one. So a frame costs

    busy   from the last byte sent until the cooler says "go ahead" again: decoding, showing, and
           whatever else it waits for
    ask    one question and its answer
    send   the JPEG over USB

and the three are printed for each size, with what they add up to. Needs the direct drawing firmware.
Nothing is stored and nothing is flashed; the app must not run meanwhile (this holds its lock).
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageFilter  # noqa: E402
from PySide6.QtCore import QLockFile  # noqa: E402

from deephack import device  # noqa: E402
from deephack.config import Config, config_dir  # noqa: E402


def picture(seed: int, rough: float) -> Image.Image:
    """Something to compress: soft blobs of colour with noise over them, the more noise the larger the
    JPEG, and another picture for every seed."""
    rnd = random.Random(seed)
    small = Image.frombytes("RGB", (12, 16), bytes(rnd.randrange(256) for _ in range(12 * 16 * 3)))
    soft = small.resize((device.WIDTH, device.HEIGHT), Image.Resampling.BICUBIC)
    noise = Image.frombytes("RGB", (device.WIDTH, device.HEIGHT), rnd.randbytes(device.WIDTH * device.HEIGHT * 3))
    return Image.blend(soft, noise, rough)


def jpegs(kilobytes: float, count: int = 6) -> list[bytes]:
    """A few different JPEGs of about that size (a frame like the last is not a fair test of anything)."""
    low, high = 0.0, 1.0
    for _ in range(14):
        rough = (low + high) / 2
        size = len(device.encode_jpeg(picture(0, rough), quality=80)) / 1024
        low, high = (rough, high) if size < kilobytes else (low, rough)
    return [device.encode_jpeg(picture(seed, rough), quality=80) for seed in range(count)]


def prepare(dev: device.Mystique, slot: int) -> None:
    """The image screen, entered afresh, with a stored still on it: what frames are drawn over."""
    dev.set_mode(device.MODE_SITUATIONAL)
    time.sleep(0.15)
    dev.set_mode(device.MODE_MOTION)
    dev.set_slideshow(False)
    dev.show_slot(slot, device.TYPE_JPG)
    time.sleep(0.5)


def run(dev: device.Mystique, frames: list[bytes], count: int, pause: float = 0.0) -> dict:
    """Send `count` frames one after the other; `pause`: wait that long after each before asking again."""
    busy, ask, send, cycle, asks, sent_at = [], [], [], [], [], None
    for i in range(count + 3):
        jpeg = frames[i % len(frames)]
        check, n = device.sum16(jpeg), 0
        while True:
            asked = time.perf_counter()
            status = dev._direct(len(jpeg), check)
            answered = time.perf_counter()
            n += 1
            if status == device.DIRECT_GO:
                break
            if status != device.DIRECT_LATER:
                raise SystemExit(f"the cooler said {status:#04x}: is the direct drawing firmware on it?")
            if answered - (sent_at or answered) > 2.0:
                raise SystemExit("the cooler has said 'later' for two seconds")
        dev.dev.write(1, jpeg, dev.timeout)
        done = time.perf_counter()
        if i >= 3:                                           # the first ones: the cooler is still making its buffers
            busy.append(asked - sent_at)
            ask.append(answered - asked)
            send.append(done - answered)
            cycle.append(done - sent_at)
            asks.append(n)
        sent_at = done
        if pause:
            time.sleep(pause)
    ms = lambda values: statistics.median(values) * 1000
    size = statistics.mean(len(f) for f in frames) / 1024
    return {"kb": size, "busy": ms(busy), "ask": ms(ask), "send": ms(send), "cycle": ms(cycle), "asks": statistics.median(asks),
            "rate": size / (ms(send) / 1000), "worst": max(cycle) * 1000}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--sizes", default="3,8,15,25,40,60,90", help="kilobytes, with commas")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds to wait after a frame before asking for the next")
    args = ap.parse_args()
    lock = QLockFile(str(config_dir() / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        raise SystemExit("deephack is running: quit it first, the cooler takes one program at a time")
    slot = Config().get("slot_index") or 0
    with device.Mystique() as dev:
        version = dev.draw_version()
        if not version:
            raise SystemExit("this cooler's firmware does not draw without storing")
        timed = []
        for _ in range(200):
            t = time.perf_counter()
            dev.draw_version()
            timed.append(time.perf_counter() - t)
        print(f"patch version {version}; a question and its answer take {statistics.median(timed) * 1000:.2f} ms "
              f"(the fastest {min(timed) * 1000:.2f})", flush=True)
        prepare(dev, slot)
        print(f"{'KB':>6} {'busy':>7} {'ask':>6} {'send':>7} {'= frame':>8} {'a second':>9} {'KB/s':>6} {'asks':>5} {'worst':>7}")
        for kilobytes in (float(k) for k in args.sizes.split(",")):
            r = run(dev, jpegs(kilobytes), args.frames, args.pause)
            print(f"{r['kb']:6.1f} {r['busy']:6.1f}  {r['ask']:5.1f}  {r['send']:6.1f}  {r['cycle']:7.1f}  {1000 / r['cycle']:8.1f}  "
                  f"{r['rate']:5.0f}  {r['asks']:4.0f}  {r['worst']:6.0f}", flush=True)
        dev.set_mode(device.MODE_SITUATIONAL)                # leave the screen as the app finds it at its start
        time.sleep(0.15)
        dev.set_mode(device.MODE_MOTION)
        dev.show_slot(slot, device.TYPE_JPG)
    lock.unlock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
