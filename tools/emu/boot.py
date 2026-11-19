"""Boot the MYSTIQUE's stock firmware in an emulator (Unicorn), as far as it gets.

A test bench for firmware work (issues #14 and #16): see what the firmware does without the cooler.
Nothing here touches the device. The firmware image is DeepCool's and is not in this repository; it is
read from the installed DeepCool app (or pass its path).

    python tools/emu/boot.py [firmware.bin] [--steps N] [--functions functions_index.txt]

What is known about the image and the chip:

- The chip is a Synwit SWM341 (Cortex-M33 class) with an LCD controller, a hardware JPEG decoder, USB,
  a serial flash controller and an SDRAM controller. Register names come from the swm341-pac crate
  (0BSD), generated from the vendor's register description: swm341_registers.json next to this file.
- The image is a plain binary that runs at 0xE000, after a 56 KiB bootloader that is not in the file.
  Word 0 is the initial stack pointer, word 1 the reset handler (0xE33D; the startup code is at file
  offset 0x33C). Ghidra's decompile was made at base 0, so its FUN_ addresses are file offsets.
- 0x11000000 is the chip's mask ROM (flash timing helpers). It is not in the image either: every
  routine there is stubbed to return at once, and the calls are logged.

Hardware registers are plain memory here (they read back what was written, 0 at first) unless MODELS
says otherwise. Every access is logged, and when the firmware spins on a register that never changes,
the run stops and names it. That register is the next thing to model.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import struct
import sys
from pathlib import Path

from unicorn import (UC_ARCH_ARM, UC_HOOK_BLOCK, UC_HOOK_MEM_READ, UC_HOOK_MEM_UNMAPPED, UC_HOOK_MEM_WRITE, UC_MODE_MCLASS,
                     UC_MODE_THUMB, Uc, UcError)
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP

DEFAULT_IMAGE = Path("C:/DeepCool/resources/app.asar.unpacked/resources/fw/MYSTIQUE_2.70.bin")
APP = 0xE000                                              # where the image sits in flash
FLASH = (0x0000_0000, 0x0010_0000)
SRAM = (0x2000_0000, 0x0010_0000)
EXT_RAM = (0x8000_0000, 0x0100_0000)                      # SDRAM: frame buffers, the upload staging buffer
ROM = (0x1100_0000, 0x0000_1000)
EXT_FLASH = (0x7000_0000, 0x0100_0000)                    # the serial flash, as the flash controller maps it for
                                                          # reading. It holds the cooler's files; blank here.
# register areas: logged, and modelled where MODELS says so
PERIPHERALS = (0x4000_0000, 0x1000_0000)
SDRAMC = (0x8800_0000, 0x0000_1000)
CORE = (0xE000_0000, 0x0010_0000)                         # SysTick, NVIC, SCB
SPIN_LIMIT = 20_000                                       # a tiny loop run this often: the firmware is waiting
IRQ_RETURN = ROM[0] + 0xF00                               # interrupt handlers return here (see Machine.run)
BLOCKS_PER_TICK = 400                                     # a SysTick interrupt every so many code blocks

_MAP = json.loads(Path(__file__).with_name("swm341_registers.json").read_text())
REGISTERS = {int(a, 16): n for a, n in _MAP["registers"].items()}
ADDRESS = {n: a for a, n in REGISTERS.items()}
_BASES = sorted((b, n) for n, b in _MAP["peripherals"].items())
_CORE_NAMES = {0xE000ED88: "SCB.CPACR", 0xE000ED08: "SCB.VTOR", 0xE000E010: "SysTick.CTRL", 0xE000E014: "SysTick.LOAD",
               0xE000E018: "SysTick.VAL", 0xE000ED0C: "SCB.AIRCR", 0xE000ED04: "SCB.ICSR"}


def register_name(address: int) -> str:
    if address in REGISTERS:
        return REGISTERS[address]
    if address in _CORE_NAMES:
        return _CORE_NAMES[address]
    if 0xE000E100 <= address < 0xE000E500:
        return f"NVIC+{address - 0xE000E100:#x}"
    i = bisect.bisect_right(_BASES, (address, "~")) - 1
    if i >= 0 and 0 <= address - _BASES[i][0] < 0x800:
        return f"{_BASES[i][1]}+{address - _BASES[i][0]:#x}"
    return f"{address:#010x}"


# What a register reads as, where "the last value written" is not good enough: name -> f(machine, last value).
MODELS = {
    "SYS.PLLLOCK": lambda m, v: 1,              # the PLL is always locked
    "SDRAMC.CR": lambda m, v: v & ~2,           # the SDRAM's power-on sequence (bit 1) finishes at once
    "SFC.GO": lambda m, v: 0,                   # a serial flash command is done as soon as it is started
    "SDIO.CR2": lambda m, v: v | 2 if v & 1 else v,   # the SD clock is stable as soon as it is switched on
}


class Machine:
    def __init__(self, image: bytes, functions: Path | None = None):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        for base, size in (FLASH, SRAM, EXT_RAM, ROM, PERIPHERALS, SDRAMC, CORE):
            self.uc.mem_map(base, size)
        self.uc.mem_write(APP, image)
        self.uc.mem_write(ROM[0], b"\x70\x47" * (ROM[1] // 2))           # "bx lr" everywhere
        self.uc.mem_map(*EXT_FLASH)
        self.uc.mem_write(EXT_FLASH[0], b"\xff" * EXT_FLASH[1])            # erased flash reads as FF
        self.sp, self.reset = struct.unpack_from("<II", image, 0)
        self.reads: collections.Counter = collections.Counter()           # register -> times read
        self.writes: dict[int, list[int]] = collections.defaultdict(list)  # register -> last few values written
        self.readers: dict[int, set[int]] = collections.defaultdict(set)   # register -> code addresses reading it
        self.rom_calls: collections.Counter = collections.Counter()       # (routine, caller) -> times
        self.blocks: collections.Counter = collections.Counter()
        self.recent: collections.deque = collections.deque(maxlen=64)
        self.unmapped: list[tuple[int, int]] = []
        self.console = bytearray()              # what the firmware prints on its debug port (UART0)
        self.models = {ADDRESS[name]: fn for name, fn in MODELS.items()}
        self._spin = 0
        self.stuck: int | None = None
        self._interrupted = None                # the saved CPU state while an interrupt handler runs
        self._fire: int | None = None           # exception number to take at the next stop
        self._until_tick = BLOCKS_PER_TICK
        self.ticks = 0
        self.block_budget = 3_000_000
        self.names = self._load_names(functions)
        for base, size in (PERIPHERALS, SDRAMC, CORE):
            self.uc.hook_add(UC_HOOK_MEM_READ, self._on_read, begin=base, end=base + size - 1)
            self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write, begin=base, end=base + size - 1)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
        self.uc.hook_add(UC_HOOK_BLOCK, self._on_block)
        self.uc.hook_add(UC_HOOK_BLOCK, self._on_rom, begin=ROM[0], end=ROM[0] + ROM[1] - 1)

    @staticmethod
    def _load_names(path: Path | None) -> list[tuple[int, str]]:
        names = []
        if path and path.exists():
            for line in path.read_text(encoding="utf8", errors="replace").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("FUN_"):      # its other labels assume base 0: wrong
                    names.append((int(parts[0], 16), parts[1]))
        return sorted(names)

    def where(self, pc: int) -> str:
        """A code address as the decompile names it (its addresses are file offsets) plus the real address."""
        offset = pc - APP
        i = bisect.bisect_right(self.names, (offset, "~")) - 1
        if not self.names or i < 0 or offset < 0:
            return f"{pc:#x}"
        base, name = self.names[i]
        return f"{name}+{offset - base:#x}"

    # ---- hooks
    def _on_read(self, uc, access, address, size, value, data):
        self.reads[address] += 1
        self.readers[address].add(uc.reg_read(UC_ARM_REG_PC))
        model = self.models.get(address & ~3)
        if model is not None:
            last = struct.unpack("<I", uc.mem_read(address & ~3, 4))[0]
            uc.mem_write(address & ~3, struct.pack("<I", model(self, last) & 0xFFFFFFFF))

    def _on_write(self, uc, access, address, size, value, data):
        if address == ADDRESS["UART0.DATA"]:
            self.console.append(value & 0xFF)
        log = self.writes[address]
        log.append(value & 0xFFFFFFFF)
        del log[:-4]

    def _on_unmapped(self, uc, access, address, size, value, data):
        self.unmapped.append((address, uc.reg_read(UC_ARM_REG_PC)))
        return False

    def _on_rom(self, uc, address, size, data):
        if address == IRQ_RETURN:
            return
        self.rom_calls[(address, (uc.reg_read(UC_ARM_REG_LR) & ~1) - 4)] += 1

    def _on_block(self, uc, address, size, data):
        self.blocks[address] += 1
        if self._interrupted is not None:            # inside an interrupt handler
            if address == IRQ_RETURN:
                uc.emu_stop()
            return
        self.recent.append(address)
        self._spin = self._spin + 1 if len(set(self.recent)) <= 6 and len(self.recent) == self.recent.maxlen else 0
        if self._spin >= SPIN_LIMIT and not self._ticking():
            self.stuck = address
            uc.emu_stop()
        self._until_tick -= 1
        if self._until_tick <= 0 and self._ticking():
            self._until_tick = BLOCKS_PER_TICK
            self._fire = 15                          # SysTick
            uc.emu_stop()

    def _ticking(self) -> bool:
        """SysTick is enabled with its interrupt on."""
        return struct.unpack("<I", self.uc.mem_read(0xE000E010, 4))[0] & 3 == 3

    # ---- running
    def run(self, steps: int) -> str:
        """Run from reset. Interrupts are delivered between blocks: the whole CPU state is saved, the
        handler runs and returns to a marker address, and the state is put back (the firmware has no
        RTOS, so no handler looks at what was interrupted)."""
        self.uc.reg_write(UC_ARM_REG_SP, self.sp)
        self.uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFFF)
        pc = self.reset
        try:
            while self.stuck is None:
                self._fire = None
                self.uc.emu_start(pc | 1, 0xFFFFFFF0, count=steps)
                pc = self.uc.reg_read(UC_ARM_REG_PC)
                if self._fire is not None:           # stopped at a block boundary to take an interrupt
                    self._interrupted = self.uc.context_save()
                    self.ticks += 1
                    handler = struct.unpack("<I", self.uc.mem_read(APP + 4 * self._fire, 4))[0]
                    self.uc.reg_write(UC_ARM_REG_SP, (self.uc.reg_read(UC_ARM_REG_SP) - 0x40) & ~7)
                    self.uc.reg_write(UC_ARM_REG_LR, IRQ_RETURN | 1)
                    pc = handler
                elif self._interrupted is not None and pc == IRQ_RETURN:
                    self.uc.context_restore(self._interrupted)
                    self._interrupted = None
                    pc = self.uc.reg_read(UC_ARM_REG_PC)
                else:
                    break
                if sum(self.blocks.values()) >= self.block_budget:
                    return f"ran {self.block_budget} blocks ({self.ticks} timer ticks)"
        except UcError as e:
            return f"stopped by the emulator ({e})"
        return "waiting in a loop" if self.stuck is not None else f"ran all {steps} instructions"


def report(m: Machine, outcome: str) -> None:
    pc = m.uc.reg_read(UC_ARM_REG_PC)
    print(f"{outcome} at {m.where(pc)} ({pc:#x}); {sum(m.blocks.values())} blocks run, {len(m.blocks)} distinct")
    if m.unmapped:
        address, at = m.unmapped[-1]
        print(f"  touched unmapped memory {address:#010x} from {m.where(at)}")
    if m.stuck is not None:
        loop = sorted(set(m.recent))
        print("  the loop:", ", ".join(m.where(a) for a in loop))
        polled = [(a, n) for a, n in m.reads.items() if any(loop[0] <= r <= loop[-1] + 64 for r in m.readers[a])]
        for address, n in sorted(polled, key=lambda x: -x[1])[:4]:
            print(f"  it keeps reading {register_name(address)} ({address:#010x}), {n} times so far")
    if m.console:
        print("  debug port output:")
        for line in m.console.decode("latin1").splitlines():
            print("    | " + line)
    for (address, caller), n in sorted(m.rom_calls.items()):
        print(f"  ROM routine {address:#010x} called {n}x from {m.where(caller)}")
    pages: collections.Counter = collections.Counter()
    for address, n in m.reads.items():
        pages[address & ~0x7FF] += n
    for address, values in m.writes.items():
        pages[address & ~0x7FF] += len(values)
    print("  hardware touched (accesses, then the registers):")
    for page, n in sorted(pages.items()):
        registers = sorted({a for a in list(m.reads) + list(m.writes) if a & ~0x7FF == page})
        more = f" ... ({len(registers)} registers)" if len(registers) > 10 else ""
        print(f"    {n:7d}  " + " ".join(register_name(a) for a in registers[:10]) + more)


def screenshot(m: Machine, path: Path) -> str:
    """The LCD controller's layer 0 as a picture: the firmware points it at a frame buffer in SDRAM."""
    from PIL import Image

    def reg(offset):
        return struct.unpack("<I", m.uc.mem_read(_MAP["peripherals"]["LCD"] + offset, 4))[0]

    width, height, base = (reg(0x44) >> 16) + 1, (reg(0x48) >> 16) + 1, reg(0x4C)
    if not (EXT_RAM[0] <= base < EXT_RAM[0] + EXT_RAM[1]) or width > 1024 or height > 1024:
        return "the LCD controller has no frame buffer yet"
    raw = bytes(m.uc.mem_read(base, width * height * 2))
    Image.frombytes("RGB", (width, height), raw, "raw", "BGR;16").save(path)
    used = sum(1 for i in range(0, len(raw), 2048) if raw[i:i + 2048].strip(b"\x00"))
    return f"{width}x{height} frame buffer at {base:#x} saved to {path} ({'has content' if used else 'all black'})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image", nargs="?", default=str(DEFAULT_IMAGE))
    ap.add_argument("--steps", type=int, default=200_000_000)
    ap.add_argument("--blocks", type=int, default=3_000_000, help="stop after this many code blocks")
    ap.add_argument("--functions", default=str(Path.home() / "Desktop" / "m_fmw_decompiled" / "functions_index.txt"),
                    help="Ghidra's function list, to name code addresses (optional)")
    a = ap.parse_args()
    image = Path(a.image).read_bytes()
    m = Machine(image, Path(a.functions))
    m.block_budget = a.blocks
    print(f"image {len(image)} bytes, stack {m.sp:#x}, reset handler {m.reset:#x}")
    report(m, m.run(a.steps))
    out = Path(__file__).resolve().parents[2] / "captures"
    out.mkdir(exist_ok=True)
    print("  screen:", screenshot(m, out / "emu_screen.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
