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

## Which firmware

**Everything below is about v2.70. The one cooler this was tried on came with v2.71 and now runs v2.70:**
it was downgraded to DeepCool's image, and on 19 Nov 2026 it reported `0x0246` before the patched image
(below) was flashed to it. So v2.70 does run on a unit that shipped with v2.71, with the `ui.bin` that
unit came with: the stored pictures, the upload screen and the boot logo all show. The rest of this
section is what was known before that, and still holds for any other cooler that reports 2.71. The image the
DeepCool app ships is `MYSTIQUE_2.70.bin`; run in the emulator it reports USB `bcdDevice` `0x0246`, and
the two bytes come straight from its data section, nothing changes them. The low byte is the minor
version as a number: 0x46 = 70. The cooler reports `0x0247`: 2.71, a build that is not in the app
(v1.2.15), newer than the one it carries. The app's compiled code holds the strings `bcdDevice` and
`deviceVersion` and a pattern for the version in a firmware file's name, which fits an app that compares
the two and would not offer this cooler its 2.70. *Inferred from the strings; the code was not read.*

What that means:

- The protocol notes agree with the cooler wherever they were tried, so the two builds are close. The
  addresses are another matter: none of them can be assumed for 2.71.
- The patch (below) is for 2.70. Putting it on this cooler is also a **downgrade** to a build that has
  never run on it, next to a `ui.bin` on the serial flash that came with 2.71. The firmware uses its
  widgets unchecked: a screen whose widget ids differ between the two would end in a call through a null
  pointer, and a cooler that does that on its boot screen has no USB to be flashed back through.
- There is no 2.71 image to go back to. No USB command reads the firmware out. Looked for on
  19 Nov 2026 and not found:
  - the app's firmware service: it logs in as a guest at `downloads.deepcool.com/official/auth/login`
    and asks `/firmware/update`; both answer 404 now, the service is gone;
  - the installers on DeepCool's download site (`deepcool.io/downloads/DeepCool-<version>-setup.exe`,
    versions from the site's own list): every one that can still be downloaded, 1.2.2 (May 2025) to
    1.2.16 (Nov 2026), carries `MYSTIQUE_2.70.bin` and no other firmware. 1.2.1 and older are 404.

  So 2.71 was never given out through the app: it looks like a build that only the factory used, for a
  reason nobody outside knows (a fix, or a change in the hardware). DeepCool's support is the one place
  left to ask.

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
  the device reboots into a bootloader that applies it. The host driver refuses type 0.
  *Confirmed that type 0 routes to the update file; the boot-time applier was not read.* Of the header
  only the type, the size (under 2 MiB) and the sum are used; the file id is not. As soon as the last
  byte is in and the sum matches, without waiting for `dcldfinish`, the firmware writes the file
  (`FUN_0001cda0`, 4 KiB at a time), turns interrupts off, resets its peripherals (`SYS.PRSTR0/1`) and
  restarts the chip (`FUN_00027380`).
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

In detail, as seen running in the emulator (v2.70):

1. `FUN_00062b40` (the image screen's logic, run when the screen is entered and every 100 ms from its
   timer) picks the slot and builds the path.
2. `FUN_000027d8` **reads the whole file into RAM** and returns a 16-byte descriptor in front of it:
   an LVGL image header (colour format 1, width, height: it finds them in the JPEG's SOF0 segment),
   the size, and a pointer to the data.
3. `FUN_0003198c` frees the previous picture, hands the descriptor to the picture widget (`90F`) with
   LVGL's `lv_img_set_src` (`FUN_0004ad34`), and starts the slide or fade animation if one is set.
4. When LVGL redraws, a custom image decoder (registered next to LVGL's own; its `info` callback is
   `0x3CAA8`) programs the **hardware JPEG decoder**: where the entropy-coded data is in RAM, its
   length, the tables, and as output address **the frame buffer itself** (RGB565, 480 x 640). No
   intermediate copy.
5. The screen's timer un-hides the picture widget every 100 ms, which invalidates it, so a still
   picture is **decoded again ten times a second**, alternately into the two frame buffers.

So the decoder takes its input from RAM already; the file system is only how the bytes get there.

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
- **Can the JPEG decoder take a RAM source** or is it hardwired to file paths? **It takes RAM.** See
  "Display path": the firmware loads the file into RAM and gives the picture widget a descriptor
  pointing at it. A direct-draw command needs to build that descriptor around bytes that came from
  USB and call what `FUN_0003198c` calls; the redraw and the decode follow by themselves.

### Risk

Flashing a rejected or malformed image with SWD locked can brick the unit with no recovery. Order of
operations: confirm SWD access → dump → understand update verification → only then flash anything.

### The patch

`tools/fw/patch.py` builds the patched image from the stock v2.70 one (checked by hash; the result goes
to `captures/`, which is not in the repository). **It runs in the emulator, and since 19 Nov 2026 on the
cooler** (see "On the cooler" below).

It appends 472 bytes of code after the image and changes sixteen bytes of it: four 4-byte jumps.

| where | stock | patched |
| --- | --- | --- |
| `0x109A4`, `main` before its loop | sets a "running" flag | the same, and clears the patch's state |
| `0x109A8`, the main loop | `lv_task_handler()` | shows a finished frame, then the same |
| `0x2F158`, end of the command dispatcher | sends the reply | command `20` starts a frame and sends its own reply; anything else as before |
| `0x34C54`, USB interrupt, endpoint 1 OUT | "does this packet start `AA 2E`?" | while a frame is coming in the packet is picture data; otherwise as before |

The command, on endpoint 1: `AA 2E 20 <size:u32> <sum16 of the JPEG:u16>`, then the JPEG in packets of up
to 64 bytes. The reply's status byte says what to do:

| status | meaning |
| --- | --- |
| `10` | go ahead, send the JPEG |
| `11` | not now: the last frame is not on screen yet, or the buffers are still being made. Ask again |
| `12` | refused: size 0 or more than 128 KiB |
| `00` | what the **stock** firmware answers to every command it does not know. `deephack.device` takes it for "no direct drawing here" and sends no picture data (data without an upload in progress would be left in the endpoint's buffer, and the endpoint would stay shut) |

How it works:

- The frame is received **inside the USB interrupt**, so its speed is the USB's and not the main loop's,
  into the one of two 128 KiB buffers that is not on screen. The buffers come from the firmware's heap
  the first time the command is used (the main loop makes them; the command says `11` until then) and
  are kept. Each has an `lv_img_dsc_t` in front, as the stock loader makes it (`0x50078001`: raw,
  480 x 640).
- When the last byte is in and the sum matches, the **main loop** gives the buffer to the picture widget
  with `lv_img_set_src` (`0x58D34`), the call the stock firmware makes after reading a file. LVGL
  redraws and the hardware decoder writes into the frame buffer. No file, no screen change.
- A frame with a wrong sum is not shown. A frame that is not complete two seconds after its command is
  given up, so a host that dies half way cannot leave the cooler taking commands for picture data.
- It only acts on the image screen (`90E`, mode 2); a frame that arrives on another screen is dropped.
  The screen needs one stored picture: with none the stock firmware hides the picture widget every
  100 ms. A stored picture comes back with `08` (show), but on the cooler only for a slot other than
  the one already selected (see "On the cooler").
- Its state is 36 bytes of RAM at `0x2000F000`. The firmware's stack starts at `0x2000B418` and grows
  down; in the emulator nothing touches the RAM above it.

What was needed to know for it, all seen in the emulator:

- **The main loop is `for (;;) lv_task_handler();`** (`0x109A8`). Everything else, the screen logic
  included, is an LVGL task: the screen selector and the image screen's logic both run from the screen's
  timer, every 100 ms. A frame handed over there would wait up to 100 ms, so the patch sits in the loop.
- **The heap** (`lv_mem_alloc`, `0x5C3C8`) is 8 MiB at the start of the SDRAM, `0x80000000`; the frame
  buffers come from it too (`0x800061A0` and `0x8009C1C0`). After an upload about 4 MiB of it had ever
  been in use, 2 MiB of that the upload's staging buffer.
- **The reply routine** (`0x34D84`) writes command and status only for the commands it knows; for any
  other it sends zeros, which is why the stock firmware "answers" unknown commands with command byte 0
  and status 0. It ends in `usb_send(endpoint, data, length)` at `0x3488C`.
- **A packet that starts `AA 2E` is always taken for a command**, during a stock upload too (`0x34C54`):
  the dispatcher rejects it for its missing `HIDC` and the 64 bytes never reach the staging buffer. One
  packet in 65 536 on average, so about one upload in 140 for a 30 KB picture would stall until the
  upload loop gives up. Not seen happening; read off the code.

What the emulator run shows (`tests/test_direct_draw.py`): three frames in a row are on the emulated LCD,
decoded alternately from the two buffers, with still one file on the card; a wrong sum and a stalled
frame are handled as described; an upload and `08` work afterwards; the stock image refuses nothing and
is sent nothing.

#### On the cooler

Flashed on 19 Nov 2026 with `python tools/fw/flash.py patched --i-accept-the-risk`, to the one cooler,
which ran DeepCool's v2.70 at that point. Watched with the webcam, a frame every two seconds.

- **The bootloader takes a changed, longer image.** The screen showed "Loading..." while the firmware
  stored the file, the cooler left the bus one second after the last packet and was back 12 seconds
  later, with the screen dark in between and the DeepCool logo after. It reports `0x0246` as before
  (the version bytes are not touched by the patch), the same id to `12`, and answers `20` as the patch
  does. So whatever the bootloader checks, it is not a sum or signature over the image that the patch
  breaks, and it writes at least to the end of the file (the patch's code is the file's last 472
  bytes, in the same 4 KiB of flash as the stock image's end). The stored pictures were still there.
- **Frames from USB are shown.** Three test cards five seconds apart were each on screen in the next
  camera frame; `draw_jpeg` returned after 10 to 13 ms for cards of 7 to 9 KB.
- **Speed, as the host sees it** (frames sent back to back, each as soon as the cooler said "go ahead";
  none had to be given up): 8.6 KB frames 19.6 a second, 41 KB (a real picture at quality 75) 16.2 a
  second, 72 KB (quality 92) 10.2 a second. The camera is too slow to count them: it confirmed that the
  last frame of each run was the one left on screen.
- **Getting the stored picture back is not as described above.** `08` for the slot that is already
  selected leaves the drawn frame on screen, and so does sending mode 2 again. `08` for another slot
  shows that slot's picture, and from there `08` for the first one works. Drawing works again after.
  Leaving the image screen (mode 1, 0.15 s, mode 2) brings the stored picture back as well, with
  nothing of the sensor screen left in the webcam's picture 0.4 s later; a frame sent while the sensor screen is up gets
  "go ahead" and is dropped, as the patch is written.
- deephack was started again afterwards and its Scene, a stored animation, came back on screen. Whether
  that took an upload was not looked at, so an upload to the patched firmware on the cooler is not
  proven yet.

- **After an animation the patch takes one frame and no more.** Show an animation (`08`, type 1), then
  a still, then draw: the first frame gets "go ahead", every one after it "later" (`11`), for as long as
  was tried (3 s, a frame asked for every 50 ms). It makes no difference whether anything was drawn
  while the animation was on. Over the animation itself it is the same, and the drawn frame is not seen:
  the animation's next frame replaces it. Leaving the image screen (mode 1 for 0.15 s, then mode 2)
  clears it, and frames are taken again at once, also when the slot selected is still the animation's
  until `08` follows. *Seen, not explained.* "Later" means the last frame is still waiting to be shown
  (phase 2) or a buffer is missing, and it is the main loop's hook that ends both; so either that call
  site is not reached while the image screen is in this state (an inner loop that calls
  `lv_task_handler` itself would do that), or the patch's 36 bytes of state are written over. The
  emulator can tell which: `tests/test_direct_draw.py` has no animation in it.
- **The engine draws with this firmware** (`deephack/engine.py`, `_draw_step`): the Visualizer's frames,
  12 to 30 KB each at quality 85, one every 50 ms while the sound changes. With a made-up beat 164
  frames went out in 11.7 s and none was refused in the end; between frames the cooler answers "later"
  to about two asks in three, so 50 ms is its own pace. Before the first frame the engine leaves the
  image screen and comes back, then shows its own still, because of the item above.

- **The side light follows drawn frames.** With the light's source set to the screen (`02`, fourth
  byte 2), plain red, green and blue frames were drawn and the webcam read the RAM sticks next to the
  pump head, where the light falls; the control was the same frames with the source set to the fixed
  colour (fourth byte 1), then the first setting again. Mean R G B there, 0 to 255: red frame 8.5 0.2
  0.0 against 2.0 0.1 0.4 in the control; green 0.2 14.6 1.1 against 0.0 9.8 2.0; blue 1.3 1.3 44.8
  against 1.4 1.3 44.5. Two readings each, equal to a few tenths, and the same again after the control.
  So for red and green the light takes its colour from the frame on screen, wherever that came from,
  and not from the stored file when it is shown. Blue shows no difference: not explained, the screen's
  own blue on the sticks may simply drown it.

- **Every mode's pictures are drawn now** (`_draw_still`): Weather, System, Claude Code, Ticker and Now
  Playing one after the other in one session, each one `20` with a JPEG of 27 to 68 KB that took 36 to
  88 ms to send, and no upload at all. They go over a plain black still stored once in deephack's slot.
  An animation (Scene) in between is stored as before, and the pictures after it are drawn again.

Not looked at on the cooler: a frame with a wrong sum, a frame the host abandons half way, how far a
drawn frame is behind its command (the webcam's own delay is not known), and how the patched firmware
behaves over hours.

What the emulator cannot show:

- **Whether v2.70 runs on a cooler that came with v2.71** (see "Which firmware"). *It does, on this one.*
- **Whether the bootloader takes the image.** *It did, see above.* The update file is a bare image: vector table first, the
  compressed data section last, no header and no trailer, and neither a CRC-32 nor a byte or word sum of
  the file is anywhere at its end. So there is no room in the file for a signature, and a check, if there
  is one, can only be a sanity check or a sum kept inside the image. That is encouraging and proves
  nothing. The patched file is also 472 bytes longer, and what the bootloader does with length is
  unknown. (The vendor app's update code is compiled bytecode, `out/main/index.jsc`, and was not read.)
- **Whether the flash after the image is free.** The code goes to `0x7C218`, right behind the image.
  The firmware was not seen writing to its own flash, but only the paths that ran were looked at.
- **Speed.** Emulated time is not real time. On the cooler a frame costs its USB transfer (full speed:
  at most about 1 MB/s) plus one decode. *Measured since, see above.*
- **The decoder itself.** The model decodes the file with Pillow (#18); the real one has only ever been
  given what the stock loader hands it, and the patch hands it the same thing from another address.

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
| drawing at all | widgets marked visible in `ui.bin` (a screen that is not is made hidden, and nothing is redrawn) |
| a stored picture on screen | a model of the JPEG decoder (`tools/emu/jpegdec.py`) |

It then runs its **main loop**: the logo screen for 4 s, then the screen for its mode, USB commands
answered, the tilt sensor polled over I2C. The frame buffer is saved as a picture after every run
(`captures/emu_screen.png`). With `--flash blank` the firmware shows what it does without its UI data:
a red cross and "Couldn't open 'S:spi:///ui.bin'".

`tools/emu/bench.py` puts deephack's own driver (`deephack.device.Mystique`) in front of the emulated
firmware. What works that way: the device descriptor (3633:0009), commands and their replies (`12`
answers an empty id on a fresh card), and **an upload**: the driver's `upload_jpeg` ends with
`IMG/000_<id>/angle_0.jpg` on the emulated SD card, byte for byte what was sent, and **showing it**:
after `show_slot(0)` the picture is in the emulated frame buffer. The whole run (boot, upload, show)
takes about ten seconds.

The JPEG decoder model does not decode from the tables the firmware writes to the decoder's registers.
The header is in RAM right in front of the data the firmware points the decoder at, so the model finds
it there and lets Pillow decode the file. A patch that programmed the tables wrongly would not be
caught.

Still to do: the widgets of the other screens (see below). The dashboard and the recorder end in a
call through a null pointer, because the firmware uses its widgets unchecked.

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
| a widget | a blob: u32 size, u8 type, 3 unused bytes, u32 id, the rest is the widget's own structure (see below); then u32 string count, u32 (unknown), the strings as {u32 length, bytes}; then u32 child count and that many u32 offsets of child widgets |
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

Inside a widget's structure. The firmware has a setter by property name for the common part
(`FUN_0000711c`), which gives both the names and the offsets:

| offset | field |
| --- | --- |
| `0x10` | `name`, up to 32 characters |
| `0x30` to `0x40`, `0x50` | filled in at run time (parent, the LVGL object, class data) |
| `0x45` | `visual`: 0 makes the widget hidden. This goes for a screen too, and nothing on a hidden screen is ever drawn |
| `0x46` | `enable` |
| `0x48` `0x4A` `0x4C` `0x4E` | `left`, `top`, `width`, `height` (s16) |
| screen `0x5C` | background colour, red in the low byte |
| image `0x60` | opacity |
| image `0x64` | id of its picture in the resource table (0: none) |
| label `0xE0` and on | three style blocks of 24 bytes at `0xE8`, `0x100`, `0x118`; the defaults routine (`0x10E40`) fills them with "no font, no colour" |

An image widget is an LVGL `lv_img` (made by `FUN_000078d0`); the base and screen setup routines are
at `0x152CC` and `0x10DDC`.

`uibin.py` writes a file with every screen the firmware knows. The logo, upload and image screens have
the widgets their code uses (without pictures or fonts: the resource sections are empty); the others
are blank, so the stock dashboards cannot be shown yet.

### Screens

The firmware's code is tied to `ui.bin` by ids. A screen's widget carries the screen id; at start
(`FUN_0002e340`) the firmware registers three handlers per screen id: **on entering** (it looks up its
widgets by id here and keeps the pointers), **on leaving**, and **on the screen's timer**. The table
is in the compressed data section, so the handlers have no visible reference in the image (and Ghidra
did not find most of them).

| screen | what it is |
| --- | --- |
| `01` | boot logo: four pictures, one per rotation of the panel (`4A4`, `912`, `913`, `914`), and two labels (`918`, `919`). Its timer handler runs twice: it starts a 4 s timer, then sets the "ready" byte (`0x200051A8`) and goes to `2D`. The screen selector (`FUN_00020628`) does nothing before that byte is set |
| `02` to `10`, `17` to `1F` | dashboards, 10 to 24 widgets each (mode 1; which one by layout and rotation) |
| `29` `2A` `2B` | recorder (mode 3): made in code from line objects and a chart, on top of 8 to 13 widgets |
| `2C` | upload. Entered when command `0F` sets its flag. **The file is received by this screen's "leaving" handler** (`0x69A88`): a loop around the upload state machine (`FUN_000200ec`) that ends on success, on a flag, or after about 15.7 million rounds without a packet; then the next screen loads |
| `2D` | where the firmware goes after the logo and when it has nothing to show; timer every tick |
| `90E` | stored pictures and animations (mode 2). `90F` is the picture; `915` (150 x 200) and `916` (325 x 28) are two pictures shown instead when nothing is stored, placed and turned to match the rotation. All three are images |
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

- Boot-time firmware-update applier: it took the patched image (see "On the cooler"), so no signature
  and no sum the patch breaks. What it does check, if anything, is still unknown: a broken image may
  well be written as readily
- Is SWD locked? (the only recovery path if a flashed image does not start)
- Why the patched firmware stops taking frames once an animation has been on the image screen (see "On
  the cooler"): the one known way to make direct drawing hang, worked around in the engine, not fixed
- Why `08` for the slot already on show does not bring the stored picture back after a drawn frame
  (seen on the cooler; in the emulator test `08` after a new upload does, which is not the same case)
- Does the firmware ever write to its own flash behind the image?
- Sensor fields at offset 27+ and the clock command (`0A`) live in the second-endpoint handler, not
  yet read
- The SD-type chip's real erase-cycle budget (it holds the `SD:` files; the serial flash holds the UI)
- The kind (label, image, level image) of each widget id on the dashboard and recorder screens, and
  the label's and level image's own fields
- Whether the real cooler also decodes a still picture ten times a second (seen in the emulator only)
- Why the emulated firmware answers command `10` with command byte 0 (not tried on the real cooler; the
  reply routine has a second path that sends on endpoint 2, not followed)
- What `13` (factory reset) and `14` (delete all) do after their flags are set, and why `12` returns
  junk until a power cycle after a reset
