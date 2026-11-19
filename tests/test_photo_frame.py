import asyncio
import json
import time

from PIL import Image

from deephack.modes import photo_frame as pf


def make(folder, names, size=(300, 400)):
    for name in names:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (10, 120, 200)).save(path)


def mode(tmp_path, monkeypatch, **values):
    monkeypatch.setattr(pf, "config_dir", lambda: tmp_path)
    return pf.PhotoFrame({"folder": str(tmp_path / "pics"), **values})


def test_scan_includes_subfolders_and_skips_other_files(tmp_path):
    make(tmp_path, ["b.jpg", "A.PNG", "sub/c.webp", ".hidden/d.jpg", ".e.jpg"])
    (tmp_path / "notes.txt").write_text("x")
    assert [p.relative_to(tmp_path).as_posix() for p in pf.scan(tmp_path)] == ["A.PNG", "b.jpg", "sub/c.webp"]


def test_next_in_order_wraps_and_survives_deleted_files(tmp_path):
    photos = [tmp_path / n for n in ("a.jpg", "c.jpg", "e.jpg")]
    assert pf.next_in_order(photos, None) == photos[0]
    assert pf.next_in_order(photos, photos[0]) == photos[1]
    assert pf.next_in_order(photos, tmp_path / "d.jpg") == photos[2]        # d was deleted: continue after it
    assert pf.next_in_order(photos, photos[2]) == photos[0]


def test_render_fit():
    landscape, portrait = Image.new("RGB", (1600, 1200), "red"), Image.new("RGB", (900, 1350), "blue")
    for photo in (landscape, portrait):
        for fit in ("auto", "fill", "whole"):
            assert pf.render_photo(photo, fit).size == (480, 640)
    # auto shows a landscape photo whole: the corners are the dimmed background, not the photo
    assert pf.render_photo(landscape, "auto").getpixel((5, 5)) != (255, 0, 0)
    assert pf.render_photo(landscape, "fill").getpixel((5, 5)) == (255, 0, 0)


def test_cycles_by_name_and_keeps_the_photo_across_restarts(tmp_path, monkeypatch):
    make(tmp_path / "pics", ["1.jpg", "2.jpg", "3.jpg"])
    m = mode(tmp_path, monkeypatch, order="name", interval="15")
    assert asyncio.run(m.frame()) is not None and m.status == "1.jpg"
    assert asyncio.run(m.frame()) is None                   # not due yet
    state = json.loads((tmp_path / pf.STATE_NAME).read_text())
    assert state["path"].endswith("1.jpg")

    again = mode(tmp_path, monkeypatch, order="name", interval="15", fit="whole")   # e.g. a setting changed
    assert asyncio.run(again.frame()) is not None and again.status == "1.jpg"

    again._since = time.time() - 16 * 60                    # interval elapsed
    assert asyncio.run(again.frame()) is not None and again.status == "2.jpg"


def test_random_order_shows_every_photo_once_per_round(tmp_path, monkeypatch):
    names = [f"{i}.jpg" for i in range(6)]
    make(tmp_path / "pics", names)
    m = mode(tmp_path, monkeypatch)
    seen = []
    for _ in names:
        m._since = 0.0
        assert asyncio.run(m.frame()) is not None
        seen.append(m.status)
    assert sorted(seen) == sorted(names)


def test_skips_unreadable_files_and_reports_empty_folders(tmp_path, monkeypatch):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / "a.jpg").write_bytes(b"not a picture")
    m = mode(tmp_path, monkeypatch, order="name")
    assert asyncio.run(m.frame()) is None and m.placeholder[0] == "No pictures found"
    make(tmp_path / "pics", ["b.jpg"])
    m = mode(tmp_path, monkeypatch, order="name")
    assert asyncio.run(m.frame()) is not None and m.status == "b.jpg"


def test_missing_folder(tmp_path, monkeypatch):
    m = mode(tmp_path, monkeypatch)
    assert asyncio.run(m.frame()) is None and m.placeholder[0] == "Folder not found"
