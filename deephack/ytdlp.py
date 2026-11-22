"""yt-dlp, the program that finds the stream behind a YouTube link, and a newer one when there is one.

YouTube changes how its videos are handed out several times a year. yt-dlp follows within days, but a
deephack that was installed once carries the yt-dlp of that day: its links stop playing at some point.
So deephack can fetch the newest yt-dlp itself (it is plain Python, one file of 3 MB from PyPI), keep
it in its settings folder and use it instead of its own.

- **Only when asked.** "Update now" in the app's Settings, or the switch there ("Keep it current"):
  with that on it is looked for at most once a day, and once more when a link will not open. The switch
  is off unless somebody turns it on: this is code from the net that deephack then runs.
- **Checked.** What was downloaded must have the size and the SHA-256 that PyPI names for it. That
  guards against a broken download, not against a bad release of yt-dlp: it is trusted as much as the
  one deephack came with, which is from the same place.
- **Thrown away when it does not work.** A copy that will not load, or that is not the version it says
  it is, is deleted and deephack's own is used. So is a copy fetched by another deephack than this
  one: a new deephack brings a new yt-dlp.

A fetched copy can still fail in the packaged app where it works from source: that app carries only the
parts of Python's own library that something in it uses, and a later yt-dlp may want one more. It is
thrown away then like any copy that does not load, and `state.json` in its folder says why ("dropped").

Not done: yt-dlp says YouTube will need a JavaScript runtime (deno) next to it before long. deephack
brings none, and this fetches none.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import re
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .config import config_dir

INDEX = "https://pypi.org/pypi/yt-dlp/json"
DAY, HOUR = 86400.0, 3600.0
LARGEST = 40 << 20                               # bytes: ten times what it is today
TIMEOUT = 20.0

wanted = lambda: False                           # noqa: E731  "Keep it current" is on (the app sets this)
_lock = threading.RLock()
_loaded_from: str | None = None                  # "" for deephack's own, a folder for a fetched one; None: not loaded yet


def home() -> Path:
    return config_dir() / "yt-dlp"


def _state() -> dict:
    try:
        return json.loads((home() / "state.json").read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def _save(**changes) -> None:
    state = {**_state(), **changes}
    home().mkdir(parents=True, exist_ok=True)
    (home() / "state.json").write_text(json.dumps(state), encoding="utf8")


def number(version: str) -> tuple[int, ...]:
    """"2026.10.26" as something to compare."""
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"deephack/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
        data = answer.read(LARGEST + 1)
    if len(data) > LARGEST:
        raise OSError("more than was expected")
    return data


# ---------------------------------------------------------------- which one is used
def _forget() -> None:
    for name in [name for name in sys.modules if name == "yt_dlp" or name.startswith("yt_dlp.")]:
        del sys.modules[name]


def fetched() -> tuple[str, Path] | None:
    """The fetched copy that is to be used: (version, folder). None when there is none, or it belongs
    to another deephack than this one."""
    state = _state()
    version = state.get("version")
    if not version or state.get("app") != __version__:
        return None
    folder = home() / version
    return (version, folder) if (folder / "yt_dlp" / "__init__.py").is_file() else None


def _drop(folder: Path, why: str = "") -> None:
    """Throw a fetched copy away, and write down why: nobody sees it happen."""
    shutil.rmtree(folder, ignore_errors=True)
    _save(version=None, dropped=f"{folder.name}: {why}"[:300])


def _load():
    """Import yt-dlp: the fetched copy if there is one and it works, deephack's own otherwise."""
    global _loaded_from
    copy = fetched()
    if copy is not None:
        version, folder = copy
        _forget()
        sys.path.insert(0, str(folder))
        try:
            module = importlib.import_module("yt_dlp")
            where = Path(getattr(module, "__file__", "") or "").resolve()
            if folder.resolve() not in where.parents or module.version.__version__ != version:
                raise ImportError("not the copy that was fetched")
            module.YoutubeDL                                 # it has to be yt-dlp, not just something that imports
            _loaded_from = str(folder)
            _save(loaded=time.time())                        # for whoever wonders which one a running app has
            return module
        except Exception as error:                           # noqa: BLE001  (whatever a broken copy does)
            _forget()
            _drop(folder, f"{type(error).__name__}: {error}")
        finally:
            if str(folder) in sys.path:
                sys.path.remove(str(folder))
    if _loaded_from:                                         # a fetched one was in use, and is no more
        _forget()
    # Written out as an import, not asked for by name: the packaged app carries what it can see being
    # imported, and a deephack.exe built without this line had no yt-dlp in it at all.
    import yt_dlp

    _loaded_from = ""
    return yt_dlp


def module():
    """yt-dlp, loaded. (A second for the first call.)"""
    with _lock:
        copy = fetched()
        want = str(copy[1]) if copy else ""
        if "yt_dlp" in sys.modules and (_loaded_from or "") == want:
            return sys.modules["yt_dlp"]
        return _load()


def version() -> tuple[str, bool]:
    """(the version in use, whether it is a fetched one). Does not load it: the version of deephack's
    own is read from its installation, and is "" where that cannot be done."""
    copy = fetched()
    if copy is not None:
        return copy[0], True
    if "yt_dlp" in sys.modules and hasattr(sys.modules["yt_dlp"], "version"):
        return sys.modules["yt_dlp"].version.__version__, False
    try:
        from importlib import metadata

        return metadata.version("yt-dlp"), False
    except Exception:                                        # noqa: BLE001
        return "", False


# ---------------------------------------------------------------- a newer one
def _python_will_do(requires: str | None) -> bool:
    """">=3.11" and this Python. Anything else that is asked for is taken as fine."""
    found = re.fullmatch(r"\s*>=\s*(\d+)\.(\d+)[\d.]*\s*", requires or "")
    return found is None or sys.version_info[:2] >= (int(found[1]), int(found[2]))


def update() -> tuple[str, str]:
    """Look for a newer yt-dlp and take it into use: ("updated", version), ("current", version) when
    there is none newer, or ("failed", why). Asks PyPI; takes some seconds."""
    with _lock:
        _save(checked=time.time())
        try:
            using = module().version.__version__
            index = json.loads(fetch(INDEX))
            newest = index["info"]["version"]
            if number(newest) <= number(using):
                return "current", using
            if not _python_will_do(index["info"].get("requires_python")):
                return "failed", f"yt-dlp {newest} needs a newer Python than this deephack has: update deephack"
            wheel = next((file for file in index["urls"] if file.get("packagetype") == "bdist_wheel"
                          and file["filename"].endswith("-py3-none-any.whl") and not file.get("yanked")), None)
            if wheel is None:
                return "failed", f"yt-dlp {newest} is not there in a form deephack can use"
            data = fetch(wheel["url"])
            if len(data) != wheel["size"] or hashlib.sha256(data).hexdigest() != wheel["digests"]["sha256"]:
                return "failed", "the download is not what PyPI says it should be"
            folder = home() / newest
            shutil.rmtree(folder, ignore_errors=True)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = [name for name in archive.namelist() if name.startswith("yt_dlp/") and ".." not in name.split("/")]
                archive.extractall(folder, names)
            _save(version=newest, app=__version__, sha256=wheel["digests"]["sha256"], fetched=time.time())
            if _load().version.__version__ != newest:        # it would not load: it is gone again, and ours is back
                return "failed", f"yt-dlp {newest} was fetched but does not work here"
            for other in home().iterdir():
                if other.is_dir() and other != folder:
                    shutil.rmtree(other, ignore_errors=True)
            return "updated", newest
        except Exception as error:                           # noqa: BLE001  (the net, PyPI, the disk)
            return "failed", (str(error).strip() or type(error).__name__).splitlines()[0][:140]


def due(after: float = DAY) -> bool:
    """With "Keep it current" on: has it been `after` seconds since it was last looked for?"""
    try:
        return bool(wanted()) and time.time() - float(_state().get("checked") or 0) >= after
    except (TypeError, ValueError):
        return bool(wanted())


def keep_current(after: float = DAY) -> bool:
    """Look for a newer one if that is wanted and due. True when one was taken into use."""
    return due(after) and update()[0] == "updated"
