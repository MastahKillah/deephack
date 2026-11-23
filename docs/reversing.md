# Reversing the DeepCool MYSTIQUE's LCD

The MYSTIQUE is a liquid cooler with a 480 x 640 LCD on its pump head. DeepCool's app can show sensor
readouts on it, or images you upload, and every image is written to the cooler's storage before it can
be shown. This is how that became a screen a PC can draw on sixteen times a second: the USB protocol,
the firmware run in an emulator, and a patch of 504 bytes.

The short version of each step is here. The long one, with every claim marked as traced in the code,
seen in the emulator or seen on the cooler, is in [firmware-analysis.md](firmware-analysis.md); the
wire protocol is in the [README](../README.md#protocol).

## 1. The protocol, from the vendor's app

The app is Electron. Started with `--inspect-brk`, its main process takes a debugger, and a script
injected there ([`tools/hook.js`](../tools/hook.js), through [`tools/cdp.js`](../tools/cdp.js)) logs every
USB transfer and IPC call without changing what the app does.

The cooler is a full-speed, vendor-class USB device (`3633:0009`) with two bulk endpoint pairs: one for
commands and uploads, one for a sensor feed once a second. A command is 48 bytes:

```
AA 2E <cmd> <payload, zero padded> 'H' 'I' 'D' 'C' <sum16>
```

An image goes up as a 64 byte header (`"DCLd"`, size, checksum, a file id), the JPEG in 64 byte
packets, and a `"dcldfinish"` marker, after which the cooler writes it to its storage. Then a second
command shows it. There is no way to show a picture without storing it first, and the cooler keeps
nine.

Trying the command numbers the app never sends found two that were not harmless: `13` is a factory
reset and `14` deletes every stored image.

## 2. The firmware

No command reads the firmware out, but the app carries an update image, `MYSTIQUE_2.70.bin`. It is a
plain binary for a Synwit SWM341 (a Cortex-M33 class microcontroller), with LVGL for its screens, which
it loads from a `ui.bin` on a serial flash; pictures live on an SD card.

The first decompile was made with the image at address 0, and half of its functions came out as empty
endless loops. The image is linked at `0xE000`, above a bootloader of 56 KiB that is not in the update
file. What settled it: with base `0xE000`, eleven words in the image point exactly at the start of a
string; with base 0, none do.

The chip was identified by its registers: everything the start-up code touches has a name in the
SWM341's register description (LCD controller, JPEG decoder, USB, SDIO, the SDRAM controller).

## 3. An emulator, so that the one cooler is not the test bench

[`tools/emu/boot.py`](../tools/emu/boot.py) runs the stock image in Unicorn. Hardware registers are
plain memory until they are modelled; a run stops when the firmware spins on a register that never
changes, which names the next thing to model. In the order the firmware asked for them: the PLL's lock
flag, the SDRAM controller, an SD card and its host controller, a FAT volume on the serial flash with a
`ui.bin` on it, the hardware divider (the loader checks its answers), the USB device controller, and
the JPEG decoder.

It now reaches LVGL's main loop, and [`tools/emu/bench.py`](../tools/emu/bench.py) puts the real host
driver in front of it: an upload ends as a file on the emulated SD card, byte for byte, and "show" puts
the picture in the emulated frame buffer.

One bug worth passing on: an interrupt can arrive inside a Thumb IT block. The handler has to start
with the IT state cleared, or its first instructions stay conditional on the interrupted code's flags.
Here the handler's `push` was skipped once in a few million ticks, and it returned to garbage.

## 4. The patch

[`tools/fw/patch.py`](../tools/fw/patch.py) appends 504 bytes of code to the image and changes twenty
bytes of it: five 4-byte jumps. It adds one command, `20`: a size and a checksum, then a JPEG. The
frame is received inside the USB interrupt, so its speed is the bus's and not the main loop's, and
handed to the firmware's own JPEG decoder from the main loop. Nothing is stored.

It was written against the emulator ([`tests/test_direct_draw.py`](../tests/test_direct_draw.py)), then
sent to the cooler through the update channel the vendor's app uses. The cooler was off the bus for
twelve seconds and came back drawing: a 41 KB frame sixteen times a second, an 8 KB one twenty times.

The emulator did not catch everything. On the cooler, once an animation had played, the first version
took one frame and answered "later" to every one after it. A still shown after an animation keeps the
animation's frame time as its timer, LVGL's task loop never comes round, and the patch was only called
from outside that loop. Version 2 is also called from inside it.

## What is not known

- **One cooler.** Everything on hardware was seen on a single unit.
- **v2.71.** That unit shipped with 2.71, a build that is in no version of the app that can still be
  downloaded. Flashing the patched 2.70 was a one-way downgrade.
- **The bootloader.** It does the flashing and is not in the update file. A flash that goes wrong
  cannot be undone over USB.

The repository does not carry DeepCool's firmware. The patch is a table of bytes
([`deephack/firmware_patch.py`](../deephack/firmware_patch.py)) that the app applies to the image from
an installed copy of DeepCool's app, checked by its hash.
