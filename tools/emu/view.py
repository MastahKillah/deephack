"""A window on the emulated cooler: its screen and its 14 lights, live, while deephack draws on it.

    python tools/emu/view.py                     the Scene, drawn by deephack's own engine on the patched firmware
    python tools/emu/view.py --mode countdown    another mode (one that draws pictures)
    python tools/emu/view.py --stock             DeepCool's firmware as it is: pictures are stored, the cooler picks the lights
    python tools/emu/view.py --save clip.gif --seconds 20      no window to watch: record it, and leave

Everything between the mode and the glass is the real thing: deephack's engine and driver, the USB
packets, the firmware (with the direct drawing patch from tools/fw/patch.py, assembled here), its JPEG
decoder's model, the frame buffer the LCD controller is pointed at. The lights are read where the
firmware puts them on their wire (a byte a bit, at 0x20005B54), so what is shown beside the screen is
what the 14 would be sent, fades and all.

What it is not: the whole cooler. Only the image screen has its widgets (uibin.py); switching to one
of the cooler's own sensor screens would end the firmware in a call through a null pointer, so the
driver here does not pass that on: a mode that uses those screens (Live) shows nothing, and the detour
through them that deephack makes before its first drawn picture is left out. The firmware's clock runs
at 0.7 to 1 times the PC's here (the window says how fast: the emulator counts code, not time), and a
stored picture takes some ten seconds to get there, a packet at a time. Nothing
here touches a real cooler, nor deephack's settings: it has its own, in a folder that is thrown away.

Needs DeepCool's firmware image (from the installed DeepCool app) and the `emu` extra.
"""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import json
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

faulthandler.disable()                 # Unicorn sets its memory up through access violations it handles itself
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "fw"))
sys.path.insert(0, str(HERE.parents[1]))
import bench  # noqa: E402
import boot  # noqa: E402
import patch  # noqa: E402
import usb.core  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from deephack import device, engine  # noqa: E402
from deephack.config import Config  # noqa: E402
from deephack.ui import style  # noqa: E402
from deephack.ui.widgets import Preview  # noqa: E402

STEP = 2000                            # code blocks the firmware runs between two looks at who else wants the cooler
WIRE = 0x20005B54                      # the 14 lights as the firmware sends them: a byte a bit, green red blue
LIGHT_STATE = 0x200007B0               # +8: the motherboard's signal is passed through; +0xA: 3 when they follow the screen
PATIENCE = 60.0                        # real seconds a command may wait for the emulated firmware


class LiveCooler:
    """The emulated cooler, running by itself in a thread; everyone else touches it inside `hold()`."""

    def __init__(self, firmware: Path):
        self.cooler = bench.EmulatedCooler(firmware=firmware)
        self.machine = self.cooler.machine
        self.lock, self.door = threading.Lock(), threading.Lock()
        self.running = True
        self.stopped = ""                           # why the firmware does not run any more
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @contextlib.contextmanager
    def hold(self):
        """The cooler to oneself. Whoever waits for it stands in the door meanwhile, so that the firmware's
        own thread, which wants it back the moment it lets go, cannot get in first: without that an upload's
        packets waited for seconds each."""
        with self.door:
            self.lock.acquire()
        try:
            yield
        finally:
            self.lock.release()

    def _run(self) -> None:
        while self.running and not self.stopped:
            with self.hold():
                outcome = self.machine.run(blocks=STEP)
                if self.machine.stuck is not None:
                    self.stopped = outcome
                    print("the firmware stopped:", outcome[:600], flush=True)

    def look(self) -> dict:
        """What there is to see: the picture the LCD controller shows, the lights, the firmware's clock."""
        m = self.machine
        with self.hold():
            lcd = boot._MAP["peripherals"]["LCD"]
            width, height, base = (struct.unpack("<I", m.uc.mem_read(lcd + at, 4))[0] for at in (0x44, 0x48, 0x4C))
            width, height = (width >> 16) + 1, (height >> 16) + 1
            raw = None
            if boot.EXT_RAM[0] <= base < boot.EXT_RAM[0] + boot.EXT_RAM[1] and width <= 1024 and height <= 1024:
                raw = bytes(m.uc.mem_read(base, width * height * 2))
            wire = bytes(m.uc.mem_read(WIRE, device.LIGHTS * 24))
            light = bytes(m.uc.mem_read(LIGHT_STATE + 8, 3))
            ours = m.uc.mem_read(patch.STATE + patch.L_ON, 1)[0]
            ticks = m.ticks
        values = [sum((wire[at + k] == 0xFC) << (7 - k) for k in range(8)) for at in range(0, len(wire), 8)]
        lights = [[values[i + 1], values[i], values[i + 2]] for i in range(0, len(values), 3)]
        return {"size": (width, height), "raw": raw, "lights": lights, "ticks": ticks, "ours": bool(ours),
                "follows": light[0] == 0 and light[2] == 3}


class LiveUsb:
    """The emulated cooler as the pyusb device deephack's driver talks to."""
    bcdDevice = 0x0246

    def __init__(self, live: LiveCooler):
        self.live, self.usb = live, live.machine.usb

    def _wait(self, done):
        until = time.monotonic() + PATIENCE
        while True:
            with self.live.hold():
                result = done()
            if result is not None:
                return result
            if self.live.stopped or not self.live.running:
                raise usb.core.USBError("the emulated firmware has stopped")
            if time.monotonic() > until:
                raise usb.core.USBTimeoutError("the emulated firmware did not answer")
            time.sleep(0.001)

    def write(self, endpoint: int, data, timeout=None) -> int:
        data = bytes(data)
        with self.live.hold():
            for at in range(0, max(len(data), 1), 64):          # as the host controller does: 64 byte packets
                self.usb.send(endpoint, data[at:at + 64])
        self._wait(lambda: None if self.usb.events else True)
        return len(data)

    def read(self, endpoint: int, size: int, timeout=None) -> bytes:
        return self._wait(lambda: self.usb.take(endpoint & 0x7F))[:size]


def driver_for(live: LiveCooler):
    """deephack.device.Mystique on the emulated cooler. An upload's wait for the flash write is a wait for
    the firmware's clock, which runs slower than the PC's here."""
    link = LiveUsb(live)

    class Driver(device.Mystique):
        def __init__(self, timeout_ms: int = 2000):
            self.timeout, self.dev = timeout_ms, link

        def close(self) -> None:
            pass

        def set_mode(self, mode: int) -> None:
            if mode == device.MODE_MOTION:           # the only screen the emulated cooler has (see the head of this file)
                super().set_mode(mode)

        def _settled(self, since: int, milliseconds: int = 1500) -> None:
            while live.machine.ticks - since < milliseconds and not live.stopped and live.running:
                time.sleep(0.01)

        def upload_jpeg(self, file_id, jpeg):
            super().upload_jpeg(file_id, jpeg)
            self._settled(live.machine.ticks)

        def upload_gif(self, file_id, frames, duration_ms):
            super().upload_gif(file_id, frames, duration_ms)
            self._settled(live.machine.ticks)

    return Driver


class Window(QWidget):
    def __init__(self, live: LiveCooler, eng: engine.Engine, what: str, save: Path | None, seconds: float):
        super().__init__()
        self.live, self.eng, self.what = live, eng, what
        self.setWindowTitle("deephack: the emulated cooler")
        self.setStyleSheet(f"background: {style.BG}; color: {style.TEXT};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        self.glass = Preview()
        self.line = QLabel("")
        self.line.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.small = QLabel("")
        self.small.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.small.setStyleSheet(f"color: {style.MUTED};")
        lay.addWidget(self.glass, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.line)
        lay.addWidget(self.small)
        self.began, self.ticks0 = time.monotonic(), live.machine.ticks
        self.save, self.seconds, self.frames = save, seconds, []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)

    def refresh(self) -> None:
        seen = self.live.look()
        if seen["raw"] is not None:
            width, height = seen["size"]
            picture = QImage(seen["raw"], width, height, width * 2, QImage.Format.Format_RGB16).convertToFormat(QImage.Format.Format_RGB888)
            self.glass._image = picture
        self.glass._lights = [list(map(float, colour)) for colour in seen["lights"]]
        self.glass.update()
        real = time.monotonic() - self.began
        emulated = (seen["ticks"] - self.ticks0) / 1000
        who = ("deephack's colours, faded by the cooler" if seen["ours"] else "the cooler's own picking") if seen["follows"] \
            else "the lights do not follow the screen"
        self.line.setText(f"{self.what} · {who}")
        self.small.setText(self.live.stopped and f"the firmware stopped: {self.live.stopped[:90]}"
                           or f"{emulated:.1f} s of the cooler's time in {real:.0f} s: {emulated / max(real, 0.1):.2f} of its real speed"
                           + (f" · {self.eng._mode.status}" if self.eng._mode is not None and self.eng._mode.status else ""))
        if self.save is not None:
            self.frames.append(self.grab().toImage())
            if real >= self.seconds:
                self.finish()

    def finish(self) -> None:
        from PIL import Image

        self.timer.stop()
        pictures = []
        for frame in self.frames:
            frame = frame.convertToFormat(QImage.Format.Format_RGB888)
            pictures.append(Image.frombuffer("RGB", (frame.width(), frame.height()), bytes(frame.constBits()), "raw", "RGB",
                                             frame.bytesPerLine()))
        if self.save.suffix.lower() == ".gif":
            pictures[0].save(self.save, save_all=True, append_images=pictures[1::2], duration=200, loop=0)
        else:
            pictures[-1].save(self.save)
        print(f"{self.save}: {len(pictures)} looks in {self.seconds:.0f} s; {self.small.text()}")
        QApplication.instance().quit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", default="scene", help="the mode deephack runs on it (default: scene)")
    ap.add_argument("--stock", action="store_true", help="DeepCool's firmware without the patch")
    ap.add_argument("--firmware", help="another image to run (default: the patched one, built now)")
    ap.add_argument("--save", help="record the window to this file (.gif: all of it, anything else: its last look) and leave")
    ap.add_argument("--seconds", type=float, default=20.0, help="... after this many seconds")
    a = ap.parse_args()
    if a.mode not in engine.BY_ID:
        sys.exit(f"no mode {a.mode!r}: one of {', '.join(sorted(engine.BY_ID))}")
    scratch = Path(tempfile.mkdtemp(prefix="deephack-emu-"))
    image = boot.DEFAULT_IMAGE
    if a.firmware:
        image = Path(a.firmware)
    elif not a.stock:
        image = scratch / "patched.bin"
        image.write_bytes(patch.build(boot.DEFAULT_IMAGE.read_bytes()))
    print("starting the firmware (some seconds: its logo stays for four of its own) ...", flush=True)
    live = LiveCooler(image)

    device.Mystique = driver_for(live)
    engine._stored_count = lambda kind: 0                       # an empty card: nothing of the DeepCool app's on it
    (scratch / "config.json").write_text(json.dumps({"enabled": True, "mode": a.mode, "light": "screen"}), encoding="utf8")
    app = QApplication(sys.argv[:1])
    eng = engine.Engine(Config(scratch / "config.json"))
    what = f"{engine.BY_ID[a.mode].title} on " + ("DeepCool's firmware 2.70" if a.stock else f"firmware 2.70 with patch {patch.VERSION}"
                                                    if not a.firmware else image.name)
    window = Window(live, eng, what, Path(a.save) if a.save else None, a.seconds)
    window.show()
    eng.start()
    code = app.exec()
    eng.stop()
    eng.wait(8000)
    live.running = False
    return code


if __name__ == "__main__":
    sys.exit(main())
