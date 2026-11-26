import asyncio
import json
import os
import time

import pytest

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


# ------------------------------------------------------------------ the live page
def test_what_burns_is_every_token_of_the_last_minute_and_the_minutes_before_it():
    day = cu.Day(START)
    now = 1_795_000_000.0

    def at(seconds_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - seconds_ago))

    day.start_utc = at(86400)
    day.add_line(entry("a", 100, ts=at(5)))                    # each: 10 in, 1000 read, 100 written, and its output
    day.add_line(entry("b", 300, ts=at(50)))
    day.add_line(entry("c", 7, ts=at(70)))
    day.add_line(entry("d", 9, ts=at(29 * 60 + 30)))
    day.add_line(entry("old", 999, ts=at(31 * 60)))            # before the half hour
    day.add_line(entry("ahead", 999, ts=at(-30)))              # a clock that is ahead: not counted yet
    trail, out = day.recent(now, 30)
    assert len(trail) == 30 and trail[-1] == 2 * 1110 + 400 and out == 400
    assert trail[-2] == 1117 and trail[0] == 1119 and sum(trail[1:-2]) == 0


def test_a_log_belongs_to_its_session_and_says_where_it_works(tmp_path):
    main = tmp_path / "D--Projects-orchard" / "s1.jsonl"
    agent = tmp_path / "D--Projects-orchard" / "s1" / "subagents" / "agent-a1.jsonl"
    assert cu.session_of(main) == "s1" and cu.session_of(agent) == "s1"
    agent.parent.mkdir(parents=True)
    line = json.loads(entry("m1", 5))
    line["cwd"] = "D:\\Projects\\orchard\\"
    main.write_bytes(json.dumps(line).encode() + b"\n")
    agent.write_bytes(entry("m2", 7) + b"\n")
    day = cu.Day(START)
    day.refresh(tmp_path, 0)
    assert day.names == {"s1": "orchard"} and list(day.written) == ["s1"] and day.totals()["output"] == 12
    now = day.written["s1"]
    assert day.sessions(now + 5) == [cu.Session("s1", "orchard", "busy", now)]      # written a moment ago: busy
    assert day.sessions(now + cu.WRITING + 1) == []


def record(folder, pid, **fields):
    fields = {"pid": pid, "sessionId": f"id{pid}", "name": f"name{pid}", "procStart": str(1000 * pid), **fields}
    (folder / f"{pid}.json").write_text(json.dumps(fields), encoding="utf8")


def test_sessions_are_read_from_claude_codes_own_record(tmp_path):
    assert cu.read_sessions(tmp_path / "none") is None                               # an older Claude Code
    record(tmp_path, 1, status="idle", statusUpdatedAt=5_000_000)
    record(tmp_path, 2, status="busy", statusUpdatedAt=9_000_000)
    record(tmp_path, 3, status="waiting", statusUpdatedAt=7_000_000, waitingFor="input needed")
    record(tmp_path, 4, status="busy", statusUpdatedAt=8_000_000, waitingFor="left over")
    record(tmp_path, 5, status="waiting", statusUpdatedAt=7_500_000)
    record(tmp_path, 6, status="busy")                                               # killed: its process is gone
    record(tmp_path, 7, status="busy")                                               # ... or is another program by now
    record(tmp_path, 8, status="dreaming", updatedAt=1_000_000)                      # a state this does not know
    (tmp_path / "9.json").write_text("{ not json", encoding="utf8")
    (tmp_path / "10.json").write_text(json.dumps({"status": "busy"}), encoding="utf8")
    (tmp_path / "1.abc.key").write_text("secret", encoding="utf8")                   # never opened
    del_start = json.loads((tmp_path / "2.json").read_text(encoding="utf8"))
    del del_start["procStart"]                                                       # no word on when it began
    (tmp_path / "2.json").write_text(json.dumps(del_start), encoding="utf8")
    running = {1: 1000, 2: 123, 3: 3000, 4: 4000 + cu.SAME_START, 5: 5000, 7: 7000 + cu.SAME_START + 1, 8: 8000}
    got = cu.read_sessions(tmp_path, running.get)
    assert [(s.name, s.state, s.since, s.why) for s in got] == [
        ("name4", "busy", 8000.0, ""), ("name2", "busy", 9000.0, ""),                # the busy ones, the longest first
        ("name5", "waiting", 7500.0, ""), ("name3", "waiting", 7000.0, "input needed"),      # the waiting, the newest first
        ("name8", "idle", 1000.0, ""), ("name1", "idle", 5000.0, "")]
    assert cu.process_started(os.getpid()) is not None and cu.process_started(0x7FFFFFF0) is None


def test_a_number_counts_up_to_where_it_is_going():
    roll = cu.Rolling(pace=1.0, falls=False)
    assert roll.step(1.0) is False and roll.value is None                            # nothing yet
    roll.to(1000)
    assert roll.value == 1000 and roll.step(1.0) is False                            # the first one is simply there
    roll.to(2000)
    assert roll.step(1.0) is True and 1600 < roll.value < 1650                       # most of the way in a second
    seen = [roll.value]
    while roll.step(0.125):
        seen.append(roll.value)
    assert roll.value == 2000 and seen == sorted(seen) and 8 < len(seen) <= 24      # there within four seconds in all
    roll.to(10)                                                                      # a new day: a count begins anew
    assert roll.value == 10 and roll.step(1.0) is False
    rate = cu.Rolling(pace=1.0)
    rate.to(100)
    rate.to(0)                                                                       # what burns comes down as it went up
    assert rate.step(1.0) is True and 30 < rate.value < 40


def test_live_words_and_sizes():
    assert [cu.clock_span(s) for s in (0, 7, 151, 3725, -3)] == ["0:00", "0:07", "2:31", "1:02:05", "0:00"]
    assert [cu.ago(s) for s in (5, 60, 3599, 7200)] == ["a moment", "1 min", "59 min", "2 h"]
    assert [cu.flame_level(n) for n in (0, 1, 99_999, 100_000, 999_999, 1_000_000, 50_000_000)] == [0, 1, 1, 2, 3, 4, 5]
    assert cu._flame(5, 0).size == (56, 72) and cu._flame(5, 0).tobytes() != cu._flame(5, 3).tobytes()     # it flickers
    assert cu._flame(0, 0).tobytes() == cu._flame(0, 3).tobytes()                    # what is left of it does not
    assert cu._spark(0).tobytes() != cu._spark(5).tobytes() and cu._dot(cu.ACCENT, 1).tobytes() != cu._dot(cu.ACCENT, None).tobytes()


def test_the_live_page_is_drawn_whatever_there_is():
    now = 1_795_000_000.0
    totals = cu.Day(START).totals()
    sessions = [cu.Session(str(i), "a-long-name-for-a-session-" * 3, "busy", now - 100 * i) for i in range(7)]
    sessions += [cu.Session("w", "kiln", "waiting", now - 30, "input needed"), cu.Session("i", "lantern", "idle", now)]
    both = {"five_hour": (43, now + 3600), "seven_day": (90, None)}
    empty = cu.render_live(None, totals, [], [], {}, now, 0.0)
    assert empty.size == (480, 640)
    full = cu.render_live(both, totals, sessions, [5] * 30, {"burn": 4_000_000, "burn_out": 900, "five_hour": 42.5}, now, 1.0, now - 5)
    assert full.size == (480, 640) and full.tobytes() != empty.tobytes()
    later = cu.render_live(both, totals, sessions, [5] * 30, {"burn": 4_000_000, "burn_out": 900, "five_hour": 42.5}, now, 1.0 + cu.FRAME, now - 5)
    assert later.tobytes() != full.tobytes()                                         # the flame, the spark, the marks
    idle = [cu.Session("i", "lantern", "idle", now)]
    quiet = cu.render_live(both, totals, idle, [0] * 30, {}, now, 1.0, now - 900)
    assert quiet.tobytes() == cu.render_live(both, totals, idle, [0] * 30, {}, now, 9.0, now - 900).tobytes()    # nothing moves
    assert cu.render_live({"seven_day": (10, None)}, totals, sessions, [0, 9], {}, now, 0.0).size == (480, 640)


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A Claude Code folder of its own, with one session in it, and the mode looking at it as on a drawn cooler."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cu, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cu, "process_started", lambda pid: 1000 * pid)
    (tmp_path / "projects" / "p").mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    mode = cu.ClaudeUsage()
    mode.direct = True
    mode.log = tmp_path / "projects" / "p" / "s1.jsonl"
    mode.said = lambda mid, out, ago=1.0: open(mode.log, "ab").write(entry(mid, out, ts=time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))) + b"\n")
    mode.state = lambda state, **more: record(tmp_path / "sessions", 1, status=state, statusUpdatedAt=1000 * (time.time() - 20), **more)
    mode.again = lambda: setattr(mode, "_look_at", 0.0) or setattr(mode, "_logs_at", 0.0)
    return mode


def test_the_mode_counts_and_burns_while_a_session_works(claude):
    mode = claude
    mode.said("m1", 500)
    mode.state("busy")
    first = asyncio.run(mode.frame())
    assert first is not None and first.size == (480, 640)
    assert mode.tick == cu.FRAME and "1 working" in mode.status and "tokens a minute" in mode.status
    limits, totals, sessions, trail, last = mode._live
    assert totals["output"] == 500 and [s.state for s in sessions] == ["busy"] and trail[-1] == 1610 and last > 0
    assert mode._rolls["output"].value == 500 and mode._rolls["burn"].value == 1610
    assert asyncio.run(mode.frame()) is not None                                     # a picture at every call: it turns
    mode.said("m2", 2500)                                                            # the next request is logged
    mode.again()
    assert asyncio.run(mode.frame()) is not None
    assert mode._live[1]["output"] == 3000 and mode._rolls["output"].target == 3000
    mode._stepped -= 0.5
    asyncio.run(mode.frame())
    assert 500 < mode._rolls["output"].value < 3000 and 1 < mode._rolls["requests"].value < 2      # on its way
    for _ in range(200):
        mode._stepped -= 0.125
        asyncio.run(mode.frame())
    assert mode._rolls["output"].value == 3000 and mode._rolls["burn"].value == 2 * 1110 + 3000


def test_the_mode_stands_still_when_nobody_works(claude):
    mode = claude
    mode.said("m1", 500, ago=90)
    mode.state("idle")
    assert asyncio.run(mode.frame()) is not None and mode.tick is None
    assert "working" not in mode.status and "tokens a minute" not in mode.status and "500 output tokens today" in mode.status
    assert asyncio.run(mode.frame()) is None                                         # nothing changed: nothing drawn
    mode.again()
    assert asyncio.run(mode.frame()) is None
    mode.state("waiting", waitingFor="input needed")                                 # it asks its user something
    mode.again()
    assert asyncio.run(mode.frame()) is not None and mode.tick is None
    assert [(s.state, s.why) for s in mode._live[2]] == [("waiting", "input needed")]
    mode.state("busy")                                                               # ... and goes on
    mode.again()
    assert asyncio.run(mode.frame()) is not None and mode.tick == cu.FRAME


def test_the_mode_without_claude_codes_record_of_sessions_goes_by_the_logs(claude, tmp_path):
    mode = claude
    (tmp_path / "sessions").rmdir()
    mode.said("m1", 500)
    assert asyncio.run(mode.frame()) is not None
    assert [(s.id, s.state) for s in mode._live[2]] == [("s1", "busy")] and mode.tick == cu.FRAME
    old = time.time() - cu.WRITING - 5
    os.utime(mode.log, (old, old))
    mode._day.written.clear()
    mode.again()
    asyncio.run(mode.frame())
    assert mode._live[2] == []


def test_the_mode_on_a_cooler_that_stores_is_as_it_was(claude):
    mode = claude
    mode.direct = False
    mode.said("m1", 500)
    mode.state("busy")
    picture = asyncio.run(mode.frame())
    assert picture is not None and mode.tick is None and mode._live is None
    assert mode.status == "500 output tokens today" and asyncio.run(mode.frame()) is None


# ---- the pump head's lights (issue #57)
def _steady() -> float:
    return 1.0                                                            # no flicker


def test_the_lights_are_a_fire_while_a_session_works_and_amber_while_one_waits():
    now = time.time()
    busy, waiting, idle = (cu.Session(state, state, state, now) for state in ("busy", "waiting", "idle"))
    assert cu.live_lights([idle], 5e6, 0.0) is None and cu.live_lights([], 0, 0.0) is None       # the page's own edge
    thinking = cu.live_lights([busy], 0, 0.0, _steady)                    # works, and nothing has burnt this minute
    assert len(thinking) == 14 and max(thinking[0]) == 255 and thinking[1:13] == [(0, 0, 0)] * 12 and thinking[13] == thinking[0]
    some = cu.live_lights([busy], 50_000, 0.0, _steady)                   # the flame's first size: two lights and a bit
    assert [max(c) > 0 for c in some[:7]] == [True, True, True, False, False, False, False]
    most = cu.live_lights([busy], 5e6, 0.0, _steady)
    assert all(max(c) == 255 for c in most) and most[0][1] < most[6][1]   # all seven: red below, yellow above
    assert most[:7] == most[13:6:-1]                                      # the same up both sides
    flickering = cu.live_lights([busy], 5e6, 0.0, lambda: 0.0)
    assert all(140 < max(c) < 160 for c in flickering)                    # never under six tenths

    asked = cu.live_lights([waiting, idle], 0, 0.0)
    assert len(set(asked)) == 1 and asked[0][0] > asked[0][1] > asked[0][2]              # amber, all of them
    high, low = cu.live_lights([waiting], 0, cu.LIGHT_BREATH / 4), cu.live_lights([waiting], 0, 3 * cu.LIGHT_BREATH / 4)
    assert high[0][0] > 1.8 * low[0][0] and low[0][0] > 40                # it breathes, and never goes out
    both = cu.live_lights([busy, waiting], 5e6, 0.0, _steady)
    assert both[5] == both[6] == asked[0] and both[:5] == most[:5]        # the top two say "waiting" over the fire


def test_the_mode_gives_its_lights_only_on_the_live_page(claude):
    mode = claude
    mode.said("m1", 500)
    mode.state("busy")
    asyncio.run(mode.frame())
    assert mode.lights is not None and len(mode.lights) == 14 and max(mode.lights[0]) > 0 and mode.lights_fade == 0.25
    mode.state("idle")
    mode.again()
    asyncio.run(mode.frame())
    assert mode.lights is None
    mode.state("waiting", waitingFor="input needed")
    mode.again()
    asyncio.run(mode.frame())
    assert mode.lights is not None and len(set(mode.lights)) == 1
    mode.direct = False                                                   # a cooler that stores its pictures: no lights
    asyncio.run(mode.frame())
    assert mode.lights is None
