"""A newer deephack: looked for on GitHub, and for the packaged app fetched and put in the old one's place.

- **Looking** is one request to GitHub's public list of releases (no account), once a day and at the
  start if that is switched on, or when asked. What comes back is the newest release's version, its notes
  and where its files are. A newer one is said in Settings and once in the tray; nothing is fetched.
- **Fetching and installing is only done when asked**, and only by the packaged app (the folder with
  deephack.exe): a deephack that runs from a Python of the user's own was put there by pip or git, and
  is updated by them.
- **Checked.** The zip must be the size GitHub names and have the SHA-256 that the release's
  `SHA256SUMS.txt` has for it. That guards against a broken download, not against a bad release: both
  come from the same place, and the app is not signed, so what is trusted is the project's GitHub
  account, as with a download by hand.
- **The app's folder keeps its place**, so that Start with Windows and shortcuts still lead to it.

How the old one is replaced. A running program cannot be written over, so the new one does it:

1. The zip is unpacked next to the app's folder, as `<folder>.new` (`stage`).
2. The new deephack.exe is started from there with `--apply-update <folder>` (`hand_over`). When it
   says that it runs (the file `ready`), the old app quits. A new version that does not even start
   has changed nothing: the old one says so and stays.
3. The new one (`apply`) waits for the old to be gone, copies itself to `<folder>.next`, renames
   `<folder>` to `<folder>.old` and `<folder>.next` to `<folder>`, and starts the app from there, with
   what the old one was started with. (The copy is what takes time, and it is made beside the app: the
   app's folder is missing for the moment between two renames, not for the seconds of a copy.)
4. That app says that it runs (the file `started`, written by `arrived`). If it does not within a minute,
   or the copying fails, `<folder>.old` is put back and started, and says why (`outcome`).
5. The app that then runs, new or old, removes `<folder>.new` and `<folder>.old` (`tidy`).

The command line of step 2 is a promise between versions: an old deephack starts a newer one with it.
`--apply-update FOLDER --wait-pid PID --pass-on JSON` must keep meaning what it means here.

The cooler's firmware is not this module's business: the patch a new deephack carries is offered in
Settings, as before, and nothing is sent to the cooler by an update of the app.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME, __version__
from .config import config_dir

REPO = "a7ex-turcan/deephack"
FEED = f"https://api.github.com/repos/{REPO}/releases/latest"
FILES = f"https://github.com/{REPO}/releases/download/"       # where a release's files must be
FEED_ENV = "DEEPHACK_UPDATE_FEED"           # (development) another address for FEED; the files must be at its host
AGENT = "deephack-updater"
DAY = 86400.0
TIMEOUT = 20.0
LARGEST = 600 << 20                         # bytes a release's zip may be: six times what it is
SMALL = 4 << 20                             # ... and the list of releases, or the checksums
ZIP_END = "-windows-x64.zip"
SUMS = "SHA256SUMS.txt"
EXE = f"{APP_NAME}.exe"
READY_WAIT = 60.0                           # seconds the new deephack has to say that it runs, before the old one quits
                                            # (a program that was unpacked just now is looked at by the virus scanner first)
GONE_WAIT = 60.0                            # ... the old one has to be gone
STARTED_WAIT = 60.0                         # ... and the app in its new place has to say that it runs
PENDING = 600.0                             # seconds after which an update that was begun counts as given up
NOTES_END = "**To install:**"               # in a release's notes: what follows is for somebody downloading by hand


@dataclass(frozen=True)
class Release:
    version: str
    notes: str = ""                         # what changed, as the release says it (Markdown)
    page: str = ""                          # the release on GitHub
    zip_name: str = ""                      # the packaged app: "" when the release has none
    zip_url: str = ""
    zip_size: int = 0
    sums_url: str = ""


def home() -> Path:
    folder = config_dir() / "update"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _state() -> dict:
    try:
        return json.loads((home() / "state.json").read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


_saving = threading.Lock()                  # the look runs in a thread of its own, and the window writes here too


def _save(**values) -> None:
    with _saving:
        state = {**_state(), **values}
        state = {key: value for key, value in state.items() if value is not None}
        tmp = home() / "state.tmp"
        tmp.write_text(json.dumps(state, indent=1), encoding="utf8")
        tmp.replace(home() / "state.json")


def number(version: str) -> tuple[int, ...]:
    """"v0.18.0" as (0, 18, 0), to compare. () for what is no version: it is newer than nothing."""
    found = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (version or "").strip())
    return tuple(int(part) for part in found[1].split(".")) if found else ()


def newer(version: str) -> bool:
    return number(version) > number(__version__)


# ---------------------------------------------------------------- looking
def _feed() -> tuple[str, str]:
    """Where the newest release is asked for, and what its files' addresses must begin with."""
    other = os.environ.get(FEED_ENV, "")
    if other:
        parts = urllib.parse.urlsplit(other)
        return other, f"{parts.scheme}://{parts.netloc}/"
    return FEED, FILES


def fetch(url: str, largest: int = SMALL) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/vnd.github+json, */*"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
        data = answer.read(largest + 1)
    if len(data) > largest:
        raise ValueError("more came than a release has")
    return data


def download(url: str, path: Path, size: int, progress=None) -> str:
    """Fetch a large file to `path`, a piece at a time. Its SHA-256."""
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/octet-stream, */*"})
    digest, got = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=TIMEOUT) as answer, path.open("wb") as out:
        while True:
            piece = answer.read(1 << 18)
            if not piece:
                break
            got += len(piece)
            if got > min(LARGEST, size):
                raise ValueError("the download is larger than GitHub says the file is")
            out.write(piece)
            digest.update(piece)
            if progress:
                progress(got / max(1, size))
    if got != size:
        raise ValueError("the download broke off")
    return digest.hexdigest()


def latest() -> Release:
    """The newest release, as GitHub names it. Raises when it cannot be asked or the answer is none."""
    feed, files = _feed()
    data = json.loads(fetch(feed))
    version = str(data.get("tag_name") or "").lstrip("v")
    if not number(version):
        raise ValueError("GitHub named no release")
    notes = str(data.get("body") or "").split(NOTES_END)[0].strip()
    found = {"version": version, "notes": notes, "page": str(data.get("html_url") or "")}
    assets = [a for a in data.get("assets") or [] if str(a.get("browser_download_url") or "").startswith(files)]
    pack = next((a for a in assets if str(a.get("name", "")).endswith(ZIP_END)), None)
    sums = next((a for a in assets if a.get("name") == SUMS), None)
    if pack and sums and 0 < int(pack.get("size") or 0) <= LARGEST:
        found.update(zip_name=str(pack["name"]), zip_url=pack["browser_download_url"], zip_size=int(pack["size"]),
                     sums_url=sums["browser_download_url"])
    return Release(**found)


def check() -> tuple[str, Release | str]:
    """Look for a newer deephack: ("newer", the release), ("current", this version) or ("failed", why).
    Asks GitHub; what it found is kept, for `known`."""
    _save(checked=time.time())
    try:
        release = latest()
    except Exception as error:                                   # noqa: BLE001  (the net, GitHub, an answer that is none)
        return "failed", (str(error).strip() or type(error).__name__).splitlines()[0][:140]
    _save(latest=release.__dict__)
    return ("newer", release) if newer(release.version) else ("current", __version__)


def known() -> Release | None:
    """The newer release the last look found, without asking again. None when there is none."""
    try:
        release = Release(**(_state().get("latest") or {}))
    except TypeError:
        return None
    return release if newer(release.version) else None


def due(after: float = DAY) -> bool:
    try:
        return time.time() - float(_state().get("checked") or 0) >= after
    except (TypeError, ValueError):
        return True


def tell_once(what: str, version) -> bool:
    """True the first time it is asked for this version of `what` ("app", "firmware"): the tray says a thing once."""
    told = _state().get("told") or {}
    if told.get(what) == version:
        return False
    _save(told={**told, what: version})
    return True


# ---------------------------------------------------------------- fetching
def app_folder() -> Path | None:
    """The packaged app's folder, the one with deephack.exe in it. None for a deephack that runs from Python."""
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None


def beside(folder: Path, end: str) -> Path:
    return folder.with_name(folder.name + end)


def cannot_install(release: Release | None = None) -> str:
    """Why this deephack cannot put a release in its own place; "" when it can."""
    folder = app_folder()
    if folder is None:
        return "This deephack runs from a Python of your own: update it the way it was installed."
    if release is not None and not release.zip_url:
        return "This release has no packaged app."
    probe = beside(folder, ".probe")
    try:
        probe.mkdir(exist_ok=True)
        probe.rmdir()
    except OSError:
        return f"deephack may not write to {folder.parent}: move its folder to a place of your own, or update it by hand."
    return ""


def stage(release: Release, progress=None, folder: Path | None = None) -> Path:
    """Fetch the release's packaged app, check it and unpack it next to the app's folder (`folder`: that
    folder, for tests). The folder it is in now, `<folder>.new`. Raises with the reason when it cannot."""
    folder = folder or app_folder()
    if folder is None or not release.zip_url:
        raise ValueError(cannot_install(release) or "there is nothing to fetch")
    sums = fetch(release.sums_url).decode("utf8", "replace")
    wanted = next((line.split()[0].lower() for line in sums.splitlines()
                   if len(line.split()) == 2 and line.split()[1].lstrip("*") == release.zip_name), "")
    if not re.fullmatch(r"[0-9a-f]{64}", wanted):
        raise ValueError("the release names no checksum for its app")
    packed = home() / "download.zip"
    new, unpacking = beside(folder, ".new"), beside(folder, ".unpacking")
    try:
        if download(release.zip_url, packed, release.zip_size, progress) != wanted:
            raise ValueError("the download is not what the release says it should be")
        for old in (new, unpacking):
            shutil.rmtree(old, ignore_errors=True)
        with zipfile.ZipFile(packed) as archive:
            names = archive.namelist()
            top = {name.replace("\\", "/").split("/")[0] for name in names}
            bad = [name for name in names if name.startswith(("/", "\\")) or ":" in name
                   or ".." in name.replace("\\", "/").split("/")]
            if bad or len(top) != 1:
                raise ValueError("the release's zip is not laid out as deephack's are")
            archive.extractall(unpacking)
        inner = unpacking / top.pop()
        if not (inner / EXE).is_file():
            raise ValueError(f"the release's zip has no {EXE}")
        inner.rename(new)
        return new
    except zipfile.BadZipFile as error:
        raise ValueError("the download is no zip") from error
    finally:
        packed.unlink(missing_ok=True)
        shutil.rmtree(unpacking, ignore_errors=True)


# ---------------------------------------------------------------- the old one's place
def _alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
    if not handle:
        return False                                             # no such process (or not ours to wait for: gone to us)
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102     # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _start(exe: Path, arguments: list[str]):
    """Start a deephack.exe as a program of its own, not as a part of this one. PyInstaller's programs
    tell their children where the parent's files are, and Windows hands on the folder DLLs are looked
    for in: both would make the other deephack run with this one's files."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("_PYI_", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_unicode_buffer(32768)
    had = kernel32.GetDllDirectoryW(len(buffer), buffer)
    kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen([str(exe), *arguments], cwd=str(exe.parent), env=env, close_fds=True,
                                creationflags=0x00000008 | 0x00000200)       # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP
    finally:
        if had:
            kernel32.SetDllDirectoryW(buffer.value)


def _wait(condition, seconds: float, sleep=time.sleep, clock=time.monotonic) -> bool:
    until = clock() + seconds
    while not condition():
        if clock() >= until:
            return False
        sleep(0.1)
    return True


def hand_over(staged: Path, pass_on: list[str], folder: Path | None = None, start=_start, wait: float = READY_WAIT) -> str:
    """Start the new deephack from where it was unpacked and have it take this one's place. "" when it
    runs and waits for this app to quit, which the caller then does; else why not, and nothing has changed."""
    folder = folder or app_folder()
    ready = home() / "ready"
    for mark in (ready, home() / "started"):
        mark.unlink(missing_ok=True)
    _save(pending={"from": __version__, "folder": str(folder), "at": time.time()}, result=None)
    try:
        process = start(staged / EXE, ["--apply-update", str(folder), "--wait-pid", str(os.getpid()),
                                       "--pass-on", json.dumps(pass_on)])
    except OSError as error:
        process, why = None, f"the new version could not be started ({error.strerror or error})"
    else:
        why = "" if _wait(ready.exists, wait) else "the new version does not start on this PC"
    if why:
        if process is not None:
            process.kill()
        _save(pending=None)
        shutil.rmtree(staged, ignore_errors=True)
    return why


def _rename(source: Path, target: Path, seconds: float = 15.0) -> None:
    """Rename a folder that a program has just left: Windows, or a virus scanner, may hold it a moment longer."""
    until = time.monotonic() + seconds
    while True:
        try:
            source.rename(target)
            return
        except OSError:
            if time.monotonic() >= until:
                raise
            time.sleep(0.3)


def apply(folder: Path, pid: int, pass_on: list[str], here: Path | None = None, start=_start, alive=_alive,
          gone_wait: float = GONE_WAIT, started_wait: float = STARTED_WAIT) -> int:
    """What the new deephack does when the old one started it with --apply-update: take the old one's
    place and start the app there. See the module's text. 0 when the app runs in its place, new or old."""
    here = here or app_folder()
    old, coming, started = beside(folder, ".old"), beside(folder, ".next"), home() / "started"
    started.unlink(missing_ok=True)
    (home() / "ready").write_text(str(os.getpid()), encoding="utf8")

    def failed(why: str, restart: bool) -> int:
        _save(pending=None, result={"how": "failed", "version": __version__, "message": why})
        if restart:
            try:
                start(folder / EXE, pass_on)
            except OSError:
                pass
        return 1

    if not _wait(lambda: not alive(pid), gone_wait):
        return failed("the running deephack did not close", restart=False)
    try:
        for left in (old, coming):
            shutil.rmtree(left, ignore_errors=True)
        shutil.copytree(here, coming)
    except OSError as error:
        shutil.rmtree(coming, ignore_errors=True)
        return failed(f"the new version could not be put in its place ({error.strerror or error})", restart=True)
    try:
        _rename(folder, old)
    except OSError as error:
        shutil.rmtree(coming, ignore_errors=True)
        return failed(f"the app's folder could not be moved aside ({error.strerror or error})", restart=True)
    try:
        _rename(coming, folder, 5.0)
        process = start(folder / EXE, pass_on)
    except OSError as error:
        process, why = None, f"the new version could not be started in its place ({error.strerror or error})"
    else:
        why = "" if _wait(started.exists, started_wait) else "the new version did not start"
    if not why:
        _save(pending=None, result={"how": "updated", "version": __version__, "message": ""})
        return 0
    if process is not None:
        process.kill()
        try:
            process.wait(10)
        except Exception:                                        # noqa: BLE001
            pass
    try:                                                         # the old one back in its place
        if folder.exists():
            _rename(folder, beside(folder, ".failed"), 5.0)
        _rename(old, folder, 5.0)
    except OSError:
        return failed(f"{why}, and the old one could not be put back: it is in {old}", restart=False)
    return failed(why, restart=True)


def arrived() -> None:
    """Called by the app once it runs: says so to a new version that waits to hear it (see `apply`)."""
    try:
        (home() / "started").write_text(f"{__version__}\n{sys.executable}\n", encoding="utf8")
    except OSError:
        pass


def _pending() -> bool:
    """An update is being put in place right now (and was not given up half way, by a crash or a switched off PC)."""
    try:
        return time.time() - float((_state().get("pending") or {}).get("at") or 0) < PENDING
    except (TypeError, ValueError):
        return False


def outcome() -> tuple[str, str] | None:
    """How the last update went, said once: ("updated", this version), ("failed", why) or None. The app
    asks some seconds after its start: the new version that put it there writes this down when it has
    heard that the app runs."""
    result = _state().get("result")
    if not result or _pending():
        return None
    _save(result=None)
    if result.get("how") == "updated":
        return ("updated", __version__) if result.get("version") == __version__ else None
    return "failed", str(result.get("message") or "it did not work")


def tidy(folder: Path | None = None) -> None:
    """Remove what an update left beside the app's folder: the new version as it was unpacked, the old one
    that was moved aside. Not while one is being put in place."""
    folder = folder or app_folder()
    if folder is None or _pending():
        return
    for end in (".new", ".old", ".next", ".failed", ".unpacking"):
        shutil.rmtree(beside(folder, end), ignore_errors=True)
