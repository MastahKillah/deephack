"""Build a MYSTIQUE firmware that draws a JPEG straight from USB, without storing it (issue #14).

    python tools/fw/patch.py [stock.bin] [-o patched.bin] [--list]

This only writes a file. Nothing here talks to the cooler, and nothing in this repository flashes it:
whether the cooler's bootloader accepts a changed image is not known (the bootloader is not in the
update file), and a refused or broken image may leave a cooler that cannot be recovered without a
debug probe. Try the result in the emulator (tools/emu/bench.py --firmware patched.bin).

The stock image (v2.70 only, checked by hash) is DeepCool's and is read from the installed app. The
patch appends under 500 bytes of code after it and redirects four places in it:

    where                         stock                         patched
    0x109A4  main, before loop    sets a "running" flag         ... and clears the patch's state
    0x109A8  main loop            lv_task_handler()             show a finished frame, then the same
    0x2F158  command dispatcher   reply(command, 0, status 0)   command 20 starts a frame, then the same
    0x34C54  USB, endpoint 1 OUT  "is this packet a command?"   while a frame is coming in, it is data

The new command, on endpoint 1 like the others:

    AA 2E 20 <size:u32> <sum16 of the JPEG:u16> ...      reply status (payload byte 0):
        10  go ahead: send the JPEG in packets of up to 64 bytes, nothing after it
        11  not now: the last frame is not on screen yet, or the buffers are still being made. Ask again
        12  refused: size 0 or more than 128 KiB
    The stock firmware answers any unknown command with status 0, so 0 means "no direct drawing here".

How it works. The frame is received inside the USB interrupt, into the one of two 128 KiB buffers that
is not on screen (they are taken from the firmware's 8 MiB heap the first time the command is used and
kept). When all of it is in and the sum matches, the main loop gives the buffer to the picture widget
of the image screen with lv_img_set_src, exactly as the stock firmware does with a file it has read
into RAM; LVGL redraws and the hardware decoder writes into the frame buffer. No file, no screen
change. A frame that stalls for two seconds is given up, so a host that dies half way cannot leave
the cooler taking commands for picture data.

It needs the image screen (mode 2) with at least one stored picture: with none, the stock firmware
hides the picture widget. A frame that arrives on another screen is dropped. The JPEG must be what
the stock firmware shows: 480 x 640, baseline, 4:2:0 (deephack.device.encode_jpeg).

RAM: 36 bytes at 0x2000F000, above the firmware's stack (which starts at 0x2000B418); the emulated
firmware never touches anything up there.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

STOCK = Path("C:/DeepCool/resources/app.asar.unpacked/resources/fw/MYSTIQUE_2.70.bin")
STOCK_SHA256 = "df9c19b5ca87879bad559da9bb00f2560e1e9330eba44b89a549590414017e6e"   # MYSTIQUE_2.70.bin
APP = 0xE000                                # where the image runs
STATE = 0x2000F000                          # the patch's own RAM
MAX_FRAME = 0x20000                         # bytes of JPEG a buffer holds
COMMAND = 0x20
GO, LATER, REFUSED = 0x10, 0x11, 0x12
TIMEOUT_MS = 2000

# the stock firmware's own routines and data (real addresses, v2.70)
SYMBOLS = {
    "lv_task_handler": 0x63E90,
    "lv_mem_alloc": 0x5C3C8,
    "lv_img_set_src": 0x58D34,
    "reply": 0x34D84,                       # reply(command, 0, status): builds and sends the answer
    "usb_send": 0x3488C,                    # usb_send(IN endpoint, data, length)
    "memclr": 0x29D50,                      # memclr(address, length)
    "usb_done": 0x34BAE,                    # the USB handler's "pop {r4, r5, r7, pc}"
    "usb_command": 0x34C5A,                 # ... its check of the packet's second byte
}
TICKS = 0x200071DC                          # milliseconds since start
COMMAND_COPY = 0x20000674                   # the command being handled; its payload starts at +3
REPLY = 0x200006A4                          # where the firmware builds its 48 byte answers
SCREEN = 0x20000732                         # u16: the screen on show
WIDGET = 0x20001074                         # the image screen's picture (an lv_img)
RXCR1 = 0x40009510                          # USB: endpoint 1 OUT control (4 flush, 8 ready)
IMAGE_SCREEN = 0x90E
HEADER = 1 | 480 << 10 | 640 << 21          # lv_img_header_t: raw, 480 x 640

# (address, the stock bytes there, what replaces them)
HOOKS = [
    (0x109A4, "0121 0170", "bl init_hook"),             # movs r1, #1 ; strb r1, [r0]
    (0x109A8, None, "bl main_hook"),                    # bl lv_task_handler
    (0x2F158, None, "bl command_hook"),                 # bl reply
    (0x34C54, "2078 aa28", "b.w usb_hook"),             # ldrb r0, [r4] ; cmp r0, #0xaa
]
STOCK_CALLS = {0x109A8: "lv_task_handler", 0x2F158: "reply"}
BRANCHES = {"b", "bl", "cbz", "cbnz"} | {"b" + c for c in "eq ne hs lo mi pl vs vc hi ls ge lt gt le".split()}

# state: +0 u8 phase (0 idle, 1 receiving, 2 ready to show)   +1 u8 buffers wanted   +2 u8 buffer on screen
#        +4, +8 the two buffers   +12 size   +16 received   +20 running sum   +24 expected sum
#        +28 tick at start   +32 frames shown
SOURCE = f"""
init_hook:
    movs    r1, #1
    strb    r1, [r0]                    @ what the call replaced
    movw    r0, #{STATE & 0xFFFF}
    movt    r0, #{STATE >> 16}
    movs    r1, #0
    movs    r2, #36
init_clear:
    subs    r2, #4
    str     r1, [r0, r2]
    bne     init_clear
    bx      lr

main_hook:
    push    {{r4, r5, r6, lr}}
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    ldrb    r0, [r4, #1]
    cbz     r0, main_show
    movs    r5, #4
main_alloc:
    ldr     r0, [r4, r5]
    cbnz    r0, main_next
    movw    r0, #{(MAX_FRAME + 16) & 0xFFFF}
    movt    r0, #{(MAX_FRAME + 16) >> 16}
    bl      lv_mem_alloc
    cbz     r0, main_next
    movw    r1, #{HEADER & 0xFFFF}
    movt    r1, #{HEADER >> 16}
    str     r1, [r0, #0]                @ an lv_img_dsc_t in front of the data, as the stock loader makes it
    movs    r1, #0
    str     r1, [r0, #4]
    str     r1, [r0, #12]
    add.w   r1, r0, #16
    str     r1, [r0, #8]
    str     r0, [r4, r5]
main_next:
    adds    r5, #4
    cmp     r5, #12
    bne     main_alloc
    movs    r0, #0
    strb    r0, [r4, #1]                @ one try per request
main_show:
    ldrb    r0, [r4, #0]
    cmp     r0, #2
    bne     main_done
    movw    r0, #{SCREEN & 0xFFFF}
    movt    r0, #{SCREEN >> 16}
    ldrh    r0, [r0]
    movw    r1, #{IMAGE_SCREEN}
    cmp     r0, r1
    bne     main_drop
    ldrb    r0, [r4, #2]
    eor     r0, r0, #1
    strb    r0, [r4, #2]                @ the buffer just filled is the one on screen now
    add.w   r1, r4, r0, lsl #2
    ldr     r1, [r1, #4]
    ldr     r0, [r4, #12]
    str     r0, [r1, #4]                @ data_size
    movw    r0, #{WIDGET & 0xFFFF}
    movt    r0, #{WIDGET >> 16}
    ldr     r0, [r0]
    bl      lv_img_set_src
    ldr     r0, [r4, #32]
    adds    r0, #1
    str     r0, [r4, #32]
main_drop:
    movs    r0, #0
    strb    r0, [r4, #0]                @ only now may the next frame start
main_done:
    pop.w   {{r4, r5, r6, lr}}
    b.w     lv_task_handler

command_hook:                           @ r0 = command, r1 = 0, r2 = 0; runs in the USB interrupt
    cmp     r0, #{COMMAND}
    beq     command_ours
    b.w     reply
command_ours:
    push    {{r4, r5, r6, lr}}
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    movw    r3, #{COMMAND_COPY & 0xFFFF}
    movt    r3, #{COMMAND_COPY >> 16}
    ldr.w   r1, [r3, #3]                @ size
    ldrh.w  r3, [r3, #7]                @ sum16
    movs    r2, #{REFUSED}
    cbz     r1, command_out
    cmp.w   r1, #{MAX_FRAME}
    bhi     command_out
    movs    r2, #{LATER}
    ldr     r0, [r4, #4]
    cbz     r0, command_want
    ldr     r0, [r4, #8]
    cbz     r0, command_want
    ldrb    r0, [r4, #0]
    cmp     r0, #2
    beq     command_out
    str     r1, [r4, #12]
    movs    r0, #0
    str     r0, [r4, #16]
    str     r0, [r4, #20]
    str     r3, [r4, #24]
    movw    r0, #{TICKS & 0xFFFF}
    movt    r0, #{TICKS >> 16}
    ldr     r0, [r0]
    str     r0, [r4, #28]
    movs    r0, #1
    strb    r0, [r4, #0]
    movs    r2, #{GO}
    b       command_out
command_want:
    movs    r0, #1
    strb    r0, [r4, #1]                @ the main loop makes the buffers
command_out:                            @ the reply, made here: the stock routine sends zeros for a command it does not know
    mov     r5, r2
    movw    r4, #{REPLY & 0xFFFF}
    movt    r4, #{REPLY >> 16}
    mov     r0, r4
    movs    r1, #48
    bl      memclr
    movw    r0, #0x2e55
    strh    r0, [r4, #0]
    movs    r0, #{COMMAND}
    strb    r0, [r4, #2]
    strb    r5, [r4, #3]                @ status
    movw    r0, #0x4948
    movt    r0, #0x4344
    str.w   r0, [r4, #42]               @ "HIDC"
    addw    r0, r5, #{0x55 + 0x2E + COMMAND + sum(b"HIDC")}
    strh    r0, [r4, #46]               @ sum16
    movs    r0, #1                      @ endpoint 1 IN
    mov     r1, r4
    movs    r2, #48
    pop.w   {{r4, r5, r6, lr}}
    b.w     usb_send

usb_hook:                               @ r4 = the packet, r1 = where its length is; r4, r5, r7, lr are saved
    movw    r2, #{STATE & 0xFFFF}
    movt    r2, #{STATE >> 16}
    ldrb    r3, [r2, #0]
    cmp     r3, #1
    bne     usb_stock
    movw    r3, #{TICKS & 0xFFFF}
    movt    r3, #{TICKS >> 16}
    ldr     r3, [r3]
    ldr     r5, [r2, #28]
    subs    r3, r3, r5
    cmp.w   r3, #{TIMEOUT_MS}
    bhi     usb_expired
    ldrb    r0, [r1]                    @ bytes in this packet
    ldr     r3, [r2, #12]
    ldr     r5, [r2, #16]
    subs    r3, r3, r5
    cmp     r0, r3
    it      hi
    movhi   r0, r3                      @ no more than the frame still needs
    ldrb    r3, [r2, #2]
    eor     r3, r3, #1
    add.w   r3, r2, r3, lsl #2
    ldr     r3, [r3, #4]                @ the buffer that is not on screen
    adds    r3, #16
    add     r3, r5
    add     r5, r0
    str     r5, [r2, #16]
    ldr     r5, [r2, #20]
    cbz     r0, usb_copied
usb_copy:
    ldrb    r7, [r4], #1
    strb    r7, [r3], #1
    add     r5, r7
    subs    r0, #1
    bne     usb_copy
usb_copied:
    str     r5, [r2, #20]
    ldr     r0, [r2, #16]
    ldr     r3, [r2, #12]
    cmp     r0, r3
    bne     usb_arm
    ldr     r0, [r2, #24]
    uxth    r5, r5
    movs    r3, #2
    cmp     r0, r5
    it      ne
    movne   r3, #0                      @ a frame with the wrong sum is not shown
    strb    r3, [r2, #0]
usb_arm:
    movw    r0, #{RXCR1 & 0xFFFF}
    movt    r0, #{RXCR1 >> 16}
    movs    r1, #4
    str     r1, [r0]
    movs    r1, #8
    str     r1, [r0]
    pop     {{r4, r5, r7, pc}}
usb_expired:
    movs    r3, #0
    strb    r3, [r2, #0]
usb_stock:
    ldrb    r0, [r4]                    @ what the jump here replaced
    cmp     r0, #0xaa
    beq     usb_maybe_command
    b.w     usb_done
usb_maybe_command:
    b.w     usb_command
"""


def _assembler():
    from keystone import KS_ARCH_ARM, KS_MODE_THUMB, KS_MODE_V8, Ks

    return Ks(KS_ARCH_ARM, KS_MODE_THUMB | KS_MODE_V8)


def _branch(kind: str, at: int, target: int) -> bytes:
    """A Thumb-2 BL or B.W at `at` to `target`."""
    offset = target - (at + 4)
    if not -(1 << 24) <= offset < 1 << 24 or offset & 1:
        raise ValueError(f"{target:#x} is out of reach from {at:#x}")
    s = (offset >> 24) & 1
    j1 = ((offset >> 23) & 1) ^ 1 ^ s
    j2 = ((offset >> 22) & 1) ^ 1 ^ s
    first = 0xF000 | s << 10 | (offset >> 12) & 0x3FF
    second = (0xD000 if kind == "bl" else 0x9000) | j1 << 13 | j2 << 11 | (offset >> 1) & 0x7FF
    return struct.pack("<HH", first, second)


def _branch_target(code: bytes) -> int:
    """Where the BL or B.W in `code` goes, relative to its own address."""
    first, second = struct.unpack("<HH", code)
    if first & 0xF800 != 0xF000 or second & 0x8000 != 0x8000:
        raise ValueError(f"not a branch: {code.hex()}")
    s = (first >> 10) & 1
    i1, i2 = 1 - (((second >> 13) & 1) ^ s), 1 - (((second >> 11) & 1) ^ s)
    offset = s << 24 | i1 << 23 | i2 << 22 | (first & 0x3FF) << 12 | (second & 0x7FF) << 1
    return 4 + offset - (s << 25)


def assemble(base: int) -> tuple[bytes, dict[str, int]]:
    """The patch's code for address `base`, and where its labels are."""
    ks = _assembler()
    prologue = "".join(f".equ {name}, {address:#x}\n" for name, address in SYMBOLS.items())
    code, labels = b"", {}
    for routine in SOURCE.strip().split("\n\n"):        # one at a time: each starts with the label it is entered at
        labels[routine.split(":")[0]] = base + len(code)
        part, _ = ks.asm(prologue + routine, base + len(code))
        code += bytes(part) + b"\x00" * (-len(part) % 4)
    _check_branches(code, base)
    return code, labels


def _check_branches(code: bytes, base: int) -> None:
    """Every branch must land inside the patch or on one of SYMBOLS. (Keystone gets a conditional branch
    to an address outside the code wrong without saying so; the source avoids those, and this is the net.)"""
    from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs

    decoded = list(Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS).disasm(code, base))
    starts = {ins.address for ins in decoded}
    if sum(ins.size for ins in decoded) < len(code.rstrip(b"\x00")):
        raise ValueError("the patch's code does not disassemble to its end")
    for ins in decoded:
        if ins.mnemonic.split(".")[0] in BRANCHES and ins.op_str.split(", ")[-1].startswith("#"):
            target = int(ins.op_str.split("#")[-1], 16)
            if target not in starts and target not in SYMBOLS.values():
                raise ValueError(f"the branch at {ins.address:#x} goes to {target:#x}, which is nowhere known")


def build(stock: bytes) -> bytes:
    """The patched image. Refuses anything but the image the addresses above were read from."""
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA256:
        raise ValueError("not the firmware this patch is for (MYSTIQUE_2.70.bin): every address in it would be wrong")
    base = APP + len(stock)
    code, labels = assemble(base)
    image = bytearray(stock + code)
    for address, expected, replacement in HOOKS:
        at = address - APP
        old = bytes(image[at:at + 4])
        if expected is not None and old != bytes.fromhex(expected):
            raise ValueError(f"not the firmware this patch is for: {old.hex()} at {address:#x}")
        if address in STOCK_CALLS and address + _branch_target(old) != SYMBOLS[STOCK_CALLS[address]]:
            raise ValueError(f"not the firmware this patch is for: the call at {address:#x} goes elsewhere")
        kind, label = replacement.split()
        image[at:at + 4] = _branch("bl" if kind == "bl" else "b", address, labels[label])
    return bytes(image)


def listing(stock: bytes) -> str:
    from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs

    base = APP + len(stock)
    code, labels = assemble(base)
    names = {address: name for name, address in labels.items()}
    names.update({address: name for name, address in SYMBOLS.items()})
    out = []
    for ins in Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS).disasm(code, base):
        if ins.address in labels.values():
            out.append(f"{names[ins.address]}:")
        text = f"{ins.mnemonic:8} {ins.op_str}"
        if ins.op_str.startswith("#0x") and int(ins.op_str[1:], 16) in names:
            text += f"    ; {names[int(ins.op_str[1:], 16)]}"
        out.append(f"  {ins.address:#07x}: {ins.bytes.hex():<8}  {text}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stock", nargs="?", default=str(STOCK))
    ap.add_argument("-o", "--output", default=str(Path(__file__).resolve().parents[2] / "captures" / "MYSTIQUE_2.70_direct.bin"))
    ap.add_argument("--list", action="store_true", help="print the patch's code and stop")
    a = ap.parse_args()
    stock = Path(a.stock).read_bytes()
    if a.list:
        print(listing(stock))
        return 0
    image = build(stock)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image)
    print(f"{out}: {len(image)} bytes ({len(image) - len(stock)} more than the stock image), "
          f"sha256 {hashlib.sha256(image).hexdigest()[:16]}...")
    print("Not flashed, and not known to be accepted by the cooler's bootloader. Try it in the emulator:")
    print(f"  python tools/emu/bench.py --firmware {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
