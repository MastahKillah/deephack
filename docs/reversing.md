# Reversing the DeepCool MYSTIQUE's LCD

The MYSTIQUE is a liquid cooler with a 480 x 640 LCD on its pump head. DeepCool's app can show sensor
readouts on it, or images you upload, and every image is written to the cooler's storage before it can
be shown. This is how that became a screen a PC can draw on fifty-five times a second, with its own
logo when it starts and the lights beside it in the PC's hands too: the USB protocol, the firmware run
in an emulator, a patch of 2 KB, and the bootloader read out through that patch.

(When this was first written the patch was 504 bytes and the app drew sixteen frames a second. Steps 5
to 9 are what came after.)

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

## 4. The patch: drawing without storing

[`tools/fw/patch.py`](../tools/fw/patch.py) appends code to the image and changes twenty-four bytes of it:
six 4-byte jumps (five until version 16, which took the light's task). Its first job, in 504 bytes, was one new command, `20`: a size and a checksum, then
a JPEG. The frame is received inside the USB interrupt, so its speed is the bus's and not the main
loop's, and handed to the firmware's own JPEG decoder from the main loop. Nothing is stored.

It was written against the emulator ([`tests/test_direct_draw.py`](../tests/test_direct_draw.py)), then
sent to the cooler through the update channel the vendor's app uses. The cooler was off the bus for
twelve seconds and came back drawing.

The emulator did not catch everything. On the cooler, once an animation had played, the first version
took one frame and answered "later" to every one after it. A still shown after an animation keeps the
animation's frame time as its timer, LVGL's task loop never comes round, and the patch was only called
from outside that loop. Version 2 is also called from inside it.

## 5. How fast, and what the limit is

The first numbers, sixteen frames a second, were wrong about the cooler: that was the pace at which the
host asked for pictures. Timed step by step ([`tools/drawbench.py`](../tools/drawbench.py)):

- **It showed a picture every 36 ms and no faster, however small the picture was**: about 27 a second.
  That looked like the panel's own turn, and was two of them. The panel turns every 18 ms. After each
  frame LVGL copied it from one of its two screen buffers into the other, 614 KB, which took 20 ms, and
  its redraw task came round every 33. The vendor's own video widget skips that copy, through a routine
  the patch can call too; with the redraw task set to 5 ms, **55 pictures a second reach the glass**.
- The bytes were the surprise. The first patch took them at 820 KB a second; the versions with more in
  them took large frames at 540, with no line of the frame path changed. The cause was 25 microseconds
  wide: the patch made the USB endpoint ready for the next packet after it had copied the last one, and
  whether that was done before the host sent again went by how fast 64 bytes were copied where the loop
  happened to lie in the flash. A packet that comes too soon is sent again whole. Making the endpoint
  ready first, and copying after, gave **997 KB a second for every size**: a frame of 33 KB in one turn
  of the panel, where 26 KB had been the most.
- Other traffic on the same USB slows it. With a webcam sending pictures next to it, the same frames
  went 16 a second.

So what a frame may weigh is what the bus carries in the time the frame is to be shown: 28 KB for a
video of 30 a second, and the app trades JPEG quality for that. (The later patches take 874 KB a second,
not 997. Seven packets where eight would go, and it is not known why.)

One fault hid in this for a week: **an update that was sent whole and never happened.** The firmware
puts an upload into 2 MiB of its heap in one piece, asked for after its "Loading" screen has taken
nearly 3 MB, and when there is no such piece it drops the upload without a word to the host. The
patch's three frame buffers were what was missing, by 36 KB, and only with a large stored picture on
show. Found by walking the cooler's heap over USB, block by block, at the moment an upload was
announced. The patch gives its buffers back before an upload now.

## 6. A part of the picture

A page with a video in it is mostly a picture that stands still. Version 3 of the patch takes a JPEG
of a rectangle and puts it on the page that was drawn last.

It needed no decoder work, because of something the stock firmware already does: its image draw
routine works out where in the frame buffer a picture's top left corner is and hands that address to
the hardware decoder, so a picture is decoded straight into its place, when the area to redraw is
exactly the picture's. And LVGL starts a redraw at the topmost object that covers the area. So the
patch makes a second, small picture widget on top of the page, gives it the part, and a new part costs
its own decode and nothing else.

On the cooler, checked with a webcam on the screen: parts land exactly in their place. A page with a
video in it is 21 KB a frame as a part where the whole page was 29, at a higher JPEG quality and the
full 27 a second.

## 7. The bootloader, read out through the patch

The bootloader does the flashing and is in no file, so nobody knew what it would do with an image
that was a good deal longer. Version 4 of the patch adds a command that reads memory: an address and a
count in, up to 39 bytes back in the ordinary 48 byte reply. The 56 KiB below the firmware came out in
under a second ([`tools/fw/dump.py`](../tools/fw/dump.py)). One caution: an address that is not memory
stops the chip until it is switched off and on, so the host only asks for flash and RAM.

What it does, read off its code:

- At every start it mounts the SD card and looks for `FW_update.bin`. Without one it jumps to the
  firmware.
- With one it erases the firmware's whole area, up to the end of the chip's 512 KiB of flash, and
  writes the file 4 KiB at a time. There is no check of the size, no checksum and no signature. A file
  over 466 944 bytes loses its end.
- The file is deleted only after the install, and after it has been copied to two other names. A
  start that is cut short finds it and installs it again.

## 8. Its own start screen

DeepCool's logo at the start is a picture in `ui.bin`, on the serial flash that nothing reaches over
USB. It cannot be replaced, but it can be covered, with the trick from step 6: the first time the main
loop comes round, which is before LVGL has drawn anything, the patch puts a picture of its own on top
of the start screen. DeepCool's pictures under it are never decoded.

The picture has to travel in the firmware image, in the flash that is free behind DeepCool's code:
under 16 KB for patch and picture together, which step 7 showed is written like the rest. A 480 x 640
JPEG of a plain logo is 14 KB with the standard Huffman tables every picture had been sent with so far,
and 11 KB with tables made for it. The cooler's decoder takes those too (drawn on the cooler and
photographed, the two are the same picture).

The first try showed the logo for four seconds and then DeepCool's again: after the start screen the
firmware goes to a second screen with the same logo and waits there for a host, which is most of a PC's
start. The patch's memory command found that screen's object, and version 6 covers it as well. The
patch is 1120 bytes of code now, and the picture 10 780.

## 9. The lights

Beside the screen the pump head has 14 lights, seven up each side, of the WS2812 kind on one wire. Set
to follow the screen, the stock firmware gives each the colour of one pixel, five in from the
picture's edge, ten times a second, and sends the fourteen as they are: no fade, and a white page with
a dark border is fourteen dark lights. (It also reads the buffer that is being drawn into, not the one
on the glass, so with drawn frames it was a picture behind.)

Version 16 of the patch adds a command, `24`, with which the host says the fourteen colours and how
quickly to go there, and takes over the light's task, the sixth jump: while the host's colours are on,
it comes round every 16 ms, moves each value a share of the way and lets the firmware's own loop send
them. Three seconds without the command and the cooler picks for itself again. In the emulator the wire
was read back bit by bit; on the cooler, which light is where was found with the screen black, one
light white at a time and a webcam on what it lit.

What the colours are is the app's business from there: a share of the picture's edge for each light,
weighted by how much colour is in it, or whatever a mode says, level meters for music, a fire, two
teams' colours, the CPU's temperature up one side and the GPU's up the other.

The patch is 2028 bytes of code now. With it the cooler was drawn on for ten hours in one go, video,
TikTok and the rest: 778,000 pictures and 400,000 light commands, none refused.

## What is not known

- **One cooler.** Everything on hardware was seen on a single unit.
- **v2.71.** That unit shipped with 2.71 (and so did the second owner's who reported, in November 2026), a build that is in no version of the app that can still be
  downloaded. Flashing the patched 2.70 was a one-way downgrade.
- **A way back from a firmware that does not start.** The bootloader only installs what is on the SD
  card, and only a running firmware can put a file there. An interrupted flash repeats itself; an image
  that is whole and wrong cannot be undone over USB.
- **Why the later patches take 874 KB a second and not 997.** Seven packets where eight would go.

The repository does not carry DeepCool's firmware or its bootloader. The patch is a table of bytes
([`deephack/firmware_patch.py`](../deephack/firmware_patch.py)) that the app applies to the image from
an installed copy of DeepCool's app, checked by its hash.
