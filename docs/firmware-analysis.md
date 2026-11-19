# MYSTIQUE firmware analysis

Notes from reading a decompile of the MYSTIQUE's firmware image. This supplements the protocol
section in the [README](../README.md): the README describes the wire protocol as observed from the
host side; this describes what the firmware does with it internally, and what that means for ideas
like direct drawing.

Confidence is marked per item. "Confirmed in code" means traced in the decompile; "inferred" means
deduced from surrounding code and not fully followed; "unverified" means a reasonable guess only.
"Seen in the emulator" means the stock image did it when run in `tools/emu/boot.py`.
Addresses and function names are from one particular decompile and are not stable identifiers.

**The decompile's addresses are file offsets.** It was made with the image loaded at 0, but the image
runs at `0xE000`: add `0xE000` to a `FUN_` address to get the real one. Its `Reset_Handler` /
`..._Handler` labels are wrong for the same reason (they mark where the vector table would point if the
base were 0), and so are its jump tables: some functions decompile as an empty endless loop. String
references are unaffected, because the code reaches its strings relative to the program counter.

## Platform

- **MCU:** Synwit **SWM341** (ARM Cortex-M33 class with FPU). The config system reads a
  `SynwitManifest.cfg`; the display driver is selected by name (`jlt35002a_v3` / `jlt35002a_v3_spi`).
  *Confirmed in code; the chip by its register layout, seen in the emulator:* the clock setup polls the
  PLL lock flag at `0x400AA04C`, and every register the firmware touches during start-up has a name in
  the SWM341's register description (LCD at `0x40002000`, JPEG decoder at `0x4000B000`, USB at
  `0x40005000`, serial flash controller at `0x4004A800`, SDIO at `0x40001800`, SDRAM controller at
  `0x88000000`).
- **Image layout:** a plain binary linked at **`0xE000`**; a 56 KiB bootloader that is not in the update
  file sits below it. Word 0 is the initial stack pointer (`0x2000B418`), word 1 the reset handler
  (`0xE33D`, startup code at file offset `0x33C`), then about a hundred interrupt vectors. With base
  `0xE000`, eleven words in the image point exactly at the start of a string; with base 0, none.
  *Confirmed.*
- **Mask ROM:** the start-up code calls `0x11000430` twice (Synwit's flash timing helper). The ROM is
  not in the image. *Seen in the emulator.*
- **Memory:** SDRAM at `0x80000000`. The LCD controller's layer 0 is 480 x 640 with its frame buffer at
  `0x80000000`, 16 bits per pixel. *Seen in the emulator (the values written to the LCD registers).*
- **UI stack:** LVGL. The firmware instantiates `lv_arc`, `lv_btn`, `lv_chart`, `lv_label`, `lv_line`
  etc. and loads its screens from a `ui.bin` resource table. The dashboard labels/units are baked
  into these resources, which is why only the numbers are host-controllable. *Confirmed in code.*
- **Display:** 480x640, configured via `peripheral.display.hres` / `.vres` (0x1e0 x 0x280). *Confirmed.*
- **Storage:** *two* devices, seen in the emulator. A **serial (QSPI) flash**, identified with JEDEC
  `9F`, switched to quad mode and read through a memory window at `0x70000000` with command `EB`: it
  holds the UI resources (the firmware asks for `S:spi:///ui.bin`). And an **SD card or SD NAND on SDIO**,
  brought up with the standard SD handshake (CMD0, CMD8, ACMD41, CMD2, CMD3, CMD9, CMD7, ACMD6 for the
  4-bit bus, CMD16) and read with CMD17 through the data port: by elimination and by name it holds the
  `SD:` files, the stills and animations. So the erase-cycle worry applies to an SD-type chip, which
  does its own wear levelling, rather than to a bare NOR flash. The firmware reads block 0 of an empty
  card six times before giving up on mounting it. Earlier reading of this item: one external
  SPI flash exposed through a filesystem with an
  `S:SD:` path prefix. A `test_Spiflash_OK` string and SPI init confirm the medium. This is the part
  with the unknown, finite erase-cycle budget that the host driver rate-limits writes to spare.
  *Confirmed medium; endurance unknown.*

## Command dispatch

Incoming 48-byte command frames are parsed by a single dispatcher (switch on the command byte). It
mostly sets RAM flags/values that other tasks act on later; it does **not** itself push pixels.
The reply is built by a separate routine that sets the status byte.

Reply status byte (payload[0]): *inferred from the builder.*

| status | meaning |
| --- | --- |
| 0 | ok |
| 1 | bad `HIDC` magic |
| 2 | bad checksum |
| 3 | bad `AA 2E` header |

Command details beyond the README (*inferred unless noted*):

| cmd | firmware behaviour |
| --- | --- |
| `02` | byte1=1 enables accelerometer auto-rotation; byte3 is an RGB lighting mode for 14 outputs (0 off, 1 colour-follows-CPU-temp, 2 third mode); byte4 is brightness; byte0/byte2 stored, use not traced |
| `03` | accepts modes 1–4; mode 4 has no branch in the screen selector (unknown effect) |
| `04` | byte2 is never read by the screen selector — explains its lack of visible effect |
| `05` | two flag bytes, not one value: `1 0` / `0 1` / `1 1` select three layouts, `0 0` is a no-op |
| `06` | stores one flag byte (default 1); use not traced |
| `07` | duration byte is an index: 0/1/2 → 3/5/7, else 3; a third byte is also stored |
| `0B` | temperature unit: 0 = °C, 1 = °F |
| `0C` `0D` `0E` | acknowledged, no other effect |
| `10` | reply carries rotation (0–3 = 0/90/180/270°) and, in image mode, the on-screen still index |
| `11` | copies 32 payload bytes and raises a flag; looks like the **write** side of the `12` UUID/serial, which is read from a `PCSerialNum` setting at boot. **Caution:** sending it with an empty payload may blank the stored id (consumer of the flag not traced) |
| `A1` | undocumented; switches once to screen `0x917` (unknown screen) |

## Upload and storage

The upload handler buffers the **entire file** into a ~2 MiB RAM staging buffer, verifies the 16-bit
sum, then writes it to the filesystem in 4 KiB chunks. This is true for every file type.
*Confirmed in code.*

- **Header type 0 is a firmware update**, not an image: the data is written to `SD:FW_update.bin` and
  the device reboots into a bootloader that applies it. The host driver should refuse type 0.
  *Confirmed that type 0 routes to the update file; the boot-time applier was not read.*
- **Accepted file types:** 0 (firmware), 1 (still), 2 (animation). *Confirmed.*
- **Size cap:** file must be under 2 MiB (staging buffer size). *Confirmed.*
- **Slot cap:** 9 stills and 9 animations. Beyond that the file is written but not added to the index.
  *Confirmed.*
- **On-flash layout:** *Confirmed.*
  - still: `SD:IMG/NNN_<id>/angle_0.jpg`
  - animation: `SD:GIF/NNN_<id>/angle_0/indexNNN.jpg` plus a `config.cfg` holding `gifrate` (frame
    count) and `gifalltime` (total ms). Per-frame time = total / count, default 100 ms.
- **Suspected cause of the upload-hang:** the data-receive state has no timeout and only advances when
  both the byte count and the sum16 match. An unfinished/mismatched upload can leave it stuck.
  *Inferred, not confirmed.*

## Display path

The image screen builds a path (`<slot>/angle_0.jpg`) and hands it to the JPEG loader, which decodes
to the LVGL framebuffer that is then flushed to the LCD controller. The dashboard and recorder modes
use the same framebuffer→panel flush but render LVGL widgets instead of a decoded JPEG — and crucially
**do not touch flash**, which is why live sensor/graph updates cost no erase cycles. *Confirmed in code.*
deephack's Live mode relies on this: it feeds both screens once a second.

## Direct drawing — feasibility

**The hardware already supports RAM→panel drawing.** The dashboard/recorder modes flush a RAM
framebuffer to the LCD every second with no flash involved. The "store before you draw" rule is imposed
by the USB command surface, not the silicon.

A direct-draw command in custom firmware would reuse pieces that already exist:

- the ~2 MiB RAM staging buffer the upload handler already fills,
- the existing JPEG decoder (currently fed from a file path),
- the existing LCD flush.

Stock flow: `USB → staging buffer → SPI-flash file → read back → decode → panel`.
Target flow: `USB → staging buffer → decode → panel` (drop the two flash steps).

### Routes, best first

1. **Patched stock firmware over the existing update path.** Header type 0 already provides an
   over-USB update channel. Viable **iff** the boot-time applier does not verify a real signature.
2. **Flash via SWD/JTAG.** Open the unit, find debug pads: gives a full firmware dump (to confirm the
   real base addresses this decompile only guesses) and an unbrickable recovery path. Recommended
   first regardless of which route ships.
3. **From-scratch firmware.** Possible (known MCU + LVGL + known LCD driver) but wasteful when the
   stock image already contains a working display stack to patch.

### Deciding unknowns

- **Is SWD locked (read-out protection)?** If so, routes 1 and 2 get much harder and the recovery net
  is gone. Check physically with an SWD dongle on the debug pads.
- **Does the bootloader verify `FW_update.bin`?** A plain CRC can be recomputed; a real signature kills
  route 1. **Answerable from the decompile** — the boot-time applier/verifier was not yet read.
- **Can the JPEG decoder take a RAM source** or is it hardwired to file paths? Decides how small the
  patch is. Not yet read.

### Risk

Flashing a rejected or malformed image with SWD locked can brick the unit with no recovery. Order of
operations: confirm SWD access → dump → understand update verification → only then flash anything.

## Emulator

`tools/emu/boot.py` runs the stock image in Unicorn (nothing touches the cooler; the image is read from
the installed DeepCool app). Hardware registers are plain memory unless modelled, every access is logged
with its SWM341 register name, and a run stops when the firmware spins on a register that never changes,
which names the next thing to model. `tools/emu/disasm.py <address>` disassembles at a real address.

How far the stock firmware gets (v2.70), and what each step needed:

| step | needed |
| --- | --- |
| reset, FPU on, clocks | the image at `0xE000`; mask ROM routines stubbed; PLL lock flag reads 1 |
| pins, SDRAM | the SDRAM controller's power-on bit clears at once |
| debug UART, serial flash | a flash command completes at once; a blank (all `FF`) flash at `0x70000000` |
| GPIO, backlight PWM, delays | SysTick interrupts (the delay loop counts ticks at `0x200071DC`) |
| LCD controller, JPEG decoder set up | nothing more |
| SD card | an SD card and host controller model (`tools/emu/sdcard.py`): the handshake completes and block 0 is read |
| real-time clock | its "configurable" flag |
| I2C (the tilt sensor) | transfers end at once; nothing answers |
| graphics | nothing more: LVGL starts and draws |
| serial flash contents | a FAT volume with `ui.bin` on it (`tools/emu/fat.py`, `tools/emu/uibin.py`) |
| UI data | the hardware divider (`DIV`): the loader divides with it and checks the answer |
| SD card contents | a FAT volume (`fat.py`); the firmware makes its `IMG` and `GIF` folders itself |
| the boot screen ending | the LCD controller taking a new frame buffer address at once (`LCD.CR` bit 15) |
| USB commands | a model of the USB device controller (`tools/emu/usbdev.py`) |
| an upload | the upload screen and the image screen in `ui.bin`, with the widgets their code uses |

It then runs its **main loop**: the logo screen for 4 s, then the screen for its mode, USB commands
answered, the tilt sensor polled over I2C. The frame buffer is saved as a picture after every run
(`captures/emu_screen.png`). With `--flash blank` the firmware shows what it does without its UI data:
a red cross and "Couldn't open 'S:spi:///ui.bin'".

`tools/emu/bench.py` puts deephack's own driver (`deephack.device.Mystique`) in front of the emulated
firmware. What works that way: the device descriptor (3633:0009), commands and their replies (`12`
answers an empty id on a fresh card), and **an upload**: the driver's `upload_jpeg` ends with
`IMG/000_<id>/angle_0.jpg` on the emulated SD card, byte for byte what was sent.

Still to do: the JPEG decoder (hardware on this chip), without which no stored picture reaches the
frame buffer, and the widgets of the other screens (see below): the dashboard and the recorder end in
a call through a null pointer, because the firmware uses its widgets unchecked.

One emulator bug worth remembering: an interrupt can arrive inside a Thumb IT block. The handler must
start with the IT state cleared, or its first instructions stay conditional on the interrupted code's
flags (here the handler's `push` was skipped once in a few million ticks, and it returned to garbage).

### The serial flash and `ui.bin`

The serial flash is a FAT volume without a partition table, read 4 KiB at a time through the memory
window (FatFs drive 3; the SD card is drive 1). The firmware opens `S:spi:///ui.bin` from it. The file
is made by Synwit's UI tool and is not in the update file or the DeepCool app, and no USB command reads
a file back, so the real one is out of reach without hardware access. Its format, as the loader
(`FUN_0000110c`, `FUN_00001564`) reads it, is "SynwitUI" format 0.7:

| part | layout |
| --- | --- |
| header, `0x34` bytes | `"SynwitUI\0"`; `0x10` format major (0); `0x14` format minor (7); `0x18` a flag byte; `0x1C` offset of the screen table; `0x20` of the widgets; `0x24` of the resource table; `0x28`, `0x2C`, `0x30` of three resource data sections |
| screen table | u32 count, then that many u32 screen ids |
| widgets | 12 bytes (u16 at `+0x0A`: the screen to start with), u32 count, then {u32 screen id, u32 offset of the screen's widget} each |
| a widget | a blob: u32 size, u8 type, 3 unused bytes, u32 id, the rest is the widget's own structure (a name of up to 32 characters at `+0x10`; `+0x30` to `+0x40` and `+0x50` are filled in at run time); then u32 string count, u32 (unknown), the strings as {u32 length, bytes}; then u32 child count and that many u32 offsets of child widgets |
| resource table | `"RESLD\0"`, u32 0, u32 1, u32 count (not 0), then 12 bytes {screen id, offset, ?} each; at the offset three lists of u32 (count first), one per resource data section |

A wrong magic gives "Invalid UI data file", a wrong version "Incompatible UI data(fmt:%d.%d)! Current
platform version is v1.5.0, which uses UI data(fmt:0.7)".

Widget types, from the class name table in the image: 0 widget, 1 screen, 2 button, 3 label,
4 checkbox, 5 image, 6 bar, 7 slider, 8 switch, 9 image_btn, 10 list, 11 artchar, 12 roller,
13 dropdown, 14 arc, 15 touch, 16 levelimg, 17 chart, 18 textarea, 19 imgroller, 20 clock, 21 avi.
This firmware registers only four of them: **screen** (structure size `0x60`), **label** (`0x130`),
**image** (`0x68`) and **levelimg** (`0x98`). So every stock screen, the gauges included, is made of
labels, pictures and "level images" (a picture chosen by a value).

The loader guards itself: before and after loading a screen it divides a running number with the
chip's hardware divider and spins forever if quotient and remainder are not what software division
gives. An emulator without the divider hangs there, which is presumably the point.

`uibin.py` writes a file with every screen the firmware knows, blank except for the few widgets the
upload and image screens need. The fields inside a widget's structure (position, size, text, picture)
are not worked out, so the stock screens cannot be rebuilt yet. A label that is filled in the way the
firmware's own defaults routine does it (`0x10E40`) is accepted.

### Screens

The firmware's code is tied to `ui.bin` by ids. A screen's widget carries the screen id; at start
(`FUN_0002e340`) the firmware registers three handlers per screen id: **on entering** (it looks up its
widgets by id here and keeps the pointers), **on leaving**, and **on the screen's timer**. The table
is in the compressed data section, so the handlers have no visible reference in the image (and Ghidra
did not find most of them).

| screen | what it is |
| --- | --- |
| `01` | boot logo. Its timer handler runs twice: it starts a 4 s timer, then sets the "ready" byte (`0x200051A8`) and goes to `2D`. The screen selector (`FUN_00020628`) does nothing before that byte is set |
| `02` to `10`, `17` to `1F` | dashboards, 10 to 24 widgets each (mode 1; which one by layout and rotation) |
| `29` `2A` `2B` | recorder (mode 3): made in code from line objects and a chart, on top of 8 to 13 widgets |
| `2C` | upload. Entered when command `0F` sets its flag. **The file is received by this screen's "leaving" handler** (`0x69A88`): a loop around the upload state machine (`FUN_000200ec`) that ends on success, on a flag, or after about 15.7 million rounds without a packet; then the next screen loads |
| `2D` | where the firmware goes after the logo and when it has nothing to show; timer every tick |
| `90E` | stored pictures and animations (mode 2); widgets `90F`, `915` (150 x 200), `916` (325 x 28) |
| `917` | command `A1` |

So an upload never happens "in the background": the firmware sits in a loop inside a screen change,
and nothing else on screen is serviced until the file is in.

Widget ids each screen looks up when entered (hex; the first is the screen itself):

| screen | widget ids |
| --- | --- |
| `01` | 4A4, 912, 913, 914, 918, 919 |
| `02` | 30 to 34, 36 to 3D |
| `03` | 41 to 44, 46, 47, 4A, 4B, 4C, 50 to 53 |
| `04` | 54 to 59, 61 to 64 |
| `05` | B7 to BD, C0 to C4, E4 |
| `06` | C5, C7 to D2 |
| `07` | D3 to D9, DB, DC, DD |
| `08` | DF to E2, E5 to E9, 1B7, 1BA, 1BB, 1BC |
| `09` | EA to F2, 1B8, 1BD, 1BF, 1C1 |
| `0A` | F3 to F8, 1B9, 1BE, 1C0, 1C2 |
| `0B` | F9 to 105 |
| `0C` | 1A0, 1A2, 1A4, 1A6, 1A8 to 1B0 |
| `0D` | 1A1, 1A3, 1A5, 1A7, 1B1 to 1B6 |
| `0E` | 1C4, 1C8 to 1D2 |
| `0F` | 1C5, 1D3, 1D5, 1D7 to 1DF |
| `10` | 1C6, 1D4, 1D6, 1E0 to 1E5 |
| `17` | 22B, 22D, 386 to 38E, 3CE, 3CF, 3D0, 709, 70B |
| `18` | 22E, 3A1 to 3A9, 3D1, 3D3, 3D5, 70E, 8D8, 8E2 |
| `19` | 22F, 234, 3BC to 3C1, 3D2, 3D4, 3D6, 8D9, 8DB |
| `1A` | 23F, 241, 377, 379, 38F to 397, 3E0 to 3E5, 8E3 to 8E6 |
| `1B` | 3AA to 3B2, 8E7 to 8F4 |
| `1C` | 3C2 to 3C7, 8F5 to 902 |
| `1D` | 235, 23C, 398 to 3A0, 3D7, 3DA, 3DD, 8DC, 8DF |
| `1E` | 236, 23D, 3B3 to 3BB, 3D8, 3DB, 3DE, 8DD, 8E0 |
| `1F` | 237, 23E, 3C8 to 3CD, 3D9, 3DC, 3DF, 8DE, 8E1 |
| `29` | 482 to 489 |
| `2A` | 48A, 48C, 48E to 493 |
| `2B` | 48B, 48D, 494 to 49A, 49C, 49E, 49F, 4A0 |
| `2C` | 4A2 |
| `2D` | 706, 90B, 90C, 90D |
| `90E` | 90F, 915, 916 |

### The USB device controller

No public description of the SWM341's controller turned up; this is read off the firmware's interrupt
handler (IRQ 77, `0x34AEC`), with the register names of the swm341-pac crate. Base `0x40005000`.

- The handler deals with one cause per call, in this order: `DEVIF` reset (bit 3), set-configuration
  (bit 0), SETUP (bit 6), then `EPIF` IN 0, OUT 0, IN 1, OUT 1, IN 2, OUT 2. It never writes `EPIF`
  and clears SETUP only through `SETUPSR`: both are views of the status registers.
- A received packet is plain memory at `RXBUF` (`0x40009000`); `RXSR` has its length (bits 22 up) and
  endpoint (bits 16 to 19). The handler writes 2 to `RXSR`, checks bit 0 and copies up to 64 bytes.
- Endpoint 1 OUT: the packet goes to `0x200006F0` (length at `0x200006D5`). If it starts `AA 2E` the
  command dispatcher (`FUN_00020f58`) runs right there, in the interrupt, and the endpoint is armed
  again (`RXCR` = 4, then 8). Anything else (upload data) is left in the buffer for the upload loop,
  and the endpoint stays unarmed until the loop has taken it: the host is held off with NAKs.
- Endpoint 2 OUT: buffer `0x200014E0`, handler `0x2F268` (sensor values, clock).
- To send, the firmware fills `TXBUFn` (`0x400` apart), writes the length to `TXTRSZ` and 8 to `TXCR`;
  afterwards it clears `TXSR` with `0xA`.
- Set-configuration arms OUT endpoints 1 and 2.

Not covered, ever: the bootloader below `0xE000` (not in the update file), which is what decides
whether a flashed image is accepted. The emulator makes patches testable; it cannot make flashing safe.

## Open questions

- Boot-time firmware-update applier: signature/CRC gate? (decides route 1)
- JPEG decoder: RAM source supported? (decides patch size)
- LCD flush entry point: exact signature, for a direct-draw command to target
- Sensor fields at offset 27+ and the clock command (`0A`) live in the second-endpoint handler, not
  yet read
- The SD-type chip's real erase-cycle budget (it holds the `SD:` files; the serial flash holds the UI)
- The fields inside a `ui.bin` widget, and the kind (label, image, level image) of each widget id
- Why the emulated firmware answers command `10` with command byte 0 (not tried on the real cooler)
- What `13` (factory reset) and `14` (delete all) do after their flags are set, and why `12` returns
  junk until a power cycle after a reset
