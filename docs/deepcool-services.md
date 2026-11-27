# DeepCool's background services

The DeepCool app (v1.2.15 here, installed in `C:\DeepCool`) installs three Windows services. They run as
LocalSystem, start with Windows and keep running when the app's window is closed or the app is quit, so
deephack can use them while it owns the cooler's USB interface.

| Service | Executable (under `resources\service\x64\`) | Named pipe |
| --- | --- | --- |
| JZFS Sensor Bridge Service | `sensor-bridge\JZFSSensorBridgeServer.exe`, which starts `SensorWorker.exe` (uses the bundled `HWiNFO64.dll`) | `\\.\pipe\jzfs_sensor_bridge_v2` |
| FanControl | `fan-control\FanControlService.exe` | `\\.\pipe\DeepcoolFanControl` |
| JZFS Display Service | `display-service\JZFSDisplayService.exe` | `\\.\pipe\jzfs_display_service_v2` |

The pipe names are in each service's `config.json` next to the executable. The message formats come from
`tools/fan-sensor-observer.mjs` inside `resources\app.asar`: a read-only test client DeepCool left in the
package, together with its test script (`fan-sensor-admin-test.ps1`) and logs. Everything below was then
checked against the services on one machine (ASUS ProArt B650-CREATOR, Ryzen 9 7950X, RTX 4080 SUPER),
except where it says otherwise.

## Sensor bridge

This is what the System and Live modes read. It is the only way found so far to get the CPU temperature
and fan speeds without admin rights: Windows itself does not expose them, and this board has no ACPI
thermal zone (the `\Thermal Zone Information(*)\Temperature` counter has no instances).

**Access:** a process without admin rights can connect (checked with a `runas /trustlevel:0x20000`
token). `config.json` allows 4 clients at a time and payloads up to 1 MiB.

**Framing:** every message, in both directions, is a 20 byte header and a UTF-8 JSON body.

| offset | size | field |
| --- | --- | --- |
| 0 | 4 | `"DCS2"` |
| 4 | 1 | version, `2` |
| 5 | 1 | kind: `1` request, `2` response, `3` pushed event |
| 6 | 2 | zero |
| 8 | 4 | body length, `u32` LE (at most 1 MiB) |
| 12 | 4 | sequence number, `u32` LE; a response repeats its request's |
| 16 | 4 | CRC-32 (zlib) of the body, `u32` LE |

A request body is `{"op": "<OP>", "body": {}}`, a response `{"ok": true, "op": "<OP>", "body": {...}}`
(with an `error` object instead when `ok` is false).

| op | answer |
| --- | --- |
| `HELLO` | `{"server": "SensorBridge", "productVersion": "2.0.0", "version": 2, "gitSha": "530a2d65f189"}` |
| `GET_SENSOR_SNAPSHOT` | the readings, see below. Answered from a cache in about a millisecond, so asking once a second is cheap |
| `SUBSCRIBE_SENSOR` | from then on, kind 3 events about every 2 seconds: `{"event": "SENSOR_SNAPSHOT", "timestampMs": ..., "health": {"state": "ok", "ageMs": 183, "lastErrorCode": "", "workerRestarts": 1}, "body": {...}}` |

The observer sends `HELLO` first; deephack does the same, then only `GET_SENSOR_SNAPSHOT`. `ageMs` in
the events puts the readings at 0.2 to 0.4 seconds old.

**Where the readings are in the answer differs by the app's version.** With v1.2.15 they are the
answer's `body` itself. With v1.2.16 (seen on another owner's PC, 27 Nov 2026, issue #61) the body is
`{"event": "SENSOR_SNAPSHOT", "snapshot": {"collectedAtMs": ..., "sequence": ..., "data": {...}}}` and the
readings are `body.snapshot.data`. deephack takes either (`BridgeClient.snapshot()`); a deephack up to
0.19.0 found no reading in the newer answer and showed noughts.

**Snapshot:** 28 entries, each `{"Unit": ..., "Value": ...}` except `"Fan list"`. Values from the machine
above:

| key | unit | example | notes |
| --- | --- | --- | --- |
| `CPU Temperature` | `C` | 75.5 | |
| `CPU Usage` | `%` | 34.6 | |
| `CPU Power` | `W` | 143.9 | |
| `CPU Clock` | `MHz` | 5375 | |
| `Gpu temperature` | `C` | 42.1 | one GPU is reported (here a discrete card next to an integrated one) |
| `Gpu usage` | `%` | 0 | |
| `Gpu power` | `W` | 45 | |
| `Gpu clock`, `Gpu memory clock` | `MHz` | 600, 3200 | |
| `Gpu memory usage` | `MB` | 16.5 | |
| `Core GPU Clock` | `-` | 0 | apparently unused on this machine |
| `Memory Load` | `%` | 51.8 | |
| `Memory Used`, `Memory Available` | `MB` | 33533, 31123 | |
| `Memory Clock` | `MHz` | 3200 | |
| `Memory Temp` | `C` | 40 | |
| `Motherboard v3 volt`, `v5 volt`, `v12 volt` | `V` | 3.28, 4.98, 12.08 | the same rails the cooler's dashboard shows |
| `Disk Temperature` | `C` | 42 | |
| `Disk Read Rate`, `Disk Write Rate` | `MB/s` | 0.003, 2.9 | |
| `Network download rate`, `Network upload rate` | `KB/s` | 103, 4.1 | |
| `Ethernet Status`, `WiFi Status`, `Bluetooth Status` | | `"active"`, `"on"`, `"connected"` | strings |
| `Fan list` | | `{"AIO Pump": 3341, "CPU": 1700, "CPU_OPT": 829}` | a plain name → RPM map |

Things to know when reading it:

- **Fan names are the board's own header names.** The ASUS board above (Nuvoton NCT6799D) reports
  `AIO Pump`, `CPU` and `CPU_OPT`. DeepCool's log, from a board with an ITE IT8689E, has `CPU`,
  `System 2` and `System 3` and no pump. deephack takes the pump from any name containing "pump" and the
  CPU fan from `CPU` (then any other `CPU…` header that is not `OPT`, then any fan).
- **A missing sensor reads 0**, like the temperature of a GPU that is not there. deephack treats a 0 °C
  temperature as "no sensor".
- The values move with the hardware (a few snapshots two seconds apart differ in every load and
  temperature); the pump RPM is quantised and repeats often.

`sensor-bridge\logs-server\jzfs\worker\sensor.json` is a fuller dump that the worker wrote at some
point (here on 2026-10-28, not kept current): every HWiNFO reading grouped by chip, for example
`"ASUS ProArt B650-CREATOR (Nuvoton NCT6799D)": {"AIO Pump": {"Type": "3", "UNIT": "RPM", "VALUE": 3341.0}, ...}`.
Useful for seeing which sensors a machine has.

## Fan control

Not used by deephack. It presumably also accepts commands that change fan speeds, so leave it alone.

Frames are `u16 0xFC01` (LE), command `u8`, flags `u8` (1 marks a response), payload length `u32`,
payload. The observer only sends command `2` (with an empty payload), which per its comment returns a
cached status without triggering hardware detection. The status is binary: controller count `u8`, then
per controller a name (`u8` length + UTF-8) and a fan count `u8`, then per fan an index `u8`, a name, a
presence byte and `f32` RPM, a presence byte and `f32` control value, and a manual mode byte.

DeepCool's log shows `[{"name": "ITE IT8689E", "fans": [{"index": 0, "name": "Fan #1", "rpm": 2129.3,
"control": null, "manualMode": false}, ...]}]`. On the machine above, the same request got no answer
within 15 seconds while the DeepCool app was running; not looked into further.

## Display service

Not used and not decoded; only the pipe name is known. The app also installs a virtual display adapter,
"JZFSDisplayDriver Device" (`SWD\JZFSDISPLAYDRIVER\JZFS-DISPLAY`, 3840 x 2160 here), presumably for
mirroring part of the desktop to a device's screen. That is a guess.

## Without the services

Windows gives CPU load (`GetSystemTimes`) and memory (`GlobalMemoryStatusEx`) without admin rights; the
System and Live modes fall back to those. CPU temperature and fan speeds need a kernel driver, which is
what HWiNFO brings for the sensor bridge. For NVIDIA cards, `nvml.dll` (installed with the driver)
reports GPU temperature, load, fan and power without admin rights; deephack does not use it yet.
