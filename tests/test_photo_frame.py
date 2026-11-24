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


# ---- on a cooler that draws from USB: short intervals, and one photo fades into the next
def drawing(tmp_path, monkeypatch, **values):
    """Two photos, red and blue, by name; a mode on a cooler that draws; and the clock its fades go by."""
    for name, colour in (("1.png", (200, 0, 0)), ("2.png", (0, 0, 200))):
        (tmp_path / "pics").mkdir(exist_ok=True)
        Image.new("RGB", (480, 640), colour).save(tmp_path / "pics" / name)
    m = mode(tmp_path, monkeypatch, order="name", fit="fill", **{"interval": "0.25", **values})
    m.direct, clock = True, [100.0]
    m._clock = lambda: clock[0]
    return m, clock


def test_one_photo_fades_into_the_next_on_a_cooler_that_draws(tmp_path, monkeypatch):
    m, clock = drawing(tmp_path, monkeypatch)
    first = asyncio.run(m.frame())
    assert first.getpixel((240, 320)) == (200, 0, 0) and m.tick is None and m.quality is None
    m._since = time.time() - 10
    assert asyncio.run(m.frame()) is None                    # 15 seconds are not over
    m._since, m.sent_bytes = time.time() - 16, 90_000        # (the first photo was 90 KB as a still: that says
    begun = asyncio.run(m.frame())                           # nothing about a fade's pictures)
    assert m.status == "2.png" and begun.getpixel((240, 320)) == (200, 0, 0)
    assert m.tick == pf.FADE_TICK and m.quality == pf.FADE_QUALITY
    reds = []
    for at in (0.1, 0.3, 0.5, 0.7, 0.9):
        clock[0] = 100.0 + at * pf.FADE
        r, g, b = asyncio.run(m.frame()).getpixel((240, 320))
        assert abs(r + b - 200) <= 1 and g == 0              # a mix of the two
        reds.append(r)
    assert reds == sorted(reds, reverse=True) and reds[0] > 190 and abs(reds[2] - 100) <= 1 and reds[4] < 10
    assert reds[0] - reds[1] < reds[1] - reds[2]             # it starts slowly
    clock[0] = 100.0 + pf.FADE
    last = asyncio.run(m.frame())                            # the new photo itself, as a still again
    assert last.getpixel((240, 320)) == (0, 0, 200) and m.tick is None and m.quality is None
    assert asyncio.run(m.frame()) is None


def test_a_fade_is_no_longer_for_pictures_the_cooler_was_too_busy_for_and_its_pictures_fit_the_bus(tmp_path, monkeypatch):
    m, clock = drawing(tmp_path, monkeypatch)
    asyncio.run(m.frame())
    m._since = time.time() - 16
    asyncio.run(m.frame())
    m.sent_bytes, m.fast, m.turn = 60_000, True, 0.018       # the engine says how large the last one was as a JPEG
    clock[0] += 0.1
    asyncio.run(m.frame())
    assert m.quality < pf.FADE_QUALITY                       # too large to take 27 a second: less sharp, not fewer
    m.sent_bytes = 12_000
    clock[0] += 0.1
    lower = m.quality
    asyncio.run(m.frame())
    assert m.quality == lower + 1                            # and sharper again where there is room
    clock[0] += 5.0                                          # nothing was asked for five seconds:
    assert asyncio.run(m.frame()).getpixel((240, 320)) == (0, 0, 200) and m.tick is None     # the fade is over


def test_a_cooler_that_stores_each_photo_gets_one_every_15_minutes_at_the_soonest_and_no_fade(tmp_path, monkeypatch):
    m, clock = drawing(tmp_path, monkeypatch)
    m.direct = False
    assert asyncio.run(m.frame()).getpixel((240, 320)) == (200, 0, 0)
    m._since = time.time() - 14 * 60
    assert asyncio.run(m.frame()) is None                    # the 15 seconds asked for are 15 minutes here
    m._since = time.time() - 16 * 60
    assert asyncio.run(m.frame()).getpixel((240, 320)) == (0, 0, 200) and m.tick is None and m._fade is None


def test_a_cut_can_be_asked_for_and_a_fade_ends_when_the_cooler_stops_drawing(tmp_path, monkeypatch):
    m, clock = drawing(tmp_path, monkeypatch, transition="cut")
    asyncio.run(m.frame())
    m._since = time.time() - 16
    assert asyncio.run(m.frame()).getpixel((240, 320)) == (0, 0, 200) and m.tick is None

    (tmp_path / pf.STATE_NAME).unlink()                      # (begin with the first photo again)
    m, clock = drawing(tmp_path, monkeypatch)
    asyncio.run(m.frame())
    m._since = time.time() - 16
    asyncio.run(m.frame())
    clock[0] += 0.2
    m.direct = False                                         # a slide show was switched on, say
    assert asyncio.run(m.frame()).getpixel((240, 320)) == (0, 0, 200) and m.tick is None and m._fade is None


def test_the_folder_is_not_walked_for_every_photo(tmp_path, monkeypatch):
    m, clock = drawing(tmp_path, monkeypatch, transition="cut")
    walks, real = [], pf.scan
    monkeypatch.setattr(pf, "scan", lambda folder: walks.append(folder) or real(folder))
    for _ in range(4):
        m._since = 0.0
        assert asyncio.run(m.frame()) is not None
    assert len(walks) == 1
    m._scanned = (m._scanned[0], m._scanned[1] - pf.RESCAN, m._scanned[2])      # a minute later
    m._since = 0.0
    asyncio.run(m.frame())
    assert len(walks) == 2


def test_missing_folder(tmp_path, monkeypatch):
    m = mode(tmp_path, monkeypatch)
    assert asyncio.run(m.frame()) is None and m.placeholder[0] == "Folder not found"
