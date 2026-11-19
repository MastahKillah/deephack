"""GitHub: your contribution heatmap, open pull requests and the CI status of a repository.

Everything comes from one GraphQL request per refresh. GitHub's GraphQL API needs a token; deephack
stores none. It borrows the GitHub CLI's: `gh auth token --user <account>` (gh keeps it in the Windows
credential store), then the GH_TOKEN / GITHUB_TOKEN environment variables. With several accounts
signed in to gh, the account in the settings picks the token, without switching gh's active account.

The image only shows absolute dates, never "2 hours ago", so a refresh that finds nothing new costs no
flash write.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw, ImageOps

from .. import render
from ..config import config_dir
from ..render import H, W
from .base import Mode, Setting

API = "https://api.github.com/graphql"
AVATAR_HOST = "avatars.githubusercontent.com"
WEEKS = 17
RETRY_SECONDS = 120
CI_WATCH_SECONDS = 180        # while a build runs, look again this soon
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

QUERY = """
query($login: String!, $owner: String!, $name: String!, $hasRepo: Boolean!) {
  user(login: $login) {
    login avatarUrl(size: 128)
    contributionsCollection { contributionCalendar { totalContributions
      weeks { contributionDays { date contributionCount contributionLevel } } } }
    pullRequests(states: OPEN, first: 5, orderBy: {field: UPDATED_AT, direction: DESC}) { totalCount nodes { ...pr } }
  }
  repository(owner: $owner, name: $name) @include(if: $hasRepo) {
    nameWithOwner
    defaultBranchRef { name target { ... on Commit { messageHeadline committedDate statusCheckRollup { state } } } }
    pullRequests(states: OPEN, first: 3, orderBy: {field: UPDATED_AT, direction: DESC}) { totalCount nodes { ...pr } }
  }
}
fragment pr on PullRequest {
  number title isDraft repository { nameWithOwner } author { login }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""

BG_TOP, BG_BOTTOM = (16, 21, 29), (9, 12, 17)
CARD = (255, 255, 255, 13)
TEXT, MUTED, DIM = (230, 237, 243), (139, 148, 158), (84, 92, 102)
LEVELS = {"NONE": (28, 33, 41), "FIRST_QUARTILE": (14, 68, 41), "SECOND_QUARTILE": (0, 109, 50),
          "THIRD_QUARTILE": (38, 166, 65), "FOURTH_QUARTILE": (57, 211, 83)}
GREEN, RED, AMBER = (63, 185, 80), (248, 81, 73), (210, 153, 34)
STATES = {   # statusCheckRollup.state -> (label, colour)
    "SUCCESS": ("passing", GREEN), "FAILURE": ("failing", RED), "ERROR": ("failing", RED),
    "PENDING": ("running", AMBER), "EXPECTED": ("waiting", AMBER), None: ("no checks", DIM),
}


class AuthError(RuntimeError):
    pass


def gh_token(account: str = "") -> str | None:
    """A GitHub token from the GitHub CLI (for `account` if given) or the environment; never stored."""
    def gh(*args):
        try:
            out = subprocess.run(["gh", "auth", "token", "--hostname", "github.com", *args], capture_output=True,
                                 text=True, timeout=10, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None                         # gh not installed
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None

    if account:
        token = gh("--user", account)
        if token:
            return token
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or gh()


def graphql(token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables}).encode(),
                                 {"Authorization": f"bearer {token}", "User-Agent": "deephack", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError("GitHub refused the token") from e
        raise
    if not out.get("data") and out.get("errors"):
        raise ValueError(out["errors"][0].get("message", "GraphQL error"))
    return out


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    repo: str
    author: str
    draft: bool
    state: str | None          # statusCheckRollup of its last commit


@dataclass(frozen=True)
class Repo:
    name: str
    branch: str
    headline: str
    committed: datetime | None
    state: str | None


@dataclass(frozen=True)
class Snapshot:
    login: str
    avatar_url: str
    total: int
    days: tuple[tuple[date, int, str], ...]     # (day, contributions, level), oldest first
    prs: tuple[PullRequest, ...]
    pr_total: int
    repo: Repo | None
    repo_missing: bool = False


def _rollup(node) -> str | None:
    try:
        return node["commits"]["nodes"][-1]["commit"]["statusCheckRollup"]["state"]
    except (KeyError, IndexError, TypeError):
        return None


def _prs(conn) -> tuple[tuple[PullRequest, ...], int]:
    nodes = (conn or {}).get("nodes") or []
    prs = tuple(PullRequest(n["number"], n["title"], (n.get("repository") or {}).get("nameWithOwner", ""),
                            (n.get("author") or {}).get("login", "ghost"), bool(n.get("isDraft")), _rollup(n))
                for n in nodes if n)
    return prs, int((conn or {}).get("totalCount") or len(prs))


def parse(out: dict, has_repo: bool) -> Snapshot:
    data = out.get("data") or {}
    user = data.get("user")
    if not user:
        raise ValueError("GitHub user not found")
    calendar = user["contributionsCollection"]["contributionCalendar"]
    days = tuple((date.fromisoformat(d["date"]), int(d["contributionCount"]), d.get("contributionLevel") or "NONE")
                 for w in calendar["weeks"] for d in w["contributionDays"])
    repo, missing = None, False
    prs, pr_total = _prs(user.get("pullRequests"))
    if has_repo:
        r = data.get("repository")
        if r is None:
            missing = True
        else:
            target = ((r.get("defaultBranchRef") or {}).get("target")) or {}
            committed = target.get("committedDate")
            repo = Repo(r["nameWithOwner"], (r.get("defaultBranchRef") or {}).get("name", ""), target.get("messageHeadline", ""),
                        datetime.fromisoformat(committed.replace("Z", "+00:00")).astimezone() if committed else None,
                        (target.get("statusCheckRollup") or {}).get("state"))
            prs, pr_total = _prs(r.get("pullRequests"))
    return Snapshot(user["login"], user.get("avatarUrl", ""), int(calendar["totalContributions"]), days, prs, pr_total, repo, missing)


def streak(days: tuple[tuple[date, int, str], ...], today: date) -> int:
    """Days in a row with contributions, up to today (or yesterday, while today is still empty)."""
    counts = {d: n for d, n, _ in days}
    day = today if counts.get(today) else today - timedelta(days=1)
    n = 0
    while counts.get(day):
        n, day = n + 1, day - timedelta(days=1)
    return n


def avatar(url: str) -> Image.Image | None:
    if not url or AVATAR_HOST not in url:
        return None
    cache = config_dir() / "cache" / "github"
    path = cache / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
    try:
        if not path.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "deephack"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read(2_000_000)
            cache.mkdir(parents=True, exist_ok=True)
            Image.open(io.BytesIO(data)).convert("RGB").save(path)
        return Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ drawing
def _card(img: Image.Image, box) -> ImageDraw.ImageDraw:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=14, fill=CARD)
    img.alpha_composite(layer)
    return ImageDraw.Draw(img)


def _pill(d: ImageDraw.ImageDraw, right: float, cy: float, text: str, colour) -> None:
    fnt = render.font(render.SEMIBOLD, 17)
    tw = d.textlength(text, font=fnt)
    d.rounded_rectangle([right - tw - 34, cy - 14, right, cy + 14], radius=14, fill=tuple(round(c * 0.25) for c in colour))
    d.ellipse([right - tw - 24, cy - 4, right - tw - 16, cy + 4], fill=colour)
    d.text((right - 11, cy), text, font=fnt, fill=colour, anchor="rm")


def _heatmap(d: ImageDraw.ImageDraw, days, today: date, top: int) -> int:
    """The last WEEKS weeks, a column per week (Sunday on top), month names above. Returns the bottom."""
    cell, gap = 22, 4
    start = today - timedelta(days=(today.weekday() + 1) % 7 + 7 * (WEEKS - 1))     # a Sunday
    by_day = {day: level for day, _, level in days}
    left = (W - (WEEKS * (cell + gap) - gap)) // 2
    small = render.font(render.REGULAR, 15)
    last_month, last_label = start.month, -9
    for week in range(WEEKS):
        first = start + timedelta(days=7 * week)
        starts_month = first.month != last_month or (week == 0 and first.day <= 7)
        last_month = first.month
        if starts_month and week - last_label >= 3 and week < WEEKS - 1:    # never two names side by side
            d.text((left + week * (cell + gap), top), first.strftime("%b"), font=small, fill=MUTED, anchor="lt")
            last_label = week
        for row in range(7):
            day = first + timedelta(days=row)
            if day > today:
                continue
            x, y = left + week * (cell + gap), top + 24 + row * (cell + gap)
            d.rounded_rectangle([x, y, x + cell, y + cell], radius=5, fill=LEVELS.get(by_day.get(day, "NONE"), LEVELS["NONE"]))
            if day == today:
                d.rounded_rectangle([x - 2, y - 2, x + cell + 2, y + cell + 2], radius=6, outline=TEXT, width=2)
    return top + 24 + 7 * (cell + gap) - gap


def _pr_rows(img: Image.Image, prs, top: int, rows: int, show_repo: bool) -> None:
    d = ImageDraw.Draw(img)
    title_font, sub_font = render.font(render.SEMIBOLD, 20), render.font(render.REGULAR, 16)
    if not prs:
        d.text((W / 2, top + 30), "No open pull requests", font=render.font(render.REGULAR, 19), fill=MUTED, anchor="mm")
        return
    for i, pr in enumerate(prs[:rows]):
        y = top + i * 52
        _, colour = STATES.get(pr.state, STATES[None])
        d.ellipse([34, y + 9, 46, y + 21], fill=DIM if pr.draft else colour)
        number = f"#{pr.number}"
        d.text((60, y + 15), number, font=sub_font, fill=MUTED, anchor="lm")
        x = 60 + d.textlength(number, font=sub_font) + 8
        d.text((x, y + 15), render.fit(d, pr.title, title_font, W - 36 - x), font=title_font, fill=TEXT, anchor="lm")
        where = pr.repo if show_repo else f"by {pr.author}"
        d.text((60, y + 38), where + ("  ·  draft" if pr.draft else ""), font=sub_font, fill=DIM, anchor="lm")


def render_github(snap: Snapshot, face: Image.Image | None, today: date) -> Image.Image:
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)

    # who
    x = 28
    if face is not None:
        size = 60
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
        img.paste(ImageOps.fit(face, (size, size), Image.LANCZOS), (x, 24), mask.resize((size, size), Image.LANCZOS))
        x += size + 16
    d.text((x, 38), snap.login, font=render.font(render.BOLD, 28), fill=TEXT, anchor="lm")
    d.text((x, 70), f"{snap.total:,} contributions in the last year".replace(",", " "), font=render.font(render.REGULAR, 18),
           fill=MUTED, anchor="lm")

    bottom = _heatmap(d, snap.days, today, 104)
    counts = {day: n for day, n, _ in snap.days}
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    this_week = sum(n for day, n in counts.items() if week_start <= day <= today)
    run = streak(snap.days, today)
    stats = f"{counts.get(today, 0)} today  ·  {this_week} this week" + (f"  ·  {run} day streak" if run > 1 else "")
    d.text((W / 2, bottom + 22), stats, font=render.font(render.SEMIBOLD, 18), fill=MUTED, anchor="mm")

    small = render.font(render.BOLD, 15)
    y = bottom + 50
    if snap.repo is not None or snap.repo_missing:
        dd = _card(img, [20, y, W - 20, y + 92])
        if snap.repo is None:
            dd.text((40, y + 46), "Repository not found", font=render.font(render.SEMIBOLD, 20), fill=MUTED, anchor="lm")
        else:
            r = snap.repo
            label, colour = STATES.get(r.state, STATES[None])
            _pill(dd, W - 36, y + 28, label, colour)
            name_font = render.font(render.SEMIBOLD, 21)
            dd.text((38, y + 28), render.fit(dd, r.name, name_font, W - 220), font=name_font, fill=TEXT, anchor="lm")
            when = r.committed.strftime("%d %b %H:%M") if r.committed else ""
            sub = render.font(render.REGULAR, 17)
            dd.text((W - 38, y + 64), when, font=sub, fill=DIM, anchor="rm")
            headline_w = W - 76 - (dd.textlength(when, font=sub) + 14 if when else 0)
            dd.text((38, y + 64), render.fit(dd, f"{r.branch}: {r.headline}", sub, headline_w), font=sub, fill=MUTED, anchor="lm")
        y += 110
    rows = max(1, (H - 20 - (y + 26)) // 52)
    title = "OPEN PULL REQUESTS" if snap.repo is not None else "YOUR OPEN PULL REQUESTS"
    d.text((28, y + 6), title, font=small, fill=MUTED, anchor="lm")
    if snap.pr_total:
        d.text((W - 28, y + 6), str(snap.pr_total), font=small, fill=MUTED, anchor="rm")
    _pr_rows(img, snap.prs, y + 26, rows, show_repo=snap.repo is None)
    return img.convert("RGB")


class GitHub(Mode):
    id = "github"
    title = "GitHub"
    description = "Contributions, PRs and CI"
    glyph = ""
    claims = "CI running or failing"
    settings = (
        Setting("account", "Account", "text", "", placeholder="your GitHub user name",
                hint="Its contribution heatmap. The token comes from the GitHub CLI (run gh auth login once); "
                     "deephack stores none. Empty: the account gh is signed in with."),
        Setting("repo", "Repository", "text", "", placeholder="owner/name",
                hint="Its CI status and open pull requests. Empty: your own open pull requests."),
        Setting("refresh", "Refresh every", "choice", "30", (("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="Redrawn only when something changed; while a build runs it is checked every few minutes."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next = 0.0
        self._shown = None
        self._token: str | None = None
        self._login: str = ""
        self.placeholder = ("GitHub", "Fetching…")

    def _load(self) -> tuple[Snapshot, Image.Image]:
        account = self.values["account"].strip().lstrip("@")
        if self._token is None:
            self._token = gh_token(account)
            if self._token is None:
                raise AuthError("No GitHub token: run gh auth login")
        if not account and not self._login:
            self._login = graphql(self._token, "{ viewer { login } }", {})["data"]["viewer"]["login"]
        login = account or self._login
        owner, _, name = self.values["repo"].strip().strip("/").partition("/")
        has_repo = bool(owner and name)
        out = graphql(self._token, QUERY, {"login": login, "owner": owner, "name": name, "hasRepo": has_repo})
        snap = parse(out, has_repo)
        today = date.today()
        return snap, render_github(snap, avatar(snap.avatar_url), today)

    async def frame(self):
        if time.monotonic() < self._next:
            return None
        try:
            snap, image = await asyncio.to_thread(self._load)
        except AuthError as e:
            self._token = None
            self.status, self.placeholder = str(e), ("GitHub", "Sign in with the GitHub CLI: gh auth login")
            self._next = time.monotonic() + RETRY_SECONDS
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.status = f"GitHub unreachable ({e})" if isinstance(e, OSError) else f"GitHub: {e}"
            self._next = time.monotonic() + RETRY_SECONDS
            return None
        state = snap.repo.state if snap.repo else None
        self.active = state in ("PENDING", "EXPECTED", "FAILURE", "ERROR")
        watching = state in ("PENDING", "EXPECTED")
        self._next = time.monotonic() + (CI_WATCH_SECONDS if watching else int(self.values["refresh"]) * 60)
        label = STATES.get(state, STATES[None])[0]
        repo = f"{snap.repo.name} {label} · " if snap.repo else ("repository not found · " if snap.repo_missing else "")
        self.status = f"{repo}{snap.pr_total} open PR{'s' if snap.pr_total != 1 else ''}"
        key = (snap, date.today())
        if key == self._shown:
            return None
        self._shown = key
        return image
