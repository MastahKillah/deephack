"""Build a MYSTIQUE firmware that draws a JPEG straight from USB, without storing it (issue #14).

    python tools/fw/patch.py [stock.bin] [-o patched.bin] [--list] [--table]

This only writes a file; nothing here talks to the cooler. Try the result in the emulator
(tools/emu/bench.py --firmware patched.bin, and tests/test_direct_draw.py). Flashing is another matter
(flash.py, and the app's own "Firmware" section): the bootloader that takes the image is not in the
update file, and a broken image may leave a cooler that cannot be recovered without a debug probe.

--table writes the patch as the bytes it changes to deephack/firmware_patch.py, which is how the app
has it: without this assembler, and without DeepCool's image. Do that after every change here (a test
compares the two).

The stock image (v2.70 only, checked by hash) is DeepCool's and is read from the installed app. The
patch appends about 2000 bytes of code after it and redirects six places in it:

    where                         stock                         patched
    0x109A4  main, before loop    sets a "running" flag         ... and clears the patch's state
    0x109A8  main loop            lv_task_handler()             show a finished frame, then the same
    0x64088  lv_task_handler      "start over with the tasks"   show a finished frame, then the same
    0x2F158  command dispatcher   reply(command, 0, status 0)   command 20 starts a frame, then the same
    0x34C54  USB, endpoint 1 OUT  "is this packet a command?"   while a frame is coming in, it is data
    0x72ED0  the light's task     the stock loop for the light  the host's colours, eased; then the same

The new command, on endpoint 1 like the others:

    AA 2E 20 <size:u32> <sum16 of the JPEG:u16> <x:u16> <y:u16> <width:u16> <height:u16> ...
    Width 0: the JPEG is the whole page, 480 x 640. Otherwise it is a part of the picture, that size, to be
    put at x, y on the page that was drawn last (version 3). Reply status (payload byte 0):
        10  go ahead: send the JPEG in packets of up to 64 bytes, nothing after it
        11  not now: the last frame is not on screen yet, or the buffers are still being made. Ask again
        12  refused: size 0 or more than 128 KiB
        13  a part, and no page of ours is under it (none drawn yet, or the stock firmware has shown a
            stored picture since): draw the page, then the part
    The byte after the status is the patch's version (VERSION; 0 from the first patch, which had none).
    The stock firmware answers any unknown command with status 0, so 0 means "no direct drawing here".

How it works. The frame is received inside the USB interrupt, into the one of three 128 KiB buffers that
neither picture on screen shows (they are taken from the firmware's 8 MiB heap the first time the
command is used and kept). When all of it is in and the sum matches, the main loop gives the buffer to
the picture widget of the image screen with lv_img_set_src, exactly as the stock firmware does with a
file it has read into RAM; LVGL redraws and the hardware decoder writes into the frame buffer. No file,
no screen change. A frame that stalls for two seconds is given up, so a host that dies half way cannot
leave the cooler taking commands for picture data.

A part of the picture (version 3) goes to a second, small picture: an lv_img the patch makes as the
child of the picture widget, puts at x, y and gives the part's buffer. The stock firmware decodes a
picture straight into the frame buffer where its widget lies, when the area to redraw is exactly that
widget's, and LVGL starts a redraw at the topmost object that covers the area: so a new part costs its
own decode and nothing else. A new page hides the part; so does the stock firmware showing a stored
picture. When the part appears, moves or changes size the page is drawn whole again, because a bit of a
picture is decoded another way (into RAM, and kept there by its address) that the patch stays clear of.
The size in the command must be the JPEG's own: the firmware decodes what the JPEG says into the place
the command names (deephack.device.part_box reads it from the JPEG and checks it against the screen).

"The main loop" is two places. lv_task_handler only returns when no task is due, and it can go without
returning for as long as the image screen's timer is faster than the screen's work: while an animation
plays, and afterwards too, because a still inherits the animation's frame time as its timer (issue #22:
version 1 hooked only the call in main and then took one frame and no more). So version 2 also shows a
finished frame where lv_task_handler starts over with its task list, which it does after every task it
has run.

It needs the image screen (mode 2) with at least one stored picture: with none, the stock firmware
hides the picture widget. A frame that arrives on another screen is dropped. The JPEG must be what
the stock firmware shows: 480 x 640, baseline, 4:2:0 (deephack.device.encode_jpeg).

A second command, 21, reads the cooler's memory (version 4): AA 2E 21 <address:u32> <count:u8>, and the
reply carries up to 39 bytes from there in place of status and the rest. It only reads. It is how the
parts of the cooler that no file holds can be looked at: the bootloader below the firmware, and what
lies after it. An address that is not memory stops the cooler until it is switched off and on:
deephack.device.Mystique.peek keeps to the flash and the RAM.

A third, 22, writes (version 10): AA 2E 22 <address:u32> <count:u8> <value:u32> puts the value at the
address and answers as 21 does, with `count` bytes read back from there. It is for trying a setting on
the running firmware (how often LVGL redraws, say) before a patch is made of it: what it changes is gone
with the next start. deephack.device.Mystique.poke keeps to the RAM.

The light (version 16). The strip on the pump head is 14 lights, seven up the left side of the screen
and seven down the right. The stock firmware, told to follow the screen (command 02, fourth byte 2),
gives each the colour of one pixel of the frame buffer, 5 pixels in from the edge (0x2FD5C, ten times a
second), and sends the 14 when one of them has changed, with nothing in between: see docs/firmware-
analysis.md. Command 24 has the host say what they show:

    AA 2E 24 <13 lights: R G B each> "HIDC" <sum16> <the 14th: R G B> <on:u8> <pace:u8>

64 bytes in one packet: the command's own 48, whose 39 bytes of payload hold 13 lights, and what did not
fit after them (the USB handler has the whole packet in RAM; the dispatcher only looks at 48). The lights
in the stock firmware's order: 0 at the bottom of the left side, 6 at its top, 7 at the top of the right
side, 13 at its bottom. `on` 1: the lights go to these colours; 0 (which is what a plain 48 byte command
24 says): the cooler picks them again. `pace`: how much of what is left to a colour is covered every
16 ms, in 256ths; 0 is at once. Answered as command 20 is, with GO.

The colours are where the lights go to, not what they show: the task that runs the stock loop for the
light (35 ms) is the patch's now while the host's colours are on, comes round every 16 ms, moves each of
the 42 values a step on (8.8 fixed point), puts the result where the stock loop has its 14 colours and
lets that send them. So the host says a colour once and the cooler fades to it; nothing goes over USB for
the fade. The stock picking still runs and is written over before the loop looks. When no command 24 has
come for three seconds the host is taken to have gone, and the cooler picks again: a host that keeps the
light sends its colours once a second at least. Command 02 decides as before whether the lights follow
the screen at all.

The start screen (version 5). While it starts the cooler shows DeepCool's logo for four seconds: a
picture from a file on its serial flash, which nothing reaches over USB. The patch puts deephack's own
over it: logo.jpg (made by logo.py), which lies after the patch's code in the image. The first time the
main loop comes round, which is before LVGL has drawn anything, the JPEG is copied into RAM and given
to a new picture that is the start screen's child and covers all of it; LVGL starts a redraw at the
topmost object that covers the area, so DeepCool's pictures under it are not drawn at all.

After those four seconds the firmware goes to another screen, 2D, and stays there until a host tells it
what to show: on the cooler that is DeepCool's logo once more, as large as the panel, and it is what one
looks at while the PC starts (seen on the cooler with version 5). Version 6 puts the same picture over
that one too, as soon as the screen has been made.

The image may not be longer than 0x72000 bytes (466 944): the bootloader, read from the cooler with
command 21, erases the firmware's whole area up to the end of the chip's flash at 0x80000 and then
writes the update file 4 KiB at a time until there, whatever its size. So patch and logo together have
15 848 bytes.

RAM: 208 bytes at 0x2000F000, above the firmware's stack (which starts at 0x2000B418); the emulated
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
PEEK = 0x21                                 # read memory (version 4)
POKE = 0x22                                 # write a word of memory, and read it back (version 10)
RELEASE = 0x23                              # give the buffers back to the heap (version 15)
LIGHT = 0x24                                # the colours of the 14 lights, from the host (version 16)
LIGHTS = 14
LIGHT_STEP_MS = 16                          # how often the lights move a step on while the host has them
LIGHT_STOCK_MS = 35                         # ... and how often the stock firmware has its loop for them run
LIGHT_TIMEOUT_MS = 3000                     # no command 24 for so long: the cooler picks the colours again
UPLOAD = 0x0F                               # the stock firmware's "an upload is coming"
PEEK_MOST = 39                              # bytes a reply carries
GO, LATER, REFUSED, NO_PAGE = 0x10, 0x11, 0x12, 0x13
VERSION = 16                                # of this patch, in every reply to the command (see CHANGES)
CHANGES = {
    1: "the first: draws a JPEG from USB. Answers 0 where the version is now",
    2: "frames are also shown while lv_task_handler does not return (#22); the reply has the version",
    3: "a part of the picture can be drawn by itself, on top of the page that was drawn last (#47)",
    4: "a command that reads the cooler's memory, to look at its bootloader",
    5: "deephack's own picture while the cooler starts, in place of DeepCool's logo",
    6: "and on the screen after it, where the cooler waits for a host",
    7: "the code that runs all the time lies together (it did not bring back version 2's speed for frames over 26 KB)",
    8: "a frame's bytes are copied by a loop that runs in RAM (which was slower still: never released)",
    9: "the USB controller is told to take the next packet before this one is copied, not after",
    10: "a command that writes a word of the cooler's memory, for trying a setting on the running firmware",
    11: "LVGL's copy of each frame is left out: a redraw takes one turn of the panel, not two",
    12: "and LVGL redraws as soon as there is something to draw: a frame is on the glass at the panel's next turn, 55 a second",
    13: "the firmware's own redraws of a picture that has not changed (ten a second) are left out while ours is on show",
    14: "a packet is copied by the firmware's own memcpy and the frame's sum is checked in the main loop, not byte by byte in the USB interrupt",
    15: "the three buffers go back to the heap when an upload is announced, or when asked: an upload needs 2 MiB of it in one piece",
    16: "the 14 lights on the pump head take their colours from the host and fade to them, sixty steps a second",
}
STATE_SIZE = 208                            # bytes of the patch's RAM (see the layout above SOURCE)
TIMEOUT_MS = 2000

# the stock firmware's own routines and data (real addresses, v2.70)
SYMBOLS = {
    "lv_task_handler": 0x63E90,
    "lv_ll_get_head": 0x38E5C,              # lv_ll_get_head(list): its first entry
    "lv_mem_alloc": 0x5C3C8,
    "lv_mem_free": 0x5C600,
    "lv_img_set_src": 0x58D34,
    "lv_memcpy": 0x396E4,                   # _lv_memcpy(to, from, length)
    "lv_img_create": 0x158D0,               # lv_img_create(parent, copy)
    "lv_obj_set_pos": 0x5F100,              # lv_obj_set_pos(object, x, y), relative to its parent
    "lv_obj_set_hidden": 0x5EF64,           # lv_obj_set_hidden(object, hidden)
    "memcmp": 0x29A60,                      # memcmp(a, b, length): 0 when they are the same
    "area_no_copy": 0x282F0,                # the vendor's: the area that is to be redrawn (an lv_area_t, as an
                                            # object's coords are) is not copied to the other buffer after
    "reply": 0x34D84,                       # reply(command, 0, status): builds and sends the answer
    "usb_send": 0x3488C,                    # usb_send(IN endpoint, data, length)
    "memclr": 0x29D50,                      # memclr(address, length)
    "usb_done": 0x34BAE,                    # the USB handler's "pop {r4, r5, r7, pc}"
    "usb_command": 0x34C5A,                 # ... its check of the packet's second byte
    "light_loop": 0x2F910,                  # the stock loop for the light: sends the 14 colours when they have changed
}
PACKET = 0x200006F0                         # the packet as it came from USB: 64 bytes, cleared before each
LIGHT_COLOURS = 0x200007D0                  # the 14 colours the stock loop sends: a word each, 0x00RRGGBB
L_ON, L_PACE, L_SEEN, L_STEPPED, L_TARGET, L_NOW = 68, 69, 72, 76, 80, 124       # in the patch's state, see below
TICKS = 0x200071DC                          # milliseconds since start
COMMAND_COPY = 0x20000674                   # the command being handled; its payload starts at +3
REPLY = 0x200006A4                          # where the firmware builds its 48 byte answers
SCREEN = 0x20000732                         # u16: the screen on show
READY = 0x200051A8                          # u8: set when the start screen's four seconds are over
START_SCREEN = 0x20000994                   # the start screen (an lv_obj), while it is on
IDLE_SCREEN = 0x20000F18                    # screen 2D, once it has been made: where the firmware waits for a host
DISPLAY = 0x20000418                        # LVGL's display (an lv_disp_t), once it has redrawn for the first time
REDRAW_TASK = 0x30                          # ... in which its redraw task is; a task begins with its period in ms
REDRAW_MS = 5                               # how long LVGL waits now before it redraws what has changed (it was 33)
INVALID_AREAS, INVALID_COUNT = 0x5E, 0x19E  # ... and the areas that wait to be redrawn (lv_area_t each), and how many
LOGO = Path(__file__).with_name("logo.jpg")  # what is shown on it: see logo.py
MOST = 0x72000                              # bytes of image the bootloader writes: from 0xE000 to the flash's end
WIDGET = 0x20001074                         # the image screen's picture (an lv_img)
RXCR1 = 0x40009510                          # USB: endpoint 1 OUT control (4 flush, 8 ready)
IMAGE_SCREEN = 0x90E
HEADER = 1 | 480 << 10 | 640 << 21          # lv_img_header_t: raw, 480 x 640
EXT = 52                                    # lv_obj_t: where its ext_attr is (an lv_img's begins with its src)

# (address, the stock bytes there, what replaces them)
HOOKS = [
    (0x109A4, "0121 0170", "bl init_hook"),             # movs r1, #1 ; strb r1, [r0]
    (0x109A8, None, "bl main_hook"),                    # bl lv_task_handler
    (0x64088, None, "bl loop_hook"),                    # bl lv_ll_get_head, where lv_task_handler starts over
    (0x2F158, None, "bl command_hook"),                 # bl reply
    (0x34C54, "2078 aa28", "b.w usb_hook"),             # ldrb r0, [r4] ; cmp r0, #0xaa
    (0x72ED0, None, "b.w light_hook"),                  # b.w light_loop: all there is to the light's task
]
STOCK_CALLS = {0x109A8: "lv_task_handler", 0x64088: "lv_ll_get_head", 0x2F158: "reply", 0x72ED0: "light_loop"}
BRANCHES = {"b", "bl", "cbz", "cbnz"} | {"b" + c for c in "eq ne hs lo mi pl vs vc hi ls ge lt gt le".split()}

# state: +0 u8 phase (0 idle, 1 receiving, 2 ready to show)   +1 u8 buffers wanted
#        +2 u8 page: which buffer the picture widget shows (1 to 3; 0: none of ours yet)
#        +3 u8 part: which buffer the small picture on top of it shows (0: none, it is hidden)
#        +4, +8, +12 the three buffers   +16 size   +20 received   +24 (not used since 14)   +28 expected sum
#        +32 tick at start   +36 frames shown   +40 u8 the buffer being filled   +41 u8 1: it is a part
#        +44 u16 x, +46 u16 y of the part   +48 its lv_img_header_t (which has its size)
#        +52, +56 the same two words of the part before it: where the small picture is now
#        +60 u8 our picture is on the start screen (1) and on the idle screen (2), or was tried; the
#            redraw task has its new period (4)
#        +61 u8 whole frames are flowing: what is on the glass was drawn without LVGL's copy after it
#        +62 u8 a picture of ours waits to be redrawn   +63 u8 ... and it is a new page under a part, which
#            is to be copied into the other buffer this once
#        +64 that picture: its lv_img_dsc_t, in RAM
#        +68 u8 the lights show the host's colours   +69 u8 pace: 256ths of what is left, every step
#        +72 tick of the last command 24   +76 tick of the last step
#        +80 where the lights go to: 14 x R G B   +124 where they are: 42 x u16, 8.8 fixed point
#
# The copy (version 11). LVGL draws into the frame buffer that is not on the glass, has the panel take
# it at its next turn, and then copies what it drew into the other buffer, so that both hold the same
# picture: 614 KB, which takes this chip 20 ms. The panel turns every 18: so a redraw cost two turns,
# 36.1 ms, and that was the 27.7 pictures a second nothing got past. A picture that is drawn anew from
# end to end every time needs no such copy, and the vendor's firmware has a routine that says so for an
# area (its own video player calls it after every frame; nothing on this cooler uses that player). The
# patch calls it after it has given a frame to the picture, and after the firmware's own redraws of that
# frame (its screen timer asks for one ten times a second). The two buffers then hold different frames,
# which shows nowhere as long as every redraw is the whole picture. Where it would show, the copy is
# made again: a part of the picture is put on a page only after the page has been drawn whole once
# more, with its copy; and a picture the stock firmware shows is none of ours.
#
# That alone did not show more pictures: LVGL's redraw task ran every 33 ms at the most, which is every
# second turn of the panel again. Its period is 5 ms now (version 12). It only runs when something is to
# be redrawn, so nothing is drawn more often for it, only sooner.
#
# And the redraws nobody needs (version 13). The image screen's timer asks for its picture to be redrawn
# every 100 ms, changed or not. Each of those is a turn of the panel, or two with the copy, in which the
# main loop is not there to show a new frame: with a page and a part on show a video got 21 pictures a
# second for it where the cooler could show 30. While the picture on show is ours and nothing of ours
# waits to be drawn, such a request is taken back (after_task).
#
# How fast a frame's bytes arrive is decided between two USB packets (version 9). The host leaves some 25
# microseconds after a packet before it sends the next, and a packet that finds the endpoint not ready is
# refused and sent again whole. Until version 8 the endpoint was made ready after the packet's 64 bytes
# had been copied, which fitted into that gap or did not, by how fast the copy loop happened to run where
# it lay: the chip runs its code out of the flash through a small cache (FMC.CACHE). Seen on the cooler,
# flashing one version after the other (KB a second for frames of 26 and of 40 KB): version 2, 823 and
# 800; versions 3 to 6, 765 and 540; 7, with the routines put in another order, 760 and 595; 8, with the
# loop in RAM, 390 and 430. So the endpoint is made ready first now. The order from version 7 stays: what
# runs all the time first, together and short; then what runs once a frame; then the rest.
#
# That was not all of it. With the endpoint ready first the handler still has to be done before the next
# packet is, 64 microseconds later, and with a loop that copied and summed a byte at a time it was or was
# not, again by where the loop lay: 997 KB a second with versions 9 to 12, 873 with 13, in which not one
# line of the handler had changed. Since version 14 the handler copies with the firmware's own memcpy and
# does nothing else, and the frame's sum is taken by the main loop before it shows the frame. That did
# not bring the 997 back: 874 with 14 and with 15, for every size from 26 to 90 KB, to the kilobyte. Seven
# packets where eight would go: what takes the eighth is not known.
#
# Room for an upload (version 15). The stock firmware puts an upload, a picture or a firmware, into
# 2 MiB of LVGL's heap in one piece (malloc(0x200000) at 0x2E232), which it asks for after its "Loading"
# screen has taken some 2.9 MB of the 8. When there is no such piece it prints a line to its serial port
# and drops the upload: every packet of the file is thrown away, and nothing says so to the host. On the
# cooler, with the three buffers below held and a stored picture of 60 KB on show, the largest piece
# left was 2012 KB: a firmware update was sent whole and the cooler went on as before. With our own small
# still on show it was 2067 KB, which is why most updates did go through. So the buffers are given back:
# when the host asks (command 23, before it announces an upload) and when an upload is announced
# (command 0F, for programs that do not ask). The next frame makes them again. 2396 KB then.
def source(logo_at: int, logo_size: int) -> str:
    """The patch's code, for a logo of `logo_size` bytes at address `logo_at`."""
    return f"""
main_hook:                              @ in main's loop, where it called lv_task_handler
    push    {{r4, lr}}
    bl      look
    pop.w   {{r4, lr}}
    b.w     lv_task_handler

loop_hook:                              @ r0 = the task list, for the call this one replaced: a task has just run
    push    {{r4, lr}}
    mov     r4, r0
    bl      look_after_task
    mov     r0, r4
    pop.w   {{r4, lr}}
    b.w     lv_ll_get_head

look_after_task:                        @ a task has run: the stock firmware may have changed the picture, or asked
    movw    r3, #{STATE & 0xFFFF}       @ for a redraw (it does both in tasks, so this is not asked on every pass)
    movt    r3, #{STATE >> 16}
    ldrb    r0, [r3, #3]
    ldrb.w  r1, [r3, #61]
    orrs    r0, r1
    beq     look_plain                  @ no part on show and no frames flowing: nothing of ours to look after
    b.w     after_task
look_plain:
    b.w     look

look:                                   @ anything for the main loop to do? Hardly ever, and this is passed
    movw    r3, #{STATE & 0xFFFF}       @ thousands of times a second: so it is short, and lies next to the
    movt    r3, #{STATE >> 16}          @ other code that runs all the time (see the note above SOURCE)
    ldrb.w  r1, [r3, #60]
    ldrh    r0, [r3, #0]                @ the phase, and "buffers wanted" above it
    cmp     r1, #7
    bne     look_work                   @ what is done once after the start is not all done yet
    cmp     r0, #2
    bhs     look_work                   @ a frame is all in (2), or buffers are wanted (0x100 and up)
    bx      lr
look_work:
    b.w     show

usb_hook:                               @ r4 = the packet, r1 = where its length is; r4, r5, r7, lr are saved
    movw    r2, #{STATE & 0xFFFF}
    movt    r2, #{STATE >> 16}
    ldrb    r3, [r2, #0]
    cmp     r3, #1
    bne     usb_stock
    movw    r3, #{TICKS & 0xFFFF}
    movt    r3, #{TICKS >> 16}
    ldr     r3, [r3]
    ldr     r5, [r2, #32]
    subs    r3, r3, r5
    cmp.w   r3, #{TIMEOUT_MS}
    bhi     usb_expired
    movw    r3, #{RXCR1 & 0xFFFF}       @ The endpoint may take the next packet now, before this one is copied:
    movt    r3, #{RXCR1 >> 16}          @ the stock handler has it out of the controller already (in RAM, at r4).
    movs    r5, #4                      @ The host leaves some 25 microseconds between two packets, and a packet
    str     r5, [r3]                    @ that finds the endpoint shut is sent again, all 64 bytes of it (see the
    movs    r5, #8                      @ note above SOURCE).
    str     r5, [r3]
    mov     r7, r2                      @ the state, in a register the copy leaves alone
    ldrb    r2, [r1]                    @ bytes in this packet
    ldr     r3, [r7, #16]
    ldr     r5, [r7, #20]
    subs    r3, r3, r5
    cmp     r2, r3
    it      hi
    movhi   r2, r3                      @ no more than the frame still needs
    ldrb.w  r0, [r7, #40]
    ldr.w   r0, [r7, r0, lsl #2]        @ the buffer being filled: one that neither picture shows
    adds    r0, #16
    add     r0, r5
    add     r5, r2
    str     r5, [r7, #20]
    mov     r1, r4
    cmp     r2, #0
    beq     usb_copied
    bl      lv_memcpy                   @ the firmware's own, a word at a time. (The sum is the main loop's work.)
usb_copied:
    ldr     r0, [r7, #20]
    ldr     r3, [r7, #16]
    cmp     r0, r3
    bne     usb_more
    movs    r3, #2                      @ all of it is in
    strb    r3, [r7, #0]
usb_more:
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

command_hook:                           @ r0 = command, r1 = 0, r2 = 0; runs in the USB interrupt
    cmp     r0, #{COMMAND}
    beq     command_ours
    cmp     r0, #{PEEK}
    beq     command_peek
    cmp     r0, #{POKE}
    beq     command_poke
    cmp     r0, #{RELEASE}
    beq     command_release
    cmp     r0, #{LIGHT}
    beq     command_light
    cmp     r0, #{UPLOAD}
    bne     command_stock
    movw    r3, #{STATE & 0xFFFF}       @ An upload is coming, a picture or a firmware: the stock firmware puts
    movt    r3, #{STATE >> 16}          @ it into 2 MiB of the heap, in one piece, and gives up without a word
    movs    r2, #2                      @ when there is no such piece (see the note above SOURCE). The main loop
    strb    r2, [r3, #1]                @ gives our buffers back, at its next pass.
    movs    r2, #0
command_stock:
    b.w     reply
command_peek:
    b.w     peek
command_poke:
    b.w     poke
command_release:
    b.w     release_asked
command_light:                          @ (a short jump to a short jump: Keystone gets what follows a long
    b       light_asked                 @ conditional one wrong by two bytes)
command_ours:
    push    {{r4, r5, r6, lr}}
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    movw    r3, #{COMMAND_COPY & 0xFFFF}
    movt    r3, #{COMMAND_COPY >> 16}
    ldr.w   r1, [r3, #3]                @ size
    movs    r2, #{REFUSED}
    cmp     r1, #0
    beq     command_out
    cmp.w   r1, #{MAX_FRAME}
    bhi     command_out
    movs    r2, #{LATER}
    ldr     r0, [r4, #4]
    cmp     r0, #0
    beq     command_want
    ldr     r0, [r4, #8]
    cmp     r0, #0
    beq     command_want
    ldr     r0, [r4, #12]
    cmp     r0, #0
    beq     command_want
    ldrb    r0, [r4, #0]
    cmp     r0, #2
    beq     command_out
    ldrh.w  r5, [r3, #13]               @ width of a part; 0: the whole page
    movs    r6, #0
    cmp     r5, #0
    beq     command_take
    movs    r2, #{NO_PAGE}              @ a part goes on a page of ours, and only while that is on show
    ldrb    r0, [r4, #2]
    cmp     r0, #0
    beq     command_out
    ldr.w   r0, [r4, r0, lsl #2]
    movw    r6, #{WIDGET & 0xFFFF}
    movt    r6, #{WIDGET >> 16}
    ldr     r6, [r6]
    ldr     r6, [r6, #{EXT}]
    ldr     r6, [r6, #0]
    cmp     r0, r6
    bne     command_out
    ldrh.w  r0, [r3, #15]               @ height
    lsls    r6, r0, #21
    orr.w   r6, r6, r5, lsl #10
    adds    r6, #1                      @ lv_img_header_t: raw, width, height
    str     r6, [r4, #48]
    ldrh.w  r0, [r3, #9]
    strh    r0, [r4, #44]               @ x
    ldrh.w  r0, [r3, #11]
    strh    r0, [r4, #46]               @ y
    movs    r6, #1
command_take:
    strb.w  r6, [r4, #41]
    str     r1, [r4, #16]
    ldrh.w  r0, [r3, #7]                @ sum16
    str     r0, [r4, #28]
    movs    r0, #0
    str     r0, [r4, #20]
    str     r0, [r4, #24]
    ldrb    r1, [r4, #2]
    ldrb    r2, [r4, #3]
    movs    r0, #1                      @ into the first buffer that neither picture shows
command_pick:
    cmp     r0, r1
    beq     command_skip
    cmp     r0, r2
    bne     command_picked
command_skip:
    adds    r0, #1
    b       command_pick
command_picked:
    strb.w  r0, [r4, #40]
    movw    r0, #{TICKS & 0xFFFF}
    movt    r0, #{TICKS >> 16}
    ldr     r0, [r0]
    str     r0, [r4, #32]
    movs    r0, #1
    strb    r0, [r4, #0]
    movs    r2, #{GO}
    b       command_out
release_asked:                          @ command 23: GO when no buffer is held, LATER when the main loop is to
    push    {{r4, r5, r6, lr}}            @ give them back (ask again). Answered as command 20 is.
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    movs    r2, #{GO}
    ldr     r0, [r4, #4]
    ldr     r1, [r4, #8]
    orrs    r0, r1
    ldr     r1, [r4, #12]
    orrs    r0, r1
    beq     command_out
    movs    r0, #2
    strb    r0, [r4, #1]
    movs    r2, #{LATER}
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
    movs    r0, #{VERSION}
    strb    r0, [r4, #4]
    movw    r0, #0x4948
    movt    r0, #0x4344
    str.w   r0, [r4, #42]               @ "HIDC"
    addw    r0, r5, #{0x55 + 0x2E + COMMAND + VERSION + sum(b"HIDC")}
    strh    r0, [r4, #46]               @ sum16
    movs    r0, #1                      @ endpoint 1 IN
    mov     r1, r4
    movs    r2, #48
    pop.w   {{r4, r5, r6, lr}}
    b.w     usb_send
light_asked:                            @ command 24: where the 14 lights are to go. Answered as command 20 is.
                                        @ (After command_out, not with the other commands before it: there it
                                        @ made two of their short jumps to command_out long ones, and Keystone
                                        @ gets the calls that follow a jump it had to lengthen wrong.)
    push    {{r4, r5, r6, lr}}
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    movw    r5, #{PACKET & 0xFFFF}      @ the packet as it came: the dispatcher's copy ends after 48 bytes, and
    movt    r5, #{PACKET >> 16}         @ this one has 16 more (zeros, when the host sent 48: "pick them yourself")
    add.w   r0, r4, #{L_TARGET}
    adds    r1, r5, #3
    movs    r2, #39
    bl      lv_memcpy                   @ 13 lights from the command's payload
    ldrh.w  r0, [r5, #48]
    strh.w  r0, [r4, #{L_TARGET + 39}]
    ldrb.w  r0, [r5, #50]
    strb.w  r0, [r4, #{L_TARGET + 41}]  @ and the 14th from after it
    ldrh.w  r0, [r5, #51]
    strh.w  r0, [r4, #{L_ON}]           @ on, and the pace above it
    movw    r0, #{TICKS & 0xFFFF}
    movt    r0, #{TICKS >> 16}
    ldr     r0, [r0]
    str     r0, [r4, #{L_SEEN}]
    movs    r2, #{GO}
    b       command_out

peek:                                   @ command 21: up to 39 bytes of memory, in a reply of the usual form
    push    {{r4, r5, r6, lr}}
    movw    r4, #{REPLY & 0xFFFF}
    movt    r4, #{REPLY >> 16}
    mov     r0, r4
    movs    r1, #48
    bl      memclr
    movw    r0, #0x2e55
    strh    r0, [r4, #0]
    movw    r3, #{COMMAND_COPY & 0xFFFF}
    movt    r3, #{COMMAND_COPY >> 16}
    ldrb    r0, [r3, #2]
    strb    r0, [r4, #2]                @ the command that was asked: 21, or 22 after its write
    ldr.w   r1, [r3, #3]                @ address
    ldrb    r2, [r3, #7]                @ count
    cmp     r2, #{PEEK_MOST}
    it      hi
    movhi   r2, #{PEEK_MOST}
    adds    r3, r4, #3
    cmp     r2, #0
    beq     peek_tail
peek_copy:
    ldrb    r0, [r1], #1
    strb    r0, [r3], #1
    subs    r2, #1
    bne     peek_copy
peek_tail:
    movw    r0, #0x4948
    movt    r0, #0x4344
    str.w   r0, [r4, #42]               @ "HIDC"
    movs    r0, #0
    movs    r1, #0
peek_sum:
    ldrb    r2, [r4, r1]
    add     r0, r2
    adds    r1, #1
    cmp     r1, #46
    bne     peek_sum
    strh    r0, [r4, #46]               @ sum16
    movs    r0, #1                      @ endpoint 1 IN
    mov     r1, r4
    movs    r2, #48
    pop.w   {{r4, r5, r6, lr}}
    b.w     usb_send

poke:                                   @ command 22: a word written, then answered as 21 is
    movw    r3, #{COMMAND_COPY & 0xFFFF}
    movt    r3, #{COMMAND_COPY >> 16}
    ldr.w   r1, [r3, #3]                @ address
    ldr.w   r2, [r3, #8]                @ value
    str     r2, [r1]
    b.w     peek

show:                                   @ make the buffers when asked to, and show a frame that is all in
    push    {{r3, r4, r5, r6, r7, lr}}
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    ldrb.w  r0, [r4, #60]
    cmp     r0, #7
    beq     show_started
    bl      logo
show_started:
    ldrb    r0, [r4, #1]
    cmp     r0, #0
    beq     show_frame
    cmp     r0, #1
    beq     show_make
    bl      release
    pop     {{r3, r4, r5, r6, r7, pc}}
show_make:
    movs    r5, #4
show_alloc:
    ldr     r0, [r4, r5]
    cmp     r0, #0
    bne     show_next
    movw    r0, #{(MAX_FRAME + 16) & 0xFFFF}
    movt    r0, #{(MAX_FRAME + 16) >> 16}
    bl      lv_mem_alloc
    cmp     r0, #0
    beq     show_next
    movs    r1, #0                      @ an lv_img_dsc_t in front of the data, as the stock loader makes it
    str     r1, [r0, #0]                @ (its header is written when a frame is shown: page or part)
    str     r1, [r0, #4]
    str     r1, [r0, #12]
    add.w   r1, r0, #16
    str     r1, [r0, #8]
    str     r0, [r4, r5]
show_next:
    adds    r5, #4
    cmp     r5, #16
    bne     show_alloc
    movs    r0, #0
    strb    r0, [r4, #1]                @ one try per request
show_frame:
    movw    r0, #{SCREEN & 0xFFFF}
    movt    r0, #{SCREEN >> 16}
    ldrh    r0, [r0]
    movw    r1, #{IMAGE_SCREEN}
    cmp     r0, r1
    beq     show_here
    ldrb    r0, [r4, #0]                @ another screen: a frame that came for this one is dropped
    cmp     r0, #2
    bne     show_away
    movs    r0, #0
    strb    r0, [r4, #0]
show_away:
    pop     {{r3, r4, r5, r6, r7, pc}}
show_here:
    movw    r6, #{WIDGET & 0xFFFF}
    movt    r6, #{WIDGET >> 16}
    ldr     r6, [r6]                    @ the picture widget
    ldrb    r0, [r4, #0]
    cmp     r0, #2
    bne     show_guard
    ldrb.w  r5, [r4, #40]
    ldr.w   r5, [r4, r5, lsl #2]        @ the buffer that was filled
    add.w   r0, r5, #16                 @ Is it what was sent? Its bytes are summed here, where there is time: in
    ldr     r1, [r4, #16]               @ the USB interrupt that cost as much as the copy, sixty-four times a
    movs    r2, #0                      @ packet, with the next packet on its way.
show_sum:
    ldrb    r3, [r0], #1
    add     r2, r3
    subs    r1, #1
    bne     show_sum
    uxth    r2, r2
    ldr     r0, [r4, #28]
    cmp     r0, r2
    beq     show_whole
    movs    r0, #0                      @ a frame with the wrong sum is not shown
    strb    r0, [r4, #0]
    pop     {{r3, r4, r5, r6, r7, pc}}
show_whole:
    ldr     r0, [r4, #16]
    str     r0, [r5, #4]                @ data_size
    ldrb.w  r0, [r4, #41]
    cmp     r0, #0
    bne     show_part
    movw    r0, #{HEADER & 0xFFFF}
    movt    r0, #{HEADER >> 16}
    str     r0, [r5, #0]
    ldrb.w  r0, [r4, #40]
    strb    r0, [r4, #2]                @ the page is this buffer now
    bl      hide_part
    mov     r0, r6
    mov     r1, r5
    bl      lv_img_set_src
    add.w   r0, r6, #16                 @ its place on the screen: all of that is drawn anew, no copy after
    bl      area_no_copy
    movs    r0, #1
    strb.w  r0, [r4, #61]
    strh.w  r0, [r4, #62]               @ ours waits to be drawn (and it is no page for a part)
    b       show_count
show_part:
    ldrb.w  r0, [r4, #61]
    cmp     r0, #0
    beq     show_part_page
    movs    r0, #0                      @ The page under it was drawn without the copy: the other buffer has
    strb.w  r0, [r4, #61]               @ an older picture. So the page is drawn whole once more with this part
    str     r0, [r4, #56]               @ (as if the part had moved: see show_moved), and copied.
show_part_page:
    ldr     r0, [r4, #48]
    str     r0, [r5, #0]                @ raw, as wide and high as the command said
    adds    r0, r6, #4
    bl      lv_ll_get_head
    cmp     r0, #0
    bne     show_have
    str     r0, [r4, #56]               @ (0: no part has this header, so "it was somewhere else" below)
    mov     r0, r6
    movs    r1, #0
    bl      lv_img_create               @ the first part: a picture of its own, the widget's child
    cmp     r0, #0
    beq     show_drop
show_have:
    mov     r7, r0
    ldrh    r1, [r4, #44]
    ldrh    r2, [r4, #46]
    bl      lv_obj_set_pos
    mov     r0, r7
    mov     r1, r5
    bl      lv_img_set_src
    mov     r0, r7
    movs    r1, #0
    bl      lv_obj_set_hidden
    add.w   r0, r7, #16                 @ the part's place is drawn anew whole every time too
    bl      area_no_copy
    movs    r0, #1
    strb.w  r0, [r4, #62]               @ ours waits to be drawn
    ldrb.w  r0, [r4, #40]
    strb    r0, [r4, #3]
    ldr     r0, [r4, #44]
    ldr     r1, [r4, #48]
    ldr     r2, [r4, #52]
    ldr     r3, [r4, #56]
    cmp     r0, r2
    bne     show_moved
    cmp     r1, r3
    beq     show_count
show_moved:                             @ the small picture is new, or was somewhere else or another size: the
    str     r0, [r4, #52]               @ page is drawn whole again with it. (LVGL would redraw the bit of the
    str     r1, [r4, #56]               @ page that came free, and a bit of a picture is not decoded into the
    ldrb    r1, [r4, #2]                @ frame buffer but into RAM first, kept there by address: a path the
    ldr.w   r1, [r4, r1, lsl #2]        @ stock firmware takes for its slides, and this patch never.)
    mov     r0, r6
    bl      lv_img_set_src
    movs    r0, #1
    strb.w  r0, [r4, #63]               @ ... and copied, this once: the page into both buffers
show_count:
    ldr     r0, [r4, #36]
    adds    r0, #1
    str     r0, [r4, #36]
show_drop:
    movs    r0, #0
    strb    r0, [r4, #0]                @ only now may the next frame start
    pop     {{r3, r4, r5, r6, r7, pc}}
show_guard:                             @ nothing to show. Is the page still ours? The stock firmware may have
    ldrb    r0, [r4, #3]                @ shown a stored picture: the part must not stay on top of that
    cmp     r0, #0
    beq     show_done
    ldrb    r0, [r4, #2]
    ldr.w   r0, [r4, r0, lsl #2]
    ldr     r1, [r6, #{EXT}]
    ldr     r1, [r1, #0]                @ what the picture widget shows
    cmp     r0, r1
    beq     show_done
    bl      hide_part
show_done:
    pop     {{r3, r4, r5, r6, r7, pc}}

after_task:                             @ a task has run while a picture of ours may be on show (entered with a jump
    push    {{r4, r5, r6, lr}}            @ from look_after_task: its return is the hook's)
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    movw    r0, #{SCREEN & 0xFFFF}
    movt    r0, #{SCREEN >> 16}
    ldrh    r0, [r0]
    movw    r1, #{IMAGE_SCREEN}
    cmp     r0, r1
    bne     after_not_ours
    movw    r6, #{WIDGET & 0xFFFF}
    movt    r6, #{WIDGET >> 16}
    ldr     r6, [r6]
    ldrb    r0, [r4, #2]
    ldr.w   r0, [r4, r0, lsl #2]
    ldr     r1, [r6, #{EXT}]
    ldr     r1, [r1, #0]
    cmp     r0, r1
    beq     after_ours
    bl      hide_part                   @ the picture shows something else now: a part must not stay on top of it
after_not_ours:                         @ ... and what is redrawn and copied is the stock firmware's business again
    movs    r0, #0
    strb.w  r0, [r4, #61]
    b       after_out
after_ours:
    movw    r5, #{DISPLAY & 0xFFFF}
    movt    r5, #{DISPLAY >> 16}
    ldr     r5, [r5]
    cmp     r5, #0
    beq     after_out
    ldrh.w  r0, [r5, #{INVALID_COUNT}]
    lsls    r0, r0, #22                 @ its low ten bits: how many areas wait to be redrawn
    bne     after_waiting
    strh.w  r0, [r4, #62]               @ none: what we asked for has been drawn (and a page is in both buffers)
    b       after_out
after_waiting:
    ldrb.w  r1, [r4, #62]
    cmp     r1, #0
    bne     after_ours_too
    cmp.w   r0, #{1 << 22}              @ Nothing of ours is waiting, so the firmware asked by itself: its screen
    bne     after_out                   @ timer does, ten times a second, for the picture that is on show already.
    add.w   r0, r5, #{INVALID_AREAS}    @ If that is all there is, it is not drawn again: a redraw is a turn of the
    add.w   r1, r6, #16                 @ panel in which no new frame can be shown.
    movs    r2, #8
    bl      memcmp
    cmp     r0, #0
    bne     after_out
    ldrh.w  r0, [r5, #{INVALID_COUNT}]
    bic     r0, r0, #0xff
    bic     r0, r0, #0x300
    strh.w  r0, [r5, #{INVALID_COUNT}]
    b       after_out
after_ours_too:                         @ Ours is waiting, and the firmware's request may have been joined with it
    ldrb.w  r0, [r4, #63]               @ into the whole picture: that needs no copy either, unless this is the
    cmp     r0, #0                      @ redraw that brings a new page into both buffers.
    bne     after_out
    add.w   r0, r6, #16
    bl      area_no_copy
after_out:
    pop.w   {{r4, r5, r6, lr}}
    b.w     look

release:                                @ r4 = state: the three buffers go back to the heap
    push    {{r3, r5, r6, r7, lr}}
    sub     sp, #12
    cpsid   i                           @ (no frame may start between "none is coming in" and "there are none")
    movs    r0, #0
    strh    r0, [r4, #0]                @ no frame is coming in, and what was asked is done
    ldr     r5, [r4, #4]
    ldr     r7, [r4, #8]
    ldr     r1, [r4, #12]
    str     r1, [sp, #0]
    str     r0, [r4, #4]
    str     r0, [r4, #8]
    str     r0, [r4, #12]
    cpsie   i
    movw    r0, #{SCREEN & 0xFFFF}
    movt    r0, #{SCREEN >> 16}
    ldrh    r0, [r0]
    movw    r1, #{IMAGE_SCREEN}
    cmp     r0, r1
    bne     release_free                @ another screen: no picture of ours is on show
    movw    r6, #{WIDGET & 0xFFFF}
    movt    r6, #{WIDGET >> 16}
    ldr     r6, [r6]
    bl      hide_part
    ldr     r0, [r6, #{EXT}]
    ldr     r0, [r0, #0]                @ what the picture widget shows
    cmp     r0, #0
    beq     release_free
    cmp     r0, r5
    beq     release_shown
    cmp     r0, r7
    beq     release_shown
    ldr     r1, [sp, #0]
    cmp     r0, r1
    bne     release_free
release_shown:                          @ It shows one of them: our own picture takes its place, the one from the
    ldr     r1, [r4, #64]               @ start screen, which is not in these buffers.
    cmp     r1, #0
    bne     release_logo
    str     r5, [r4, #4]                @ (there is none: then the buffers stay, they cannot be taken from under
    str     r7, [r4, #8]                @ the picture)
    ldr     r0, [sp, #0]
    str     r0, [r4, #12]
    b       release_out
release_logo:
    mov     r0, r6
    bl      lv_img_set_src
release_free:
    movs    r0, #0
    strh    r0, [r4, #2]                @ no page of ours, no part
    str     r0, [r4, #52]
    str     r0, [r4, #56]
    strb.w  r0, [r4, #61]
    strh.w  r0, [r4, #62]
    mov     r0, r5
    bl      lv_mem_free                 @ (which takes a null as well)
    mov     r0, r7
    bl      lv_mem_free
    ldr     r0, [sp, #0]
    bl      lv_mem_free
release_out:
    add     sp, #12
    pop     {{r3, r5, r6, r7, pc}}

hide_part:                              @ r4 = state, r6 = the picture widget: the part goes, the page is whole
    push    {{r5, lr}}
    ldrb    r0, [r4, #3]
    cmp     r0, #0
    beq     hide_done
    movs    r0, #0
    strb    r0, [r4, #3]
    adds    r0, r6, #4
    bl      lv_ll_get_head              @ the widget's child, if it has one
    cmp     r0, #0
    beq     hide_done
    movs    r1, #1
    bl      lv_obj_set_hidden
hide_done:
    pop     {{r5, pc}}

logo:                                   @ r4 = state. What is done once after the start: the redraw task's period,
    push    {{r4, r5, r6, lr}}            @ and our picture over the start screen's and the idle screen's
    ldrb.w  r6, [r4, #60]
    tst     r6, #4
    bne     logo_pictures
    movw    r0, #{DISPLAY & 0xFFFF}
    movt    r0, #{DISPLAY >> 16}
    ldr     r0, [r0]
    cmp     r0, #0
    beq     logo_pictures               @ LVGL has not redrawn yet: next time
    ldr     r0, [r0, #{REDRAW_TASK}]
    cmp     r0, #0
    beq     logo_pictures
    movs    r1, #{REDRAW_MS}
    str     r1, [r0, #0]
    orr     r6, r6, #4
    strb.w  r6, [r4, #60]
logo_pictures:
    and     r0, r6, #3
    cmp     r0, #3
    beq     logo_out
    ldr     r5, [r4, #64]
    cmp     r5, #0
    bne     logo_start
    movw    r0, #{(logo_size + 16) & 0xFFFF}
    movt    r0, #{(logo_size + 16) >> 16}
    bl      lv_mem_alloc
    cmp     r0, #0
    bne     logo_made
    ldrb.w  r0, [r4, #60]
    orr     r0, r0, #3
    strb.w  r0, [r4, #60]               @ no memory: DeepCool's pictures stay
    b       logo_out
logo_made:
    mov     r5, r0
    str     r5, [r4, #64]
    movw    r0, #{HEADER & 0xFFFF}
    movt    r0, #{HEADER >> 16}
    str     r0, [r5, #0]                @ an lv_img_dsc_t as the stock loader makes it: raw, 480 x 640
    movw    r3, #{logo_size & 0xFFFF}
    movt    r3, #{logo_size >> 16}
    str     r3, [r5, #4]
    add.w   r1, r5, #16
    str     r1, [r5, #8]
    movs    r0, #0
    str     r0, [r5, #12]
    movw    r2, #{logo_at & 0xFFFF}
    movt    r2, #{logo_at >> 16}
logo_copy:                              @ the JPEG, from the flash into RAM: where the decoder is known to read
    ldrb    r0, [r2], #1
    strb    r0, [r1], #1
    subs    r3, #1
    bne     logo_copy
logo_start:
    ldrb.w  r6, [r4, #60]
    tst     r6, #1
    bne     logo_idle
    orr     r6, r6, #1
    strb.w  r6, [r4, #60]               @ the start screen: when this is first called, and never again
    movw    r0, #{READY & 0xFFFF}
    movt    r0, #{READY >> 16}
    ldrb    r0, [r0]
    cmp     r0, #0
    bne     logo_idle                   @ its four seconds are over already (not when this is first called)
    movw    r0, #{START_SCREEN & 0xFFFF}
    movt    r0, #{START_SCREEN >> 16}
    ldr     r0, [r0]
    bl      logo_on
logo_idle:
    ldrb.w  r6, [r4, #60]
    tst     r6, #2
    bne     logo_out
    movw    r0, #{IDLE_SCREEN & 0xFFFF}
    movt    r0, #{IDLE_SCREEN >> 16}
    ldr     r0, [r0]
    cmp     r0, #0
    beq     logo_out                    @ the idle screen has not been made yet: look again next time
    ldr     r1, [r0, #8]
    cmp     r1, #0
    beq     logo_out                    @ ... or has no pictures yet
    orr     r6, r6, #2
    strb.w  r6, [r4, #60]
    bl      logo_on
logo_out:
    pop     {{r4, r5, r6, pc}}

logo_on:                                @ r0 = a screen (or 0), r5 = our picture: put it on top of that screen
    push    {{r4, lr}}
    cmp     r0, #0
    beq     logo_on_out
    movs    r1, #0
    bl      lv_img_create               @ a child of the screen, made last: on top of what is there
    cmp     r0, #0
    beq     logo_on_out
    mov     r1, r5
    bl      lv_img_set_src
logo_on_out:
    pop     {{r4, pc}}

light_hook:                             @ r0 = the light's task (an lv_task_t: its period is its first word)
    push.w  {{r3, r4, r5, r6, r7, r8, r9, lr}}
    mov     r9, r0
    movw    r4, #{STATE & 0xFFFF}
    movt    r4, #{STATE >> 16}
    movw    r1, #{TICKS & 0xFFFF}
    movt    r1, #{TICKS >> 16}
    ldr     r1, [r1]
    ldrb.w  r0, [r4, #{L_ON}]
    cmp     r0, #0
    beq     light_theirs
    ldr     r2, [r4, #{L_SEEN}]
    subs    r2, r1, r2
    movw    r3, #{LIGHT_TIMEOUT_MS}
    cmp     r2, r3
    bls     light_ours
    movs    r0, #0                      @ nothing from the host for three seconds: it has gone
    strb.w  r0, [r4, #{L_ON}]
light_theirs:                           @ The cooler picks. What it shows is kept as "where the lights are", so
    movs    r0, #{LIGHT_STOCK_MS}       @ that the host's colours, when they come, begin from there.
    str.w   r0, [r9, #0]
    movw    r5, #{LIGHT_COLOURS & 0xFFFF}
    movt    r5, #{LIGHT_COLOURS >> 16}
    add.w   r6, r4, #{L_NOW}
    movs    r2, #{LIGHTS}
light_follow:
    ldr     r0, [r5], #4
    lsrs    r1, r0, #16
    uxtb    r1, r1
    lsls    r1, r1, #8
    strh    r1, [r6], #2
    lsrs    r1, r0, #8
    uxtb    r1, r1
    lsls    r1, r1, #8
    strh    r1, [r6], #2
    uxtb    r1, r0
    lsls    r1, r1, #8
    strh    r1, [r6], #2
    subs    r2, #1
    bne     light_follow
    b       light_stock
light_ours:
    movs    r0, #{LIGHT_STEP_MS}
    str.w   r0, [r9, #0]                @ the task comes round this often now
    ldr     r2, [r4, #{L_STEPPED}]
    subs    r2, r1, r2
    lsrs    r2, r2, #4                  @ steps of 16 ms since the last (a redraw can keep the task waiting)
    beq     light_put
    str     r1, [r4, #{L_STEPPED}]
    cmp     r2, #8
    it      hi
    movhi   r2, #8
    mov     r12, r2
    ldrb.w  r3, [r4, #{L_PACE}]
    add.w   r5, r4, #{L_TARGET}
    add.w   r6, r4, #{L_NOW}
    mov.w   r8, #{LIGHTS * 3}
light_channel:
    ldrb    r0, [r5], #1
    lsls    r0, r0, #8                  @ where this one goes
    ldrh    r1, [r6]                    @ where it is
    mov     r2, r12
light_step:
    subs    r7, r0, r1                  @ what is left
    muls    r7, r3, r7
    asrs    r7, r7, #8                  @ so much of it. Rounded down: never more than is left, and never nothing
    it      eq                          @ while it is above, so it gets there. From below, and with pace 0, the
    subeq   r7, r0, r1                  @ last bit is one jump.
    add     r1, r7
    subs    r2, #1
    bne     light_step
    strh    r1, [r6], #2
    subs.w  r8, r8, #1
    bne     light_channel
light_put:                              @ Every time, steps or none: the stock picking writes there ten times a
    movw    r5, #{LIGHT_COLOURS & 0xFFFF}   @ second, and the stock loop must find ours.
    movt    r5, #{LIGHT_COLOURS >> 16}
    add.w   r6, r4, #{L_NOW}
    movs    r2, #{LIGHTS}
light_word:
    ldrh    r0, [r6], #2
    lsrs    r0, r0, #8
    lsls    r0, r0, #16
    ldrh    r1, [r6], #2
    lsrs    r1, r1, #8
    orr.w   r0, r0, r1, lsl #8
    ldrh    r1, [r6], #2
    orr.w   r0, r0, r1, lsr #8
    str     r0, [r5], #4
    subs    r2, #1
    bne     light_word
light_stock:
    mov     r0, r9
    pop.w   {{r3, r4, r5, r6, r7, r8, r9, lr}}
    b.w     light_loop

init_hook:
    movs    r1, #1
    strb    r1, [r0]                    @ what the call replaced
    movw    r0, #{STATE & 0xFFFF}
    movt    r0, #{STATE >> 16}
    movs    r1, #0
    movs    r2, #{STATE_SIZE}
init_clear:
    subs    r2, #4
    str     r1, [r0, r2]
    bne     init_clear
    bx      lr
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


def logo() -> bytes:
    """The start screen's picture: a JPEG the cooler can decode, as it lies in the image after the code."""
    jpeg = LOGO.read_bytes()
    if jpeg[:2] != b"\xff\xd8" or jpeg[-2:] != b"\xff\xd9":
        raise ValueError(f"{LOGO.name} is not a JPEG")
    return jpeg


def assemble(base: int) -> tuple[bytes, dict[str, int]]:
    """The patch's code for address `base`, and where its labels are.

    Twice: a routine may call one that comes after it, and the logo lies after all of them, so the code
    has addresses in it that depend on how long the code is. Its length does not depend on them (calls
    and jumps between routines are written in their long forms, `bl` and `b.w`): so it is assembled for
    made-up addresses first, and again for the ones that gave."""
    size = len(logo())
    names = [routine.split(":")[0] for routine in source(base, size).strip().split("\n\n")]
    first, where = _assemble(base, source(base, size), dict.fromkeys(names, base))
    code, labels = _assemble(base, source(base + len(first), size), where)
    if labels != where or len(code) != len(first):
        raise ValueError("the patch's code changed its length with its addresses: a short jump between routines?")
    return code, labels


def _assemble(base: int, text: str, elsewhere: dict[str, int]) -> tuple[bytes, dict[str, int]]:
    """`elsewhere`: where the routines are taken to be, for the calls from one to another."""
    ks = _assembler()
    code, labels = b"", {}
    for routine in text.strip().split("\n\n"):          # one at a time: each starts with the label it is entered at
        name = routine.split(":")[0]
        known = {**SYMBOLS, **{n: at for n, at in elsewhere.items() if n != name}}
        prologue = "".join(f".equ {n}, {address:#x}\n" for n, address in known.items())
        labels[name] = base + len(code)
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
    image = bytearray(stock + code + logo())
    if len(image) > MOST:
        raise ValueError(f"the image is {len(image)} bytes: the bootloader writes {MOST} and no more")
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


TABLE = Path(__file__).resolve().parents[2] / "deephack" / "firmware_patch.py"
TABLE_TEXT = """\"\"\"The direct drawing patch for the MYSTIQUE's firmware v2.70, as the bytes it changes.

Made by `python tools/fw/patch.py --table` from the patch's source there, which is what to read (and
what tests/test_firmware.py compares this with): do not edit. deephack.firmware applies it to DeepCool's
own image, which is not part of deephack.
\"\"\"
VERSION = @VERSION@
CHANGES = {
@CHANGES@}
STOCK_SHA256 = "@STOCK_SHA256@"      # MYSTIQUE_2.70.bin
STOCK_SIZE = @STOCK_SIZE@
SHA256 = "@SHA256@"            # the patched image
JUMPS = {                 # offset in the file: the four bytes there afterwards
@JUMPS@}
CODE = bytes.fromhex(     # goes after the image's last byte
@CODE@)
"""


def table(stock: bytes) -> str:
    """The patch as a Python module of the bytes it changes (deephack/firmware_patch.py)."""
    image = build(stock)
    code = image[len(stock):].hex()
    parts = {
        "VERSION": str(VERSION),
        "CHANGES": "".join(f'    {version}: "{text}",\n' for version, text in CHANGES.items()),
        "STOCK_SHA256": STOCK_SHA256,
        "STOCK_SIZE": str(len(stock)),
        "SHA256": hashlib.sha256(image).hexdigest(),
        "JUMPS": "".join(f'    {address - APP:#07x}: "{image[address - APP:address - APP + 4].hex()}",\n' for address, _, _ in HOOKS),
        "CODE": "".join(f'    "{code[at:at + 96]}"\n' for at in range(0, len(code), 96)),
    }
    text = TABLE_TEXT
    for name, value in parts.items():
        text = text.replace(f"@{name}@", value)
    return text


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
    ap.add_argument("--table", action="store_true", help=f"write the patch as a table of bytes to {TABLE.name} and stop")
    a = ap.parse_args()
    stock = Path(a.stock).read_bytes()
    if a.list:
        print(listing(stock))
        return 0
    if a.table:
        TABLE.write_text(table(stock), encoding="utf8", newline="\n")
        added = len(build(stock)) - len(stock)
        print(f"{TABLE}: version {VERSION}, {added - len(logo())} bytes of code and a logo of {len(logo())}, "
              f"{MOST - len(build(stock))} bytes of room left, {len(HOOKS)} jumps")
        return 0
    image = build(stock)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image)
    print(f"{out}: {len(image)} bytes ({len(image) - len(stock)} more than the stock image), "
          f"sha256 {hashlib.sha256(image).hexdigest()[:16]}...")
    print("Not flashed. Try it in the emulator:")
    print(f"  python tools/emu/bench.py --firmware {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
