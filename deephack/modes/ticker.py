"""Stocks, indices, currencies and crypto: price, daily change and a sparkline.

Prices come from Yahoo Finance's public chart endpoint (no account or key), which covers stocks (AAPL,
SAP.DE), indices (^GSPC), currency pairs (EURUSD=X) and crypto (BTC-USD). The display is rewritten only
when a visible number or the chart changed, so a closed market costs no flash writes.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .. import render
from ..render import H, W
from .base import Mode, Setting

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
INTERVALS = {"1d": "5m", "5d": "30m", "1mo": "1d"}
RANGES = (("1d", "Today"), ("5d", "5 days"), ("1mo", "1 month"))
MAX_SYMBOLS = 5
RETRY_SECONDS = 120
_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,19}$")
CURRENCY = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "INR": "₹", "KRW": "₩", "CHF": "CHF ", "CAD": "C$", "AUD": "A$"}

BG_TOP, BG_BOTTOM = (20, 24, 34), (8, 10, 16)
CARD = (255, 255, 255, 14)
TEXT, MUTED, DIM = (240, 242, 248), (140, 146, 166), (90, 96, 116)
UP, DOWN = (52, 199, 120), (240, 88, 88)


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    change_pct: float              # over the charted range (since the previous close for "Today")
    currency: str
    decimals: int
    kind: str                      # EQUITY, INDEX, CURRENCY, CRYPTOCURRENCY, ...
    closes: tuple[float, ...]      # the sparkline
    start: float | None            # close before the charted range began: the baseline of the chart

    @property
    def price_text(self) -> str:
        number = f"{self.price:,.{self.decimals}f}"
        if self.kind in ("INDEX", "CURRENCY"):
            return number
        if self.currency == "GBp":
            return number + "p"
        prefix = CURRENCY.get(self.currency)
        return prefix + number if prefix else f"{number} {self.currency}"

    @property
    def change_text(self) -> str:
        sign = "+" if self.change_pct > 0.005 else "−" if self.change_pct < -0.005 else "±"
        return f"{sign}{abs(self.change_pct):.2f}%"


def parse_symbols(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[,;\s]+", text.upper()):
        if raw and _SYMBOL.match(raw) and raw not in out:
            out.append(raw)
    return out[:MAX_SYMBOLS]


def parse_chart(symbol: str, data: dict) -> Quote:
    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = float(meta["regularMarketPrice"])
    start = meta.get("chartPreviousClose") or meta.get("previousClose")
    if start:
        change = (price / start - 1) * 100       # over the same period as the chart
    else:
        change = meta.get("regularMarketChangePercent") if isinstance(meta.get("regularMarketChangePercent"), (int, float)) else 0.0
    closes = tuple(float(c) for c in (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or [] if c is not None)
    name = " ".join(str(meta.get("longName") or meta.get("shortName") or symbol).split())
    decimals = meta.get("priceHint") if isinstance(meta.get("priceHint"), int) else 2
    return Quote(symbol, name, price, float(change), str(meta.get("currency") or ""), max(0, min(decimals, 6)),
                 str(meta.get("instrumentType") or ""), closes, float(start) if start else None)


def fetch(symbol: str, chart_range: str) -> Quote | None:
    """The quote, or None for an unknown symbol. Network trouble raises OSError."""
    url = CHART_URL.format(symbol=urllib.parse.quote(symbol, safe=""), range=chart_range, interval=INTERVALS[chart_range])
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (deephack)"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return parse_chart(symbol, json.load(r))
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return None
        raise
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------ rendering
def sparkline(img: Image.Image, box: tuple[int, int, int, int], closes: tuple[float, ...], colour, baseline: float | None,
              width: int = 3) -> None:
    """Line chart of closes inside box with a soft fill under it; baseline (the previous close) dotted."""
    if len(closes) < 2:
        return
    x0, y0, x1, y1 = box
    s = 2                                               # drawn at twice the size for smooth edges
    lo, hi = min(closes), max(closes)
    if baseline is not None and lo <= baseline * 1.5 and baseline <= hi * 1.5:
        lo, hi = min(lo, baseline), max(hi, baseline)
    span = (hi - lo) or abs(hi) * 0.01 or 1.0
    w, h = (x1 - x0) * s, (y1 - y0) * s
    pad = width * s
    pts = [(i * (w - 1) / (len(closes) - 1), pad + (h - 2 * pad) * (1 - (c - lo) / span)) for i, c in enumerate(closes)]
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon(pts + [(w - 1, h), (0, h)], fill=colour + (40,))
    if baseline is not None and lo <= baseline <= hi:
        by = pad + (h - 2 * pad) * (1 - (baseline - lo) / span)
        for x in range(0, w, 12 * s // 2):
            d.line([(x, by), (x + 4 * s, by)], fill=DIM + (255,), width=s)
    d.line(pts, fill=colour + (255,), width=width * s, joint="curve")
    img.alpha_composite(layer.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0, y0))


def _card(img: Image.Image, box) -> ImageDraw.ImageDraw:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=16, fill=CARD)
    img.alpha_composite(layer)
    return ImageDraw.Draw(img)


def _pill(d: ImageDraw.ImageDraw, right: float, cy: float, text: str, colour, size: int) -> None:
    fnt = render.font(render.SEMIBOLD, size)
    tw = d.textlength(text, font=fnt)
    d.rounded_rectangle([right - tw - size, cy - size * 0.8, right, cy + size * 0.8], radius=size * 0.8,
                        fill=tuple(round(c * 0.28) for c in colour))
    d.text((right - size / 2, cy), text, font=fnt, fill=colour, anchor="rm")


def _colour(q: Quote):
    return UP if q.change_pct > 0.005 else DOWN if q.change_pct < -0.005 else MUTED


def render_single(q: Quote, label: str) -> Image.Image:
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)
    f = render.font
    d.text((32, 48), q.symbol, font=f(render.BOLD, 40), fill=TEXT, anchor="lm")
    d.text((32, 90), render.fit(d, q.name, f(render.REGULAR, 22), W - 64), font=f(render.REGULAR, 22), fill=MUTED, anchor="lm")
    price = q.price_text
    size = 96
    while size > 40 and d.textlength(price, font=f(render.LIGHT, size)) > W - 64:
        size -= 6
    d.text((32, 190), price, font=f(render.LIGHT, size), fill=TEXT, anchor="lm")
    right = 32 + d.textlength(q.change_text, font=f(render.SEMIBOLD, 26)) + 26
    _pill(d, right, 272, q.change_text, _colour(q), 26)
    d.text((right + 14, 273), label.lower(), font=f(render.REGULAR, 22), fill=MUTED, anchor="lm")
    d = _card(img, [20, 320, W - 20, H - 20])
    sparkline(img, (40, 344, W - 40, H - 44), q.closes, _colour(q), q.start, width=4)
    return img.convert("RGB")


def render_list(quotes: list[Quote | str], label: str) -> Image.Image:
    """One row per symbol; a str in quotes is a symbol that was not found."""
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)
    f = render.font
    d.text((28, 40), "Markets", font=f(render.SEMIBOLD, 30), fill=TEXT, anchor="lm")
    d.text((W - 28, 42), label, font=f(render.REGULAR, 19), fill=MUTED, anchor="rm")
    row_h, gap, top = 104, 8, 80
    for i, q in enumerate(quotes):
        y = top + i * (row_h + gap)
        d = _card(img, [20, y, W - 20, y + row_h])
        if isinstance(q, str):
            d.text((40, y + row_h / 2 - 14), q, font=f(render.BOLD, 26), fill=MUTED, anchor="lm")
            d.text((40, y + row_h / 2 + 18), "Symbol not found", font=f(render.REGULAR, 16), fill=DIM, anchor="lm")
            continue
        colour = _colour(q)
        sym_font = f(render.BOLD, 26)
        while sym_font.size > 16 and d.textlength(q.symbol, font=sym_font) > 140:
            sym_font = f(render.BOLD, sym_font.size - 2)
        d.text((40, y + 36), q.symbol, font=sym_font, fill=TEXT, anchor="lm")
        d.text((40, y + 70), render.fit(d, q.name, f(render.REGULAR, 16), 140), font=f(render.REGULAR, 16), fill=MUTED, anchor="lm")
        sparkline(img, (192, y + 22, 300, y + row_h - 22), q.closes, colour, q.start)
        d = ImageDraw.Draw(img)
        price_font = f(render.SEMIBOLD, 25)
        while price_font.size > 14 and d.textlength(q.price_text, font=price_font) > W - 40 - 316:
            price_font = f(render.SEMIBOLD, price_font.size - 2)
        d.text((W - 40, y + 36), q.price_text, font=price_font, fill=TEXT, anchor="rm")
        _pill(d, W - 40, y + 72, q.change_text, colour, 17)
    return img.convert("RGB")


class Ticker(Mode):
    id = "ticker"
    title = "Ticker"
    description = "Stocks and crypto prices"
    glyph = ""
    actions = (("next", "Next"),)             # from the list to each symbol by itself, large, and back to the list
    settings = (
        Setting("symbols", "Symbols", "text", "AAPL, MSFT, ^GSPC, BTC-USD", placeholder="AAPL, SAP.DE, BTC-USD",
                hint="Up to five Yahoo Finance symbols: shares (SAP.DE for other exchanges), indices (^GSPC), "
                     "currency pairs (EURUSD=X) and crypto (BTC-USD)."),
        Setting("range", "Chart", "choice", "1d", RANGES),
        Setting("refresh", "Refresh every", "choice", "30", (("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="Prices come from Yahoo Finance and can be delayed. A closed market causes no new writes."),
        Setting("next_key", "Key for the next symbol", "shortcut", "Ctrl+Alt+PgDown", action="next",
                hint="Next shows one symbol at a time, large and with its chart, and after the last one the list "
                     "again. The key works whatever window is in front, while the Ticker is on the cooler. Empty: no key."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next = 0.0
        self._shown = None
        self._quotes: list[Quote | str] = []      # the last answer, to draw again when another symbol is asked for
        self._view = 0                            # 0: all of them in a list; n: the nth one found, by itself
        self._stepped = False                     # Next was asked for: draw now, from what is known
        self.placeholder = ("Ticker", "Add symbols in the settings on the right.")

    def act(self, action: str) -> None:
        if action == "next":
            self._view, self._stepped = self._view + 1, True

    def _load(self, symbols: list[str]) -> list[Quote | str]:
        return [fetch(s, self.values["range"]) or s for s in symbols]

    async def frame(self):
        symbols = parse_symbols(self.values["symbols"])
        if not symbols:
            self.status = "No symbols set"
            return None
        stepped, self._stepped = self._stepped, False
        if time.monotonic() >= self._next:
            try:
                self._quotes = await asyncio.to_thread(self._load, symbols)
            except OSError as e:
                self.status = f"Price feed unreachable ({type(e).__name__})"
                self._next = time.monotonic() + RETRY_SECONDS
                return None
            self._next = time.monotonic() + int(self.values["refresh"]) * 60
        elif not stepped:
            return None
        quotes = self._quotes
        found = [q for q in quotes if isinstance(q, Quote)]
        if not found:
            self.status = "None of the symbols were found"
            self.placeholder = ("Symbols not found", "Use Yahoo Finance symbols, e.g. AAPL, SAP.DE, ^GSPC or BTC-USD.")
            return None
        self.status = " · ".join(f"{q.symbol} {q.change_text}" for q in found)
        label = dict(RANGES)[self.values["range"]]
        self._view %= len(found) + 1 if len(quotes) > 1 else 1      # (one symbol is shown by itself as it is)
        if self._view:
            quotes = [found[self._view - 1]]
            self.status = f"{quotes[0].symbol} {quotes[0].change_text} · {self._view} of {len(found)}"
        # only what is visible: the printed numbers and the chart's points at display precision
        key = (label, tuple(q if isinstance(q, str) else (q.symbol, q.name, q.price_text, q.change_text,
                                                           tuple(round(c, q.decimals) for c in q.closes)) for q in quotes))
        if key == self._shown:
            return None
        self._shown = key
        if len(quotes) == 1 and isinstance(quotes[0], Quote):
            return render_single(quotes[0], label)
        return render_list(quotes, label)
