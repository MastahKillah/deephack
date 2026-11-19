"""Black-box probing of the MYSTIQUE protocol: send commands the official app never (or rarely) uses and
log what comes back. Quit deephack and the DeepCool app first; both hold the USB interface.

    python tools/probe.py raw 05 01            one command, payload bytes in hex
    python tools/probe.py recorder             recorder mode with a moving fake sensor feed
    python tools/probe.py sweep 0b 30          every command in a range, empty payload

Unknown commands can do anything, including erasing stored images. Watch the screen.
"""
import argparse
import math
import sys
import time
import winsound
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deephack import device  # noqa: E402

try:
    from cam import Cam, diff, sheet
except ImportError:      # no OpenCV: run blind, a human watches
    Cam = None

KNOWN = {0x01: "sensors", 0x02: "options", 0x03: "mode", 0x04: "dashboard", 0x05: "recorder mode",
         0x06: "refresh recorder", 0x07: "slideshow", 0x08: "show slot", 0x09: "delete slot", 0x0A: "set time",
         0x0F: "start upload", 0x10: "rotate query", 0x12: "uuid", 0x13: "FACTORY RESET", 0x14: "DELETE ALL", 0x15: "cpu name", 0x16: "gpu name", 0x17: "psu name"}


def send(m, cmd, payload=b"", ep=1, timeout=700):
    """Send one command and return whatever arrives on the matching IN endpoint (None on timeout)."""
    m.dev.write(ep, device.frame(cmd, payload), 2000)
    try:
        return bytes(m.dev.read(0x80 | ep, 64, timeout))
    except Exception as e:  # timeout or stall: both are answers
        return None if "timeout" in str(e).lower() or "timed out" in str(e).lower() else f"error: {e}"


def show(cmd, payload, r):
    tag = KNOWN.get(cmd, "?")
    if r is None:
        out = "no reply"
    elif isinstance(r, str):
        out = r
    else:
        body = r[3:42].rstrip(b"\0")
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in body)
        out = f"{r[:3].hex()} | {body.hex(' ') or '(zeros)'} | {text}"
    print(f"cmd {cmd:02x} [{tag:16s}] payload {payload.hex() or '-':10s} -> {out}", flush=True)


def drain(m):
    for ep in (0x81, 0x82):
        try:
            while True:
                m.dev.read(ep, 64, 50)
        except Exception:
            pass


def feed(m, seconds, t0=0):
    """A moving fake sensor feed so graphs have something to draw."""
    for i in range(seconds):
        t = t0 + i
        m.send_sensors(cpu_temp=50 + 30 * math.sin(t / 3), cpu_load=50 + 40 * math.sin(t / 2), ram_usage=63,
                       v3_3=3.33, v5=5.05, v12=12.12, cpu_freq=3 + 2 * abs(math.sin(t / 4)),
                       fan_rpm=1111, pump_rpm=2222)
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="what", required=True)
    p = sub.add_parser("raw")
    p.add_argument("cmd")
    p.add_argument("payload", nargs="?", default="")
    p.add_argument("--ep", type=int, default=1)
    p = sub.add_parser("recorder")
    p.add_argument("--hold", type=int, default=20)
    p = sub.add_parser("sweep")
    p.add_argument("first")
    p.add_argument("last")
    p.add_argument("--pause", type=float, default=2.0, help="seconds between commands, to watch the screen")
    p.add_argument("--skip", default="09,0f,13,14",
                   help="commands to leave out (delete slot, start upload, factory reset, delete everything)")
    a = ap.parse_args()
    cam = Cam() if Cam else None
    snaps = []

    def snap(name):
        if cam:
            snaps.append(name)
            return cam.snap(name)

    with device.Mystique() as m:
        drain(m)
        if a.what == "raw":
            payload = bytes.fromhex(a.payload)
            show(int(a.cmd, 16), payload, send(m, int(a.cmd, 16), payload, a.ep))
        elif a.what == "recorder":
            steps = [("mode 3, nothing else", None), ("05 00", (5, b"\0")), ("05 01", (5, b"\1")),
                     ("05 02", (5, b"\2")), ("06 refresh", (6, b""))]
            m.set_time()
            m.set_mode(device.MODE_RECORDER)
            try:
                for n, (label, c) in enumerate(steps, 1):
                    print(f"step {n}/{len(steps)}: {label}", flush=True)
                    if c:
                        show(c[0], c[1], send(m, *c))
                    winsound.Beep(1200 if n % 2 else 900, 180)
                    for k in range(3):
                        feed(m, a.hold // 3, t0=n * a.hold + k * (a.hold // 3))
                        snap(f"rec{n}_{k}")
            finally:
                m.set_mode(device.MODE_MOTION)
                winsound.Beep(600, 500)
        elif a.what == "sweep":
            skip = {int(x, 16) for x in a.skip.split(",") if x}
            for cmd in range(int(a.first, 16), int(a.last, 16) + 1):
                if cmd in skip:
                    print(f"cmd {cmd:02x} skipped", flush=True)
                    continue
                before = snap(f"s{cmd:02x}_a")
                r = send(m, cmd)
                if r is None:   # some commands answer on the sensor endpoint instead
                    try:
                        r = bytes(m.dev.read(0x82, 64, 300))
                        print("   (reply came on endpoint 2)")
                    except Exception:
                        pass
                show(cmd, b"", r)
                time.sleep(a.pause)
                after = snap(f"s{cmd:02x}_b")
                if cam:
                    print(f"   screen change: {diff(before, after):.1f}", flush=True)

    if cam:
        cam.close()
        if snaps:
            print("sheet:", sheet(snaps, f"sheet_{a.what}.jpg"))


if __name__ == "__main__":
    main()
