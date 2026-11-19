# deephack

Custom content for the LCD on the **DeepCool MYSTIQUE** liquid cooler: the album art of whatever you are
listening to, the weather, and whatever comes next.

![deephack window](docs/screenshot.png)

The official DeepCool app can show sensor readouts and images you upload by hand. deephack talks to the
cooler directly over USB and keeps one image slot up to date with content it renders itself.

## Modes

| Mode | What it shows |
| --- | --- |
| **Auto** | Whatever has something to show right now, by a priority order you set: the game you are playing, the music that is on, Claude Pet while Claude Code works, your NBA team around its games, a countdown on its last day. Otherwise a fallback mode such as Weather. A mode keeps the screen for two minutes after it goes quiet, so a paused song does not flip the display. |
| **Now Playing** | Cover, title and artist of the current track. Uses Windows' system media controls, so it works with Spotify, anything playing in a browser, foobar2000 and so on. |
| **Weather** | Current conditions and a three day outlook for a city you pick, from [Open-Meteo](https://open-meteo.com) (no account or key). |
| **Calendar** | The next event with a countdown, and the four after it, from one or more .ics calendar links (Google Calendar's "secret address in iCal format", Outlook.com's published ICS link, iCloud) or an exported .ics file. Recurring events and time zones included. The countdown moves in coarse steps (days, hours, half hours, ten minutes), about ten writes per event. In Auto it takes the screen from 15 minutes before an event until it ends. |
| **NBA** | The last two results and the next two games, with team logos, for one team or the whole league. From ESPN's public feed; tip-off times in your local time zone. While your team plays, a big scoreboard with points per quarter takes over, updated every few minutes. |
| **Claude Code** | Plan limits (5-hour and weekly) and today's tokens, requests, hourly activity and model split. All from local files, see below. |
| **Claude Pet** | An animated pixel pet with your plan limit percentages. It works along while Claude Code is busy, dances while music plays, sleeps at night and celebrates or sulks after your NBA team's games; you pick which of these count and which wins. Otherwise its mood follows the fullest limit: happy, fine, stressed, exhausted. |
| **Countdown** | Days until a date you set, with a title. On the last day it counts hours, then minutes. |
| **Photo Frame** | Pictures from a folder you pick, changing every 15 minutes to once a day, shuffled or by file name. Portrait photos fill the screen, wider ones are shown whole. |
| **Game Cover** | The cover art of the game you are playing, for games started through Steam (Steam notes the running game in the registry; covers come from its image server). |
| **Ticker** | Up to five stocks, indices, currency pairs or coins with price, change and a sparkline for today, five days or a month. From Yahoo Finance's public chart feed (no account or key). |
| **System** | CPU and GPU temperature, load and power, memory, and pump and fan speeds, with graphs of the last half hour to three hours. Readings are taken every 10 seconds; the screen is redrawn every few minutes, since each redraw is a flash write. Temperatures and fans come from DeepCool's own sensor service (see [Protocol](#protocol)); without it only CPU load and memory are shown. |
| **Live** | The cooler's own built-in screens (a big CPU temperature, frequency, fan and pump speed, a clock, or a 7 minute graph of frequency and temperature) fed with real readings every second. The cooler draws these itself and stores nothing, so this is the one mode that updates every second without wearing the flash. A good fallback for Auto. |
| **GitHub** | Your contribution heatmap for the last four months, then the CI status and open pull requests of a repository you pick (or your own open pull requests anywhere). One GraphQL request per refresh, redrawn only when something changed. The token is borrowed from the [GitHub CLI](https://cli.github.com) (`gh auth login` once); deephack stores none. In Auto it can take the screen while the repository's build runs or fails. |

Clicking a mode opens its page with a live preview and leaves the cooler alone; **Show on cooler** (or a
double-click on the mode) puts it on the display. A green dot marks the mode that is on it.

**Keep on screen** decides whether the cooler stays on deephack's image or cycles through every image
stored on it (the cooler's own slideshow).

## Install and run

Windows 10/11 and Python 3.11+.

```powershell
git clone <this repo> deephack
cd deephack
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\deephack.exe
```

The window closes to the system tray; quit from the tray menu. "Start with Windows" adds a per-user
autostart entry.

### Things to know

- **Quit the DeepCool app first.** It holds the cooler's USB interface exclusively. deephack shows a banner
  with a button for that. The DeepCool app starts with Windows by default, so disable its autostart if you
  want deephack to own the display after a reboot. Fan control is a separate Windows service and keeps
  working, and so does the sensor service the System and Live modes read.
- **Every update is a write to the cooler's flash**, except in Live, which uses the cooler's own sensor
  screens. There is no known way to draw our own pictures without storing them first. deephack rate-limits
  writes (at most one every 8 seconds, tracks have to settle before they are drawn), but the flash
  endurance of the device is unknown. "Cover only" writes once per album instead
  of once per track. Showing an image the cooler already holds again (Auto switching back to it) costs
  no write.
- **deephack adds files the DeepCool app does not know about:** one image, and one animation once you use
  Claude Pet. They are stored after the JPGs / GIFs you uploaded with the official app. If you later add
  files there, its numbering will be off by one. `python -m deephack.device delete <index>` (add `--gif`
  for the animation) removes them; the indexes are `slot_index` and `gif_slot_index` in
  `%APPDATA%\deephack\config.json`.
- Only the MYSTIQUE (USB `3633:0009`, 480x640 portrait) is supported. The MYSTIQUE PLUS appears to share
  much of the protocol but is untested.

### Claude Code mode

Nothing leaves your machine and no credentials are read.

- **Today's numbers** are summed from Claude Code's session logs in `~/.claude/projects`.
- **Plan limits** are only handed out by Claude Code to status line plugins. deephack reads the snapshot file
  that the [claude-hud](https://github.com/jarrodwatts/claude-hud) plugin can write. Add this to
  `~/.claude/plugins/claude-hud/config.json` (adjust the user name):

  ```json
  { "display": { "externalUsageWritePath": "C:\\Users\\you\\AppData\\Roaming\\deephack\\claude-usage.json" } }
  ```

  The snapshot only updates while a Claude Code session is open. Only the windows your plan
  has are drawn (some plans have no 5-hour window); without a snapshot the limits card is left out.

## Adding a mode

A mode is a class that returns a 480x640 Pillow image whenever the display should change:

```python
# deephack/modes/hello.py
from .. import render
from .base import Mode

class Hello(Mode):
    id, title, description, glyph = "hello", "Hello", "Says hello", ""

    async def frame(self):
        if getattr(self, "_done", False):
            return None            # nothing changed, no flash write
        self._done = True
        return render.message("Hello", "from the cooler")
```

Add it to `MODES` in `deephack/modes/__init__.py`. Declare `settings` on the class and the UI builds the
form for you (see `weather.py`). `python -m deephack --no-device` runs the UI without touching the cooler.

To let Auto pick the mode, set `claims` to a few words saying when it has something to show
(`claims = "music playing"`), keep `self.active` up to date in `frame()`, and add the class to
`CANDIDATES` in `auto.py`.

## Protocol

Reverse engineered from the official app (v1.2.15, Electron) by attaching to its debug port and logging
its USB transfers; the tooling is in [`tools/`](tools). Tested against one device (USB `bcdDevice` 2.47).

The cooler is a full-speed vendor-class device with two bulk endpoint pairs (`0x01/0x81` for commands and
uploads, `0x02/0x82` for the periodic sensor feed).

**Command frame**, 48 bytes, answered with the same layout starting with `55`:

```
AA 2E <cmd> <payload, zero padded to 39 bytes> 'H' 'I' 'D' 'C' <sum16 LE of bytes 0..45>
```

| cmd | meaning | payload |
| --- | --- | --- |
| `01` | sensor values (ep 2, once a second) | 3-byte fields, see below |
| `02` | device options | five bytes, the official app sends `01 01 00 01 46`. The fifth is the backlight brightness in percent (0 turns the screen off); the others showed no visible effect |
| `03` | display mode | `mode` (1 = sensor dashboard, 2 = stored images, 3 = recorder) |
| `05` | recorder layout | `0` frequency + temperature graphs, `1` frequency graph only |
| `07` | slideshow | `duration` `animation` (0 none, 1 slide, 2 fade) |
| `08` | show stored image | `type` (0 jpg, 1 gif) `index` |
| `09` | delete stored image | `type` `index` |
| `0A` | set clock (ep 2) | `year:u16` `month` `day` `hour` `minute` `second` |
| `0F` | start upload | none |
| `12` | get UUID | reply: status byte + 32 ASCII characters |
| `13` | **factory reset** | none. Reboots and erases every stored image. `12` returns junk until the next power cycle |
| `14` | **delete all stored images** | none |

Commands `0B`, `0C`, `0D`, `0E` and `11` are acknowledged but did nothing visible with an empty payload.
`18` to `30` are answered with command byte `00`, which looks like "unknown command". `13` and `14` were
found the hard way; `tools/restore_originals.py` re-uploads what the official app had stored.

**Recorder mode** (`03` = 3) plots the last seven minutes of the CPU frequency and temperature you feed
with command `01`, as live line graphs. Like the dashboard it is not stored, so it costs no flash writes.
Command `05` picks the graphs: `00` frequency and temperature, `01` frequency only.

**Sensor values** (command `01`) feed the firmware's own dashboard and are not stored, so they can change
every second without wearing the flash. The payload is a row of 3-byte fields, `integer:u16` `fraction:u8`;
the fraction byte is printed as-is after the decimal point.

| offset | field | status |
| --- | --- | --- |
| 0 | CPU temperature | confirmed on the device |
| 3 | CPU load % | confirmed on the device |
| 6 | RAM usage % | confirmed on the device |
| 9, 12, 15 | 3.3 V, 5 V, 12 V rails | confirmed on the device |
| 18 | CPU frequency (GHz) | confirmed on the device |
| 21 | CPU fan RPM | confirmed on the device (the official app left it at zero) |
| 24 | pump RPM | confirmed on the device (the official app left it at zero) |
| 27 and up | unknown | not shown on any layout seen so far |

Command `04` picks the dashboard layout with four bytes: `main` (0 frequency, 1 time, 2 pump, 3 CPU fan,
4 CPU fan + pump, 5 temperature), `style` (frequency only), one byte with no visible effect, and `strip`
for the row at the bottom (0 voltage rails, 1 frequency / CPU load / RAM, 2 frequency / RAM). The labels
and units on every layout are fixed in the firmware; only the numbers are yours.

`python -m deephack.device probe-sensors` shows the dashboard with test values, and `map-dashboards` steps
through layouts.

The Live mode drives both screens with real readings: `0A` sets the clock, `03` switches to the
dashboard (1) or recorder (3), `04` or `05` picks the layout, then `01` follows once a second. Before
showing a stored image again deephack switches back with `03 02` and re-sends the slideshow setting (`07`).
Confirmed on the device, switching both ways.

**Image upload**

1. command `0F`, wait for the reply
2. 64 byte header: `"DCLd"`, type (1), size `u32`, sum16 of the data as `u32`, 7 zero bytes, 32 character
   file id, 10 zero bytes, sum16 of the first 62 header bytes as `u16`
3. the JPEG in 64 byte packets: 480x640, baseline, 4:2:0
4. `"dcldfinish"` zero padded to 55 bytes; no reply, the device then writes to flash (wait about a second)
5. command `08` to show it

Uploading with an id that already exists replaces that image in place.

The image is only displayed once it is committed: after the data without `dcldfinish` the screen shows
"Loading…" until the marker arrives. There is no preview of uncommitted data, and experimenting with
unfinished uploads hung the firmware once (no endpoint accepted data until the PC was powered off).

**Animations** (what the official app calls GIFs) are a sequence of JPEG frames, stored separately from the
stills and shown with type 1. One `0F` command, then a header + JPEG for every frame, then one
`dcldfinish`. The frame headers use type 2 and fill three `u16` fields at offset 13: frame count, total
loop duration in milliseconds, and the frame's index.

A mode returns an `Animation(frames, duration_ms)` instead of an image to use this (see `tamagotchi.py`).
Every frame is written to flash, so keep them few.

`python -m deephack.device --help` exposes the driver on the command line.

**DeepCool's sensor service.** The DeepCool app installs a Windows service, "JZFS Sensor Bridge Service",
that reads the hardware through HWiNFO and answers on the named pipe `\\.\pipe\jzfs_sensor_bridge_v2`,
also to programs without admin rights, and keeps running when the app is closed. Requests are JSON in a
small binary frame (`"DCS2"`, kind, length, sequence, CRC-32); `HELLO` and then `GET_SENSOR_SNAPSHOT`
return the readings by name (`"CPU Temperature"`, `"Gpu usage"`, `"Fan list"`, ...). The System and Live
modes read it. [docs/deepcool-services.md](docs/deepcool-services.md) has the frame layout, every field,
the fan control and display services next to it, and how all this was found.

## Disclaimer

Not affiliated with or endorsed by DeepCool. This talks to your hardware in ways the vendor did not
document; use it at your own risk.
