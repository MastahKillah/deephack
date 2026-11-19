"""An emulated MYSTIQUE that deephack's own driver can talk to: the stock firmware in the emulator,
booted and plugged in.

    from bench import EmulatedCooler
    cooler = EmulatedCooler()
    driver = cooler.driver()              # a deephack.device.Mystique, wired to the emulator
    print(driver.uuid())
    driver.upload_jpeg("0" * 32, jpeg)
    print(cooler.files())                 # what the firmware stored on its SD card

Only the logo, upload and image screens have their widgets in the stand-in ui.bin (uibin.py),
and the firmware uses its widgets unchecked: switching to the dashboard or the recorder (mode 1 or 3)
ends in a call through a null pointer, which the emulator reports.

    python tools/emu/bench.py             # boots, asks a few things, stores a picture and shows it

Emulated time only passes while the driver waits for something, so nothing here is fast or slow in the
way the real cooler is: a second of firmware time is about 400 000 code blocks (see run_for).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import boot  # noqa: E402
import fat  # noqa: E402
import uibin  # noqa: E402

BOOT_LIMIT = 10_000_000                    # code blocks; a boot takes about 4 million (the logo stays for 4 s)
READY = 0x200051A8                         # set when the boot screen is done; nothing switches screens before
STEP = 5_000                               # code blocks between looks at the USB traffic
BLOCKS_PER_SECOND = boot.BLOCKS_PER_TICK * 1000        # SysTick is 1 kHz


class Timeout(RuntimeError):
    pass


class EmulatedUsb:
    """What the driver uses of a pyusb device: write(endpoint, data, timeout), read(endpoint, size, timeout)."""

    def __init__(self, machine: boot.Machine, patience: int = 4_000_000):
        self.machine, self.patience = machine, patience

    def _until(self, done):
        for _ in range(self.patience // STEP):
            result = done()
            if result is not None:
                return result
            outcome = self.machine.run(blocks=STEP)
            if self.machine.stuck is not None:
                raise Timeout(f"the firmware stopped: {outcome}")
        raise Timeout(f"the firmware did not get there in {self.patience} code blocks")

    def write(self, endpoint: int, data, timeout=None) -> int:
        usb = self.machine.usb
        usb.send(endpoint, bytes(data))
        self._until(lambda: None if usb.events else True)
        return len(data)

    def read(self, endpoint: int, size: int, timeout=None) -> bytes:
        usb = self.machine.usb
        return self._until(lambda: usb.take(endpoint & 0x7F))[:size]


class EmulatedCooler:
    def __init__(self, files: dict[str, bytes] | None = None, ui: bytes | None = None,
                 firmware: Path = boot.DEFAULT_IMAGE, functions: Path | None = None):
        """`files`: what is on the SD card at power-on. `ui`: the ui.bin on the serial flash."""
        flash = fat.build({"ui.bin": ui or uibin.stock()})
        self.machine = boot.Machine(Path(firmware).read_bytes(), functions, flash=flash)
        self.machine.sd.load(fat.build(files or {}, megabytes=64, cluster_sectors=8, label="COOLER"))
        self.machine.block_budget, outcome = 0, "still running"
        while not self.machine.uc.mem_read(READY, 1)[0]:
            if sum(self.machine.blocks.values()) >= BOOT_LIMIT or self.machine.stuck is not None:
                raise Timeout(f"the firmware did not finish booting: {outcome}")
            outcome = self.machine.run(blocks=250_000)
        self.machine.usb.reset()
        self.machine.usb.configure()
        self.usb = EmulatedUsb(self.machine)
        self.run_for(0.05)

    def driver(self):
        """deephack's driver for this cooler (its waits are skipped: emulated time is not real time)."""
        from deephack import device

        cooler = self

        class Driver(device.Mystique):
            def __init__(self):
                self.timeout, self.dev = 2000, cooler.usb

            def close(self):
                pass

            def upload_jpeg(self, file_id, jpeg):
                self._upload(super().upload_jpeg, file_id, jpeg)

            def upload_gif(self, file_id, frames, duration_ms):
                self._upload(super().upload_gif, file_id, frames, duration_ms)

            @staticmethod
            def _upload(send, *args):
                real_sleep, device.time.sleep = device.time.sleep, lambda seconds: cooler.run_for(seconds)
                try:
                    send(*args)
                finally:
                    device.time.sleep = real_sleep

        return Driver()

    def run_for(self, seconds: float) -> None:
        self.machine.run(blocks=int(seconds * BLOCKS_PER_SECOND))

    def files(self) -> dict[str, bytes]:
        """The files on the SD card now."""
        return fat.read(self.machine.sd.raw())

    def screen(self, path: Path) -> str:
        return boot.screenshot(self.machine, path)

    @property
    def console(self) -> str:
        return self.machine.console.decode("latin1")


def _main() -> int:
    import io

    from PIL import Image, ImageDraw

    from deephack import device

    cooler = EmulatedCooler(functions=Path.home() / "Desktop" / "m_fmw_decompiled" / "functions_index.txt")
    driver = cooler.driver()
    print("uuid:", repr(driver.uuid()))
    driver.set_options(brightness=70, light=device.LIGHT_SCREEN)
    driver.set_mode(device.MODE_MOTION)
    print("options and mode: accepted")
    picture = Image.new("RGB", (480, 640), (20, 30, 60))
    ImageDraw.Draw(picture).ellipse((140, 220, 340, 420), fill=(240, 180, 40))
    buf = io.BytesIO()
    picture.save(buf, "JPEG", quality=85, subsampling="4:2:0")
    driver.upload_jpeg("0123456789abcdef0123456789abcdef", buf.getvalue())
    cooler.run_for(1.0)
    print("upload: sent")
    driver.show_slot(0)
    before = len(cooler.machine.jpeg.decoded)
    cooler.run_for(1.0)
    print(f"show: the firmware ran its JPEG decoder {len(cooler.machine.jpeg.decoded) - before} times in the next second"
          + (f", errors: {cooler.machine.jpeg.errors}" if cooler.machine.jpeg.errors else ""))
    for name, content in cooler.files().items():
        print(f"  on the card: {name} ({len(content)} bytes{', identical to what was sent' if content == buf.getvalue() else ''})")
    if cooler.console:
        print("debug port:", cooler.console[-800:])
    out = Path(__file__).resolve().parents[2] / "captures"
    out.mkdir(exist_ok=True)
    print(cooler.screen(out / "emu_screen.png"))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
