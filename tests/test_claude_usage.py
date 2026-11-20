import json
import time

from deephack.modes import claude_usage as cu

START = "2026-11-18T00:00:00.000Z"


def entry(mid, out, ts="2026-11-18T10:00:00.000Z", model="claude-opus-5-5", session="s1"):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": session,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}},
    }).encode()


def test_day_deduplicates_by_message_id_and_ignores_old_or_synthetic():
    day = cu.Day(START)
    day.add_line(entry("m1", 5))
    day.add_line(entry("m1", 50))                      # same response logged again with the final count
    day.add_line(entry("m2", 7, session="s2", model="claude-fable-5-2"))
    day.add_line(entry("old", 999, ts="2026-11-17T23:59:59.000Z"))
    day.add_line(entry("syn", 999, model="<synthetic>"))
    day.add_line(b'{"type":"user","timestamp":"2026-11-18T10:00:00.000Z"}')
    day.add_line(b"not json but mentions \"usage\" and \"assistant\"")
    t = day.totals()
    assert t["requests"] == 2 and t["output"] == 57 and t["input"] == 20
    assert t["cache_read"] == 2000 and t["cache_write"] == 200 and t["sessions"] == 2
    assert t["models"] == {"claude-opus-5-5": 50, "claude-fable-5-2": 7}
    assert sum(t["hours"]) == 57


def test_refresh_reads_tail_then_only_new_lines(tmp_path):
    log = tmp_path / "proj" / "a.jsonl"
    log.parent.mkdir()
    log.write_bytes(entry("old", 1, ts="2026-11-17T08:00:00.000Z") + b"\n" + entry("m1", 5) + b"\n")
    day = cu.Day(START)
    day.refresh(tmp_path, 0)
    assert day.totals()["output"] == 5
    with open(log, "ab") as fh:
        fh.write(entry("m2", 7) + b"\n" + entry("m3", 100)[:40])    # last line still being written
    day.refresh(tmp_path, 0)
    assert day.totals()["output"] == 12
    with open(log, "ab") as fh:
        fh.write(entry("m3", 100)[40:] + b"\n")
    day.refresh(tmp_path, 0)
    assert day.totals()["output"] == 112


def test_read_limits(tmp_path):
    now = time.time()
    future = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now + 3600))
    past = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 3600))
    p = tmp_path / "usage.json"
    p.write_text(json.dumps({"five_hour": {"used_percentage": None, "resets_at": None},
                             "seven_day": {"used_percentage": 27.4, "resets_at": future}}))
    limits = cu.read_limits(p, now)
    assert "five_hour" not in limits and limits["seven_day"][0] == 27 and abs(limits["seven_day"][1] - (now + 3600)) < 2
    p.write_text(json.dumps({"five_hour": {"used_percentage": 80, "resets_at": past}}))
    assert cu.read_limits(p, now) == {"five_hour": (0, None)}     # window already rolled over
    assert cu.read_limits(tmp_path / "missing.json") is None


def test_limits_are_the_highest_of_the_newest_window(tmp_path, monkeypatch):
    now = 1_000_000.0
    reset, later = now + 3600, now + 3600 + 7 * 86400
    limits = cu.Limits()
    seen = [limits.update({"seven_day": (pct, reset)}, now)["seven_day"][0] for pct in (56, 65, 51, 63, 52, 59, 60)]
    assert seen == [56, 65, 65, 65, 65, 65, 65]                       # seven sessions, each with its own number
    assert limits.update(None, now) == {"seven_day": (65, reset)}     # the file was being written just then
    assert limits.update({"seven_day": (0, None)}, now) == {"seven_day": (65, reset)}    # a session from last week
    assert limits.update({"seven_day": (66, reset + 30)}, now) == {"seven_day": (66, reset)}
    assert limits.update({"seven_day": (3, later)}, now) == {"seven_day": (3, later)}    # a new window
    assert limits.update({"seven_day": (70, reset)}, now) == {"seven_day": (3, later)}   # ... and a session from the old one
    assert limits.update({"five_hour": (40, now + 60), "seven_day": (3, later)}, now)["five_hour"] == (40, now + 60)
    assert limits.update({"seven_day": (4, later)}, now + 61) == {"seven_day": (4, later)}    # its window ran out
    # without reset times there is nothing to go by but the latest
    limits = cu.Limits()
    assert [limits.update({"seven_day": (pct, None)}, now) for pct in (40, 43, 41)][-1] == {"seven_day": (41, None)}
    assert limits.update(None, now) is None

    # reading: a look that lasts while the sessions take turns
    p = tmp_path / "usage.json"
    turns = iter((51, 63, 65, 52, 59))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 3600))

    def write(seconds=0):
        p.write_text(json.dumps({"seven_day": {"used_percentage": next(turns, 56), "resets_at": stamp}}))

    monkeypatch.setattr(cu.time, "sleep", write)
    write()
    assert cu.Limits().read(p, watch=0.0)["seven_day"][0] == 51
    assert cu.Limits().read(p, watch=0.05)["seven_day"][0] == 65
    assert cu.Limits().read(tmp_path / "missing.json", watch=5.0) is None      # nobody writes: no waiting


def test_formatting():
    assert [cu.compact(n) for n in (0, 999, 1500, 2_323_932, 980_179_058, 3_000_000_000)] == ["0", "999", "1.5K", "2.3M", "980M", "3B"]
    assert cu.model_name("claude-opus-5-5") == "Opus 5.5"
    assert cu.model_name("claude-haiku-4-5-20251001") == "Haiku 4.5"


def test_render_handles_empty_day():
    img = cu.render_usage(None, cu.Day(START).totals())
    assert img.size == (480, 640)
