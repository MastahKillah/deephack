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

It then **draws its first screen**: a red cross and "Couldn't open 'S:spi:///ui.bin'", and idles in a
loop behind it. The frame buffer is saved as a picture after every run (`captures/emu_screen.png`).

That is the wall for booting the *whole* firmware: `ui.bin` (the screens, built with Synwit's UI tool)
and `SynwitManifest.cfg` exist only on the cooler's serial flash. They are not in the update file or
in the DeepCool app, and no USB command reads a file back. Ways on:

1. Read the cooler's serial flash (hardware access: the flash chip or the debug port).
2. Work out the `ui.bin` format from its loader and write a minimal one that satisfies it.
3. Leave the UI out: run only the parts a direct-draw patch touches (USB receive, JPEG decode, LCD
   flush) as functions in the emulator, with the hardware JPEG decoder and the USB controller modelled.

Still to model for any of them: the USB device controller, to feed the firmware the `AA 2E` command
frames and uploads the real driver sends, and the JPEG decoder, without which no stored picture
reaches the frame buffer.

Not covered, ever: the bootloader below `0xE000` (not in the update file), which is what decides
whether a flashed image is accepted. The emulator makes patches testable; it cannot make flashing safe.

## Open questions

- Boot-time firmware-update applier: signature/CRC gate? (decides route 1)
- JPEG decoder: RAM source supported? (decides patch size)
- LCD flush entry point: exact signature, for a direct-draw command to target
- Sensor fields at offset 27+ and the clock command (`0A`) live in the second-endpoint handler, not
  yet read
- Which storage device holds the `SD:` files (the SDIO device or the QSPI flash), and its real
  erase-cycle budget
- What `13` (factory reset) and `14` (delete all) do after their flags are set, and why `12` returns
  junk until a power cycle after a reset
