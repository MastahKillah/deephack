# Changelog

What changed in each release of deephack, newest first. The notes of a GitHub release are its section
here: the release workflow takes them from this file, and does not build a version that has no section.

"Patch" is the direct drawing patch for the cooler's firmware. The app carries it and installs it from
Settings; a newer one is offered there when the cooler has an older one.

## 0.12.0 (2026-11-24)

- **Firmware patch 9: frames arrive faster.** The cooler takes a frame's bytes at 997 KB a second,
  whatever the frame's size. Patches 3 to 6 took frames over 26 KB at 540: a frame of 40 KB every 100 ms,
  now every 55. A frame of up to 33 KB is on the glass at the panel's next refresh (it was 26 KB).
- **Sharper video at the same rate.** With patch 9 on the cooler, Video and TikTok let a frame be 32 KB
  where they kept it to 22, and go up to JPEG quality 80 where they stopped at 65. The NBA highlights
  play at quality 65 and more, 27 pictures a second.
- Patches 7 and 8 were tries on the way and are in no release.

## 0.11.1 (2026-11-24)

- **Smaller frames.** Every drawn picture carries Huffman tables made for it. The cooler shows the same
  pixels from fewer bytes: 3 to 5 % fewer for a busy picture, 25 to 45 % for a page with flat areas, 61 %
  for a screen of one colour.
- `tools/screen.py`: the cooler's screen read back over USB, pixel for pixel (needs patch 4 or later).

## 0.11.0 (2026-11-23)

- **Firmware patch 6: deephack's own start screen.** While the PC starts, the cooler shows deephack's
  logo where DeepCool's was, from the first moment until an app takes the screen over.
- The patch can be asked for the cooler's memory (patch 4). `tools/fw/dump.py` reads the bootloader out
  with it.

## 0.10.0 (2026-11-23)

- **Firmware patch 3: a part of the picture drawn by itself.** When only one rectangle of a picture
  changes, the page is sent once and after that only the rectangle. The NBA highlights use it: 21 KB a
  frame where the whole page was 29.
- For modes: return `Part(page, picture, at)`. A cooler with an older patch gets whole pictures.

## 0.9.0 (2026-11-23)

- **NBA: the highlights of your team's game**, once it is over: found on YouTube, played once between
  the final score and the points per quarter, with a button and a key to play them again. Off unless
  switched on.
- **Now Playing: the track's music video**, in step with the music, in the cover's place or over the
  whole screen. Off unless switched on.
- Scene: Chișinău's City Gates are wider.

## 0.8.0 (2026-11-23)

- **More pictures a second.** The cooler had been waiting for the app, not the other way round. A
  YouTube video of 25 a second gets 25 (16 before), TikTok 24 to 27, the dancing pet and the Visualizer
  26, Mirror 19 to 26. Video and TikTok lower the JPEG quality before they drop pictures.

## 0.7.1 (2026-11-23)

- Fixed: Video kept some thirty idle threads for every size of video it had played (#53).

## 0.7.0 (2026-11-23)

- Claude Pet: the title and artist of the song it dances to, above it, and the album's cover behind the
  dance (which can be switched off).

## 0.6.0 (2026-11-23)

- **Mirror**: a window or a part of the screen on the cooler, live.
- **Scene: real cities**, drawn by their landmarks: Chișinău, Paris, London, New York, Tokyo.
- Countdown: a Christmas look.
- Settings: a newer yt-dlp can be fetched from the app, for when YouTube links stop working.
- `tools/longrun.py`, for runs of hours on the cooler, and `docs/reversing.md`, how the cooler's screen
  was reverse engineered.

## 0.5.0 (2026-11-22)

- **A Settings page**: what belongs to the app, the cooler and its firmware is there now, off the mode
  pages.
- Video and TikTok: sound from the PC, in time with the picture.
- The MIT licence, and a list of what the standalone app carries.

## 0.4.0 (2026-11-20)

- **TikTok**: random videos without an account, with Next and Previous as buttons and as keys.

## 0.3.0 (2026-11-20)

- **Video**: a YouTube link or a video file plays on the cooler. Needs the direct drawing firmware.

## 0.2.0 (2026-11-20)

- **The app installs the direct drawing firmware** on the cooler, and DeepCool's own again, from its
  window.
- Firmware patch 2: frames are taken after a fast animation has played, and the patch says which version
  it is.

## 0.1.0 (2026-11-20)

The first release.

- Modes: Now Playing, Weather, Claude Code usage, Claude Pet, NBA, Countdown, Photo Frame, Game Cover,
  Ticker, System, Live (the cooler's own sensor screens), GitHub, Calendar, Scene, Visualizer, and Auto,
  which shows whatever has something to show.
- **Direct drawing**: a patch for the cooler's firmware that shows a picture straight from USB, without
  storing it. With it every mode is drawn live: a progress bar in Now Playing, a city with moving cars,
  a pet that dances to the beat.
- The cooler's side light and, through OpenRGB, the PC's other lights follow the screen.
- An emulator that runs the cooler's firmware (`tools/emu`), which is where the patch was written.
- Releases built by GitHub: the standalone app, the wheel and the source archive.
