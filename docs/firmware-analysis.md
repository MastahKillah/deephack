# MYSTIQUE firmware analysis

Notes from reading a decompile of the MYSTIQUE's firmware image. This supplements the protocol
section in the [README](../README.md): the README describes the wire protocol as observed from the
host side; this describes what the firmware does with it internally, and what that means for ideas
like direct drawing.

Confidence is marked per item. "Confirmed in code" means traced in the decompile; "inferred" means
deduced from surrounding code and not fully followed; "unverified" means a reasonable guess only.
Addresses and function names are from one particular decompile and are not stable identifiers.

## Platform

- **MCU:** Synwit (ARM Cortex-M). The config system reads a `SynwitManifest.cfg`; the display driver
  is selected by name (`jlt35002a_v3` / `jlt35002a_v3_spi`). *Confirmed in code.*
- **UI stack:** LVGL. The firmware instantiates `lv_arc`, `lv_btn`, `lv_chart`, `lv_label`, `lv_line`
  etc. and loads its screens from a `ui.bin` resource table. The dashboard labels/units are baked
  into these resources, which is why only the numbers are host-controllable. *Confirmed in code.*
- **Display:** 480x640, configured via `peripheral.display.hres` / `.vres` (0x1e0 x 0x280). *Confirmed.*
- **Storage:** external **SPI flash** (not a removable SD card), exposed through a filesystem with an
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

## Open questions

- Boot-time firmware-update applier: signature/CRC gate? (decides route 1)
- JPEG decoder: RAM source supported? (decides patch size)
- LCD flush entry point: exact signature, for a direct-draw command to target
- Sensor fields at offset 27+ and the clock command (`0A`) live in the second-endpoint handler, not
  yet read
- Whether `SD:` SPI flash is NOR or NAND, and its real erase-cycle budget
- What `13` (factory reset) and `14` (delete all) do after their flags are set, and why `12` returns
  junk until a power cycle after a reset
