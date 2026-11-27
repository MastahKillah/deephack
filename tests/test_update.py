"""A newer deephack (update.py): looked for on GitHub, fetched, checked, and put in the old one's place.
GitHub is stood in for, the app's folders are folders made here, and no program is started: what starts
one is handed in."""
import hashlib
import io
import json
import os
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import deephack
from deephack import update

NEWER = "99.1.0"


def packed(top: str = "deephack", extra: dict | None = None, exe: bool = True) -> bytes:
    """A release's zip: the app's folder with its program in it."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        if exe:
            archive.writestr(f"{top}/deephack.exe", "the new program")
        archive.writestr(f"{top}/_internal/base_library.zip", "its files")
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return out.getvalue()


class GitHub:
    """What GitHub says of the newest release, and its files."""

    def __init__(self, version: str = NEWER, pack: bytes | None = None):
        self.version, self.pack = version, packed() if pack is None else pack
        self.sum = hashlib.sha256(self.pack).hexdigest()
        self.asked: list[str] = []
        self.files = update.FILES + f"v{version}/"
        self.name = f"deephack-{version}-windows-x64.zip"

    def release(self) -> dict:
        return {"tag_name": f"v{self.version}", "html_url": f"https://github.com/{update.REPO}/releases/tag/v{self.version}",
                "body": "- **Something new.** It is.\n\n**To install:** download it.\n\n**Full Changelog**: a link",
                "assets": [{"name": self.name, "size": len(self.pack), "browser_download_url": self.files + self.name},
                           {"name": "deephack-1-py3-none-any.whl", "size": 5, "browser_download_url": self.files + "x.whl"},
                           {"name": update.SUMS, "size": 100, "browser_download_url": self.files + update.SUMS}]}

    def fetch(self, url: str, largest: int = 0) -> bytes:
        self.asked.append(url)
        if url == update.FEED:
            return json.dumps(self.release()).encode()
        if url.endswith(update.SUMS):
            return f"{'0' * 64}  some-other-file.whl\r\n{self.sum}  {self.name}\r\n".encode()
        raise OSError(f"no such address: {url}")

    def download(self, url: str, path, size: int, progress=None) -> str:
        self.asked.append(url)
        path.write_bytes(self.pack)
        if progress:
            progress(1.0)
        return hashlib.sha256(self.pack).hexdigest()


@pytest.fixture
def github(monkeypatch, tmp_path):
    there = GitHub()
    monkeypatch.setattr(update, "config_dir", lambda: tmp_path / "settings")
    monkeypatch.setattr(update, "fetch", lambda url, largest=0: there.fetch(url, largest))
    monkeypatch.setattr(update, "download", lambda *a, **k: there.download(*a, **k))
    return there


@pytest.fixture
def app(tmp_path):
    """The installed app's folder, with something of the user's beside it."""
    folder = tmp_path / "programs" / "deephack"
    (folder / "_internal").mkdir(parents=True)
    (folder / "deephack.exe").write_text("the old program")
    (folder / "_internal" / "old.dll").write_text("of the old one")
    return folder


def test_versions_are_compared_as_numbers():
    assert update.number("v0.18.0") == (0, 18, 0) and update.number("0.9.0") < update.number("0.18.0")
    assert update.number("nightly") == () and update.number("") == ()
    assert update.newer(NEWER) and not update.newer(deephack.__version__) and not update.newer("0.0.1")
    assert not update.newer("something else")


def test_the_newest_release_is_read_from_what_github_says(github):
    release = update.latest()
    assert release.version == NEWER and release.zip_name == github.name and release.zip_size == len(github.pack)
    assert release.zip_url.startswith(update.FILES) and release.sums_url.endswith(update.SUMS)
    assert release.notes == "- **Something new.** It is."            # what is for downloading by hand is left out
    assert github.asked == [update.FEED]


def test_files_that_are_not_the_projects_are_not_taken(github, monkeypatch):
    github.files = "https://example.com/elsewhere/"
    release = update.latest()
    assert release.version == NEWER and release.zip_url == "" and release.sums_url == ""
    assert "no packaged app" in update.cannot_install(release) or "Python of your own" in update.cannot_install(release)


def test_a_look_says_newer_current_or_why_not_and_remembers(github, monkeypatch):
    assert update.due() and update.known() is None
    how, release = update.check()
    assert how == "newer" and release.version == NEWER
    assert not update.due() and update.known() == release            # known without asking again
    github.version = deephack.__version__
    assert update.check() == ("current", deephack.__version__) and update.known() is None
    monkeypatch.setattr(update, "fetch", lambda url, largest=0: (_ for _ in ()).throw(OSError("no net")))
    assert update.check() == ("failed", "no net")
    monkeypatch.setattr(update.time, "time", lambda now=update.time.time(): now + update.DAY + 1)
    assert update.due()


def test_the_tray_is_told_a_thing_once(github):
    assert update.tell_once("app", NEWER) and not update.tell_once("app", NEWER)
    assert update.tell_once("firmware", 17) and not update.tell_once("firmware", 17) and not update.tell_once("app", NEWER)
    assert update.tell_once("app", "99.2.0")


def test_a_release_is_fetched_checked_and_unpacked_beside_the_app(github, app):
    seen = []
    new = update.stage(update.latest(), seen.append, folder=app)
    assert new == app.parent / "deephack.new" and (new / "deephack.exe").read_text() == "the new program"
    assert (new / "_internal" / "base_library.zip").exists() and seen == [1.0]
    assert (app / "deephack.exe").read_text() == "the old program"   # the app itself is not touched yet
    assert sorted(p.name for p in app.parent.iterdir()) == ["deephack", "deephack.new"]
    assert not (update.home() / "download.zip").exists()


def test_a_download_that_is_not_what_the_release_names_is_refused(github, app):
    release = update.latest()
    github.pack = packed(extra={"deephack/more.txt": "something else"})
    with pytest.raises(ValueError, match="not what the release says"):
        update.stage(release, folder=app)
    assert [p.name for p in app.parent.iterdir()] == ["deephack"] and not (update.home() / "download.zip").exists()


@pytest.mark.parametrize("pack, why", [
    (packed(extra={"../outside.txt": "x"}), "not laid out"),
    (packed(extra={"elsewhere/file.txt": "x"}), "not laid out"),
    (packed(exe=False), "has no deephack.exe"),
    (b"this is no zip", "no zip"),
])
def test_a_zip_that_is_not_deephacks_is_refused(monkeypatch, tmp_path, app, pack, why):
    there = GitHub(pack=pack)
    monkeypatch.setattr(update, "config_dir", lambda: tmp_path / "settings")
    monkeypatch.setattr(update, "fetch", there.fetch)
    monkeypatch.setattr(update, "download", there.download)
    with pytest.raises(ValueError, match=why):
        update.stage(update.latest(), folder=app)
    assert [p.name for p in app.parent.iterdir()] == ["deephack"] and not (tmp_path / "outside.txt").exists()


def test_a_release_without_a_checksum_for_its_app_is_refused(github, app, monkeypatch):
    release = update.latest()
    monkeypatch.setattr(update, "fetch", lambda url, largest=0: b"0123  another-file.zip\n")
    with pytest.raises(ValueError, match="no checksum"):
        update.stage(release, folder=app)


def test_a_download_is_read_piece_by_piece_and_must_be_as_large_as_named(tmp_path):
    data = os.urandom(700_000)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url, seen = f"http://127.0.0.1:{server.server_port}/file.zip", []
        assert update.download(url, tmp_path / "a", len(data), seen.append) == hashlib.sha256(data).hexdigest()
        assert (tmp_path / "a").read_bytes() == data and seen[-1] == 1.0 and len(seen) >= 2
        with pytest.raises(ValueError, match="larger"):
            update.download(url, tmp_path / "b", len(data) - 1)
        with pytest.raises(ValueError, match="broke off"):
            update.download(url, tmp_path / "c", len(data) + 1)
    finally:
        server.shutdown()


# ---------------------------------------------------------------- the old one's place
class Started:
    """Stands in for a program that was started."""

    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, seconds=None):
        return 0


def test_the_old_app_quits_only_for_a_new_one_that_runs(github, app):
    staged, calls = update.stage(update.latest(), folder=app), []

    def start(exe, arguments):
        calls.append((exe, arguments))
        (update.home() / "ready").write_text("1")            # as the new version does when it runs
        return Started()

    assert update.hand_over(staged, ["--no-device"], folder=app, start=start) == ""
    exe, arguments = calls[0]
    assert exe == staged / "deephack.exe"
    assert arguments == ["--apply-update", str(app), "--wait-pid", str(os.getpid()), "--pass-on", '["--no-device"]']
    assert staged.exists() and update._pending()


def test_a_new_version_that_does_not_start_changes_nothing(github, app):
    staged, started = update.stage(update.latest(), folder=app), Started()
    why = update.hand_over(staged, [], folder=app, start=lambda exe, arguments: started, wait=0.3)
    assert "does not start" in why and started.killed
    assert not staged.exists() and not update._pending() and (app / "deephack.exe").read_text() == "the old program"

    def cannot(exe, arguments):
        raise OSError(5, "Access is denied")

    staged = update.stage(update.latest(), folder=app)
    assert "could not be started" in update.hand_over(staged, [], folder=app, start=cannot) and not staged.exists()


def staged_and_handed_over(app) -> "tuple":
    staged = update.stage(update.latest(), folder=app)
    assert update.hand_over(staged, [], folder=app, start=lambda exe, arguments: (
        (update.home() / "ready").write_text("1"), Started())[1]) == ""
    return staged


def test_the_new_version_takes_the_old_ones_place_and_starts_there(github, app, monkeypatch):
    staged, calls = staged_and_handed_over(app), []

    def start(exe, arguments):
        calls.append((exe, arguments))
        assert exe.read_text() == "the new program"          # in the app's folder, already
        update.arrived()                                     # ... and the app there says that it runs
        return Started()

    monkeypatch.setattr(update, "__version__", NEWER)        # this is the new version at work
    assert update.apply(app, 4321, ["--minimized"], here=staged, start=start, alive=lambda pid: False) == 0
    assert calls == [(app / "deephack.exe", ["--minimized"])]
    assert (app / "_internal" / "base_library.zip").exists() and not (app / "_internal" / "old.dll").exists()
    assert (app.parent / "deephack.old" / "deephack.exe").read_text() == "the old program"
    assert (update.home() / "ready").exists() and not update._pending()
    assert update.outcome() == ("updated", NEWER) and update.outcome() is None       # said once
    update.tidy(app)
    assert [p.name for p in app.parent.iterdir()] == ["deephack"]


def test_it_waits_for_the_old_app_to_be_gone(github, app):
    staged, alive, calls = staged_and_handed_over(app), [True, True, False], []

    def start(exe, arguments):
        calls.append(len(alive))
        update.arrived()
        return Started()

    assert update.apply(app, 1, [], here=staged, start=start, alive=lambda pid: alive.pop(0)) == 0
    assert calls == [0]                                      # started only once the old one was gone

    staged = staged_and_handed_over(app)
    assert update.apply(app, 1, [], here=staged, start=start, alive=lambda pid: True, gone_wait=0.3) == 1
    assert calls == [0] and (app / "deephack.exe").read_text() == "the new program"  # (the app of the first update: untouched)
    assert update.outcome() == ("failed", "the running deephack did not close")


def test_an_app_that_does_not_come_up_in_its_new_place_gives_way_to_the_old_one(github, app):
    staged, calls, new = staged_and_handed_over(app), [], Started()

    def start(exe, arguments):
        calls.append((exe.read_text(), arguments))
        return new                                           # it never says that it runs

    assert update.apply(app, 1, ["--minimized"], here=staged, start=start, alive=lambda pid: False, started_wait=0.3) == 1
    assert new.killed
    assert calls == [("the new program", ["--minimized"]), ("the old program", ["--minimized"])]
    assert (app / "deephack.exe").read_text() == "the old program" and (app / "_internal" / "old.dll").exists()
    assert update.outcome() == ("failed", "the new version did not start")
    update.tidy(app)
    assert [p.name for p in app.parent.iterdir()] == ["deephack"]


def test_a_copy_that_fails_leaves_the_old_app_in_its_place(github, app, monkeypatch):
    staged, calls = staged_and_handed_over(app), []

    def no_room(source, target):
        target.mkdir()
        (target / "half").write_text("a part of it")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(update.shutil, "copytree", no_room)
    assert update.apply(app, 1, [], here=staged, start=lambda exe, arguments: calls.append(exe.read_text()),
                        alive=lambda pid: False) == 1
    assert calls == ["the old program"] and not (app.parent / "deephack.next").exists()
    assert (app / "_internal" / "old.dll").exists() and not (app.parent / "deephack.old").exists()   # never moved
    how, why = update.outcome()
    assert how == "failed" and "could not be put in its place" in why


def test_a_folder_that_cannot_be_moved_aside_keeps_the_old_app(github, app, monkeypatch):
    staged, calls = staged_and_handed_over(app), []

    def held(source, target, seconds=0.0):
        raise OSError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(update, "_rename", held)
    assert update.apply(app, 1, [], here=staged, start=lambda exe, arguments: calls.append(exe.read_text()),
                        alive=lambda pid: False) == 1
    assert calls == ["the old program"] and not (app.parent / "deephack.next").exists()
    how, why = update.outcome()
    assert how == "failed" and "could not be moved aside" in why


def test_nothing_is_cleared_away_while_an_update_is_being_put_in_place(github, app):
    staged = staged_and_handed_over(app)
    update.tidy(app)
    assert staged.exists() and update.outcome() is None
    state = json.loads((update.home() / "state.json").read_text())
    state["pending"]["at"] -= update.PENDING + 1             # ... unless it was given up half way, long ago
    (update.home() / "state.json").write_text(json.dumps(state))
    update.tidy(app)
    assert not staged.exists()


def test_a_deephack_that_runs_from_python_installs_nothing(github):
    assert update.app_folder() is None
    assert "Python of your own" in update.cannot_install(update.latest())
    with pytest.raises(ValueError, match="Python of your own"):
        update.stage(update.latest())
