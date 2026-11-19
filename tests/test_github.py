import asyncio
import io
import json
import subprocess
import urllib.error
from datetime import date, timedelta

import pytest

from deephack import device
from deephack.modes import github as g

TODAY = date(2026, 11, 19)


def _days(n=371):
    start = TODAY - timedelta(days=n - 1)
    weeks, week = [], []
    for i in range(n):
        day = start + timedelta(days=i)
        count = (i * 7) % 5 if i % 3 else 0
        week.append({"date": day.isoformat(), "contributionCount": count,
                     "contributionLevel": ("NONE", "FIRST_QUARTILE", "SECOND_QUARTILE", "THIRD_QUARTILE", "FOURTH_QUARTILE")[min(count, 4)]})
        if day.weekday() == 5:            # a week runs Sunday to Saturday
            weeks.append({"contributionDays": week})
            week = []
    if week:
        weeks.append({"contributionDays": week})
    return weeks


def _pr(number, title, state="SUCCESS", draft=False, repo="octo/app"):
    rollup = {"state": state} if state else None
    return {"number": number, "title": title, "isDraft": draft, "repository": {"nameWithOwner": repo},
            "author": {"login": "octocat"}, "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}


RESPONSE = {"data": {
    "user": {"login": "octocat", "avatarUrl": "https://avatars.githubusercontent.com/u/1?s=128",
             "contributionsCollection": {"contributionCalendar": {"totalContributions": 812, "weeks": _days()}},
             "pullRequests": {"totalCount": 7, "nodes": [_pr(5, "Mine", None), _pr(4, "Also mine", "PENDING", draft=True)]}},
    "repository": {"nameWithOwner": "octo/app",
                   "defaultBranchRef": {"name": "main", "target": {"messageHeadline": "Fix the thing",
                                                                   "committedDate": "2026-11-19T11:53:23Z",
                                                                   "statusCheckRollup": {"state": "FAILURE"}}},
                   "pullRequests": {"totalCount": 1, "nodes": [_pr(12, "Repo PR", "PENDING")]}},
}}


def test_parse_with_and_without_a_repository():
    snap = g.parse(RESPONSE, True)
    assert snap.login == "octocat" and snap.total == 812 and snap.days[-1][0] == TODAY
    assert snap.repo.name == "octo/app" and snap.repo.state == "FAILURE" and snap.repo.headline == "Fix the thing"
    assert snap.prs == (g.PullRequest(12, "Repo PR", "octo/app", "octocat", False, "PENDING"),) and snap.pr_total == 1
    mine = g.parse(RESPONSE, False)
    assert mine.repo is None and mine.pr_total == 7 and mine.prs[0].state is None and mine.prs[1].draft
    missing = g.parse({"data": {**RESPONSE["data"], "repository": None}, "errors": [{"type": "NOT_FOUND"}]}, True)
    assert missing.repo is None and missing.repo_missing
    with pytest.raises(ValueError):
        g.parse({"data": {"user": None}}, False)


def test_streak():
    days = tuple((TODAY - timedelta(days=i), n, "") for i, n in enumerate((3, 1, 2, 0, 5)))
    assert g.streak(days, TODAY) == 3
    assert g.streak(((TODAY, 0, ""),) + days[1:], TODAY) == 2          # today still empty: counts up to yesterday
    assert g.streak(((TODAY, 0, ""), (TODAY - timedelta(days=1), 0, "")), TODAY) == 0


def test_render():
    for snap in (g.parse(RESPONSE, True), g.parse(RESPONSE, False),
                 g.parse({"data": {**RESPONSE["data"], "repository": None}}, True)):
        img = g.render_github(snap, None, TODAY)
        assert img.size == (device.WIDTH, device.HEIGHT) and img.mode == "RGB"


def test_token_from_the_cli_for_the_account(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args[3:])
        ok = "--user" not in args or args[-1] == "me"
        return subprocess.CompletedProcess(args, 0 if ok else 1, stdout="tok-" + (args[-1] if "--user" in args else "active") if ok else "")

    monkeypatch.setattr(g.subprocess, "run", run)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert g.gh_token("me") == "tok-me"
    assert g.gh_token("someone-else") == "tok-active"                # not signed in to gh: the active account
    monkeypatch.setenv("GH_TOKEN", "from-env")
    assert g.gh_token("someone-else") == "from-env"
    assert calls[0] == ["--hostname", "github.com", "--user", "me"]

    def missing(args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(g.subprocess, "run", missing)
    monkeypatch.delenv("GH_TOKEN")
    assert g.gh_token("me") is None


def test_graphql_errors(monkeypatch):
    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Bad credentials", {}, io.BytesIO(b""))

    monkeypatch.setattr(g.urllib.request, "urlopen", refuse)
    with pytest.raises(g.AuthError):
        g.graphql("t", "{ viewer { login } }", {})

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(g.urllib.request, "urlopen", lambda req, timeout: Reply(json.dumps({"errors": [{"message": "nope"}]}).encode()))
    with pytest.raises(ValueError, match="nope"):
        g.graphql("t", "{ viewer { login } }", {})


def test_mode_redraws_only_on_change_and_watches_running_builds(monkeypatch):
    snap = g.parse(RESPONSE, True)
    m = g.GitHub({"account": "octocat", "repo": "octo/app"})
    monkeypatch.setattr(m, "_load", lambda: (snap, "image"))
    clock = [0.0]
    monkeypatch.setattr(g.time, "monotonic", lambda: clock[0])
    assert asyncio.run(m.frame()) == "image" and m.active                 # failing CI: Auto may show it
    assert m.status == "octo/app failing · 1 open PR"
    assert m._next == 30 * 60
    clock[0] = m._next
    assert asyncio.run(m.frame()) is None                                 # nothing new, no write
    running = g.Snapshot(**{**snap.__dict__, "repo": g.Repo("octo/app", "main", "x", None, "PENDING")})
    monkeypatch.setattr(m, "_load", lambda: (running, "image 2"))
    clock[0] = m._next
    assert asyncio.run(m.frame()) == "image 2" and m._next == clock[0] + g.CI_WATCH_SECONDS


def test_mode_without_a_token(monkeypatch):
    monkeypatch.setattr(g, "gh_token", lambda account: None)
    m = g.GitHub({})
    assert asyncio.run(m.frame()) is None
    assert m.status.startswith("No GitHub token") and "gh auth login" in m.placeholder[1]
