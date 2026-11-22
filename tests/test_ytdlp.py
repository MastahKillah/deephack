"""A newer yt-dlp, fetched and used instead of deephack's own. PyPI is stood in for, and the yt-dlp that is
"fetched" is a made-up one of a few lines."""
import hashlib
import io
import json
import sys
import time
import zipfile

import pytest

import deephack
from deephack import ytdlp
from deephack.modes import video

BUILT_IN = "2026.10.26"


def wheel(version: str, body: str = "class YoutubeDL:\n    made_up = True\n", extra: dict | None = None) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", "from . import version\n" + body)
        archive.writestr("yt_dlp/version.py", f"__version__ = {version!r}\n")
        archive.writestr(f"yt_dlp-{version}.dist-info/METADATA", "Name: yt-dlp\n")
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return data.getvalue()


class PyPI:
    """Stands in for the net: the list of what there is, and the file."""

    def __init__(self, version: str, data: bytes | None = None, python: str = ">=3.10"):
        self.version, self.data, self.python = version, data if data is not None else wheel(version), python
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.asked: list[str] = []
        self.down = False

    def fetch(self, url: str) -> bytes:
        self.asked.append(url)
        if self.down:
            raise OSError("no net")
        if url == ytdlp.INDEX:
            name = f"yt_dlp-{self.version}-py3-none-any.whl"
            return json.dumps({"info": {"version": self.version, "requires_python": self.python}, "urls": [
                {"packagetype": "sdist", "filename": f"yt_dlp-{self.version}.tar.gz", "url": "https://files/sdist",
                 "size": 1, "digests": {"sha256": "0"}},
                {"packagetype": "bdist_wheel", "filename": name, "url": "https://files/" + name, "size": len(self.data),
                 "digests": {"sha256": self.sha256}}]}).encode()
        return self.data


@pytest.fixture
def own(tmp_path, monkeypatch):
    """deephack's own yt-dlp stood in for by a made-up one, the settings folder an empty one, and
    whatever the test loads forgotten afterwards."""
    before = {name: module for name, module in sys.modules.items() if name == "yt_dlp" or name.startswith("yt_dlp.")}
    ytdlp._forget()
    built_in = tmp_path / "built-in"
    with zipfile.ZipFile(io.BytesIO(wheel(BUILT_IN, "class YoutubeDL:\n    made_up = False\n"))) as archive:
        archive.extractall(built_in)
    monkeypatch.syspath_prepend(str(built_in))
    monkeypatch.setattr(ytdlp, "config_dir", lambda: tmp_path / "settings")
    monkeypatch.setattr(ytdlp, "_loaded_from", None)
    monkeypatch.setattr(ytdlp, "wanted", lambda: False)
    yield tmp_path
    ytdlp._forget()
    sys.modules.update(before)


def use(monkeypatch, pypi: PyPI) -> PyPI:
    monkeypatch.setattr(ytdlp, "fetch", pypi.fetch)
    return pypi


def test_deephacks_own_is_used_when_nothing_was_fetched(own):
    module = ytdlp.module()
    assert module.version.__version__ == BUILT_IN and module.YoutubeDL.made_up is False
    assert ytdlp.module() is module and ytdlp.version() == (BUILT_IN, False)
    assert ytdlp.fetched() is None and not (own / "settings" / "yt-dlp").exists()        # nothing was written


def test_a_newer_one_is_fetched_checked_and_used(own, monkeypatch):
    ytdlp.module()
    pypi = use(monkeypatch, PyPI("2027.1.15"))
    assert ytdlp.update() == ("updated", "2027.1.15")
    assert pypi.asked == [ytdlp.INDEX, "https://files/yt_dlp-2027.1.15-py3-none-any.whl"]
    module = ytdlp.module()
    assert module.version.__version__ == "2027.1.15" and module.YoutubeDL.made_up is True
    home = own / "settings" / "yt-dlp"
    assert (home / "2027.1.15" / "yt_dlp" / "__init__.py").is_file()
    assert not (home / "2027.1.15" / "yt_dlp-2027.1.15.dist-info").exists()             # only the program itself
    assert ytdlp.version() == ("2027.1.15", True)
    assert str(home / "2027.1.15") not in sys.path                                       # nothing left in the way of others

    pypi.asked.clear()
    assert ytdlp.update() == ("current", "2027.1.15") and pypi.asked == [ytdlp.INDEX]    # nothing newer: nothing fetched

    newer = use(monkeypatch, PyPI("2027.2.1"))                                           # and the one after that
    assert ytdlp.update() == ("updated", "2027.2.1") and len(newer.asked) == 2
    assert ytdlp.module().version.__version__ == "2027.2.1"
    assert sorted(p.name for p in home.iterdir()) == ["2027.2.1", "state.json"]          # the one before is gone


def test_after_a_restart_the_fetched_one_is_used_again(own, monkeypatch):
    use(monkeypatch, PyPI("2027.1.15"))
    assert ytdlp.update()[0] == "updated"
    ytdlp._forget()
    monkeypatch.setattr(ytdlp, "_loaded_from", None)                                     # as a new start of the app
    assert ytdlp.module().version.__version__ == "2027.1.15"


def test_what_is_not_newer_is_left_alone(own, monkeypatch):
    for version in (BUILT_IN, "2025.12.31"):
        pypi = use(monkeypatch, PyPI(version))
        assert ytdlp.update() == ("current", BUILT_IN) and pypi.asked == [ytdlp.INDEX]
    assert ytdlp.number("2026.10.26") < ytdlp.number("2026.11.2") < ytdlp.number("2026.11.2.232901")


def test_a_download_that_is_not_what_pypi_names_is_refused(own, monkeypatch):
    pypi = use(monkeypatch, PyPI("2027.1.15"))
    pypi.sha256 = "0" * 64                                   # the file is another than the list says
    assert ytdlp.update() == ("failed", "the download is not what PyPI says it should be")
    assert ytdlp.fetched() is None and ytdlp.module().version.__version__ == BUILT_IN
    cut = use(monkeypatch, PyPI("2027.1.15"))
    cut.data = cut.data[:-20]                                # ... or it broke off
    assert ytdlp.update()[0] == "failed" and ytdlp.fetched() is None


def test_one_that_does_not_work_is_thrown_away(own, monkeypatch):
    use(monkeypatch, PyPI("2027.1.15", wheel("2027.1.15", "this is not Python\n")))
    how, why = ytdlp.update()
    assert how == "failed" and "does not work here" in why
    home = own / "settings" / "yt-dlp"
    assert not (home / "2027.1.15").exists() and ytdlp.fetched() is None
    assert ytdlp.module().YoutubeDL.made_up is False         # deephack's own is back
    use(monkeypatch, PyPI("2027.1.16", wheel("2027.1.15")))  # says it is one version and is another
    assert ytdlp.update()[0] == "failed" and ytdlp.module().version.__version__ == BUILT_IN
    use(monkeypatch, PyPI("2027.1.17", wheel("2027.1.17", "x = 1\n")))      # loads, but is not yt-dlp
    assert ytdlp.update()[0] == "failed" and ytdlp.module().version.__version__ == BUILT_IN


def test_only_the_program_is_unpacked_and_nothing_outside_its_folder(own, monkeypatch):
    bad = wheel("2027.1.15", extra={"../outside.py": "x = 1\n", "yt_dlp/../../also.py": "x = 1\n", "other/thing.py": "x = 1\n"})
    use(monkeypatch, PyPI("2027.1.15", bad))
    assert ytdlp.update() == ("updated", "2027.1.15")
    home = own / "settings" / "yt-dlp"
    assert not list(own.rglob("outside.py")) and not list(own.rglob("also.py")) and not (home / "2027.1.15" / "other").exists()


def test_one_that_needs_a_newer_python_is_not_fetched(own, monkeypatch):
    pypi = use(monkeypatch, PyPI("2027.1.15", python=">=3.99"))
    how, why = ytdlp.update()
    assert how == "failed" and "newer Python" in why and pypi.asked == [ytdlp.INDEX]
    assert ytdlp._python_will_do(None) and ytdlp._python_will_do(">=3.8") and ytdlp._python_will_do("something else")


def test_no_net_is_an_answer_not_a_crash(own, monkeypatch):
    pypi = use(monkeypatch, PyPI("2027.1.15"))
    pypi.down = True
    assert ytdlp.update() == ("failed", "no net")
    assert ytdlp.module().version.__version__ == BUILT_IN


def test_a_copy_fetched_by_another_deephack_is_not_used(own, monkeypatch):
    use(monkeypatch, PyPI("2027.1.15"))
    assert ytdlp.update()[0] == "updated" and ytdlp.module().YoutubeDL.made_up is True
    monkeypatch.setattr(ytdlp, "__version__", deephack.__version__ + ".1")              # deephack itself was updated
    assert ytdlp.fetched() is None
    assert ytdlp.module().version.__version__ == BUILT_IN and ytdlp.version() == (BUILT_IN, False)


def test_it_is_looked_for_by_itself_only_when_that_is_switched_on(own, monkeypatch):
    pypi = use(monkeypatch, PyPI("2027.1.15"))
    assert not ytdlp.due() and ytdlp.keep_current() is False and pypi.asked == []       # off: never
    monkeypatch.setattr(ytdlp, "wanted", lambda: True)
    assert ytdlp.due() and ytdlp.keep_current() is True                                  # on: now
    assert ytdlp.module().version.__version__ == "2027.1.15"
    pypi.asked.clear()
    assert not ytdlp.due() and ytdlp.keep_current() is False and pypi.asked == []       # and not again today
    now = time.time()
    monkeypatch.setattr(ytdlp.time, "time", lambda: now + ytdlp.HOUR + 1)
    assert not ytdlp.due() and ytdlp.due(ytdlp.HOUR)                                     # a link that will not open: after an hour
    monkeypatch.setattr(ytdlp.time, "time", lambda: now + ytdlp.DAY + 1)
    assert ytdlp.due() and ytdlp.keep_current() is False and pypi.asked == [ytdlp.INDEX]     # tomorrow: asked, nothing newer


def test_a_link_that_will_not_open_gets_the_newer_one_and_another_try(own, monkeypatch):
    old = "class YoutubeDL:\n    def __init__(self, options): pass\n    def __enter__(self): return self\n" \
          "    def __exit__(self, *a): pass\n    def extract_info(self, source, download):\n" \
          "        raise RuntimeError('ERROR: [youtube] abc: YouTube said no')\n"
    new = old.replace("raise RuntimeError('ERROR: [youtube] abc: YouTube said no')",
                      "return {'url': 'https://stream/' + source[-3:], 'title': 'A film', 'vcodec': 'avc1'}")
    ytdlp._forget()
    (own / "built-in" / "yt_dlp" / "__init__.py").write_text("from . import version\n" + old, encoding="utf8")
    pypi = use(monkeypatch, PyPI("2027.1.15", wheel("2027.1.15", new)))

    with pytest.raises(video.LinkError, match="abc: YouTube said no"):                   # switched off: it is said, no more
        video.resolve("https://www.youtube.com/watch?v=abc")
    assert pypi.asked == []

    monkeypatch.setattr(ytdlp, "wanted", lambda: True)
    ytdlp._save(checked=time.time() - 2 * ytdlp.HOUR)                                    # looked two hours ago: not due for the day
    address, options, title, sound = video.resolve("https://www.youtube.com/watch?v=abc")
    assert (address, title, sound) == ("https://stream/abc", "A film", None) and len(pypi.asked) == 2

    player = video.Player("https://www.youtube.com/watch?v=abc", resolve=lambda source: (_ for _ in ()).throw(video.LinkError("no")))
    assert player.link_failed is False


def test_the_packaged_app_can_see_that_yt_dlp_is_needed():
    """PyInstaller packs what it sees imported. When yt-dlp was only asked for by its name as a text,
    deephack.exe was built without it, started fine, and could not play one link."""
    from pathlib import Path

    root = Path(deephack.__file__).parent
    assert "\n    import yt_dlp\n" in (root / "ytdlp.py").read_text(encoding="utf8")
    spec = root.parent / "packaging" / "deephack.spec"
    if spec.is_file():                                       # not in an installed copy
        assert 'hidden += ["yt_dlp"]' in spec.read_text(encoding="utf8")
