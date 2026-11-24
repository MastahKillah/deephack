import asyncio

from deephack.modes import ticker as tk


def chart(price=352.45, start=351.91, closes=(351.0, None, 352.0, 352.45), **meta):
    m = {"currency": "USD", "shortName": "Apple Inc.   x", "longName": "Apple Inc.", "regularMarketPrice": price,
         "chartPreviousClose": start, "priceHint": 2, "instrumentType": "EQUITY", "regularMarketChangePercent": -9.0, **meta}
    return {"chart": {"result": [{"meta": m, "indicators": {"quote": [{"close": list(closes)}]}}]}}


def test_parse_symbols():
    assert tk.parse_symbols("aapl, msft;  ^gspc BTC-USD eurusd=x aapl SAP.DE") == ["AAPL", "MSFT", "^GSPC", "BTC-USD", "EURUSD=X"]
    assert tk.parse_symbols("../etc, a b?c, ") == ["A"]
    assert tk.parse_symbols("") == []


def test_parse_chart_measures_change_over_the_charted_range():
    q = tk.parse_chart("AAPL", chart())
    assert q.name == "Apple Inc." and q.closes == (351.0, 352.0, 352.45) and q.start == 351.91
    assert round(q.change_pct, 3) == 0.153                      # from the chart's start, not the feed's own field
    no_start = tk.parse_chart("AAPL", chart(start=None))
    assert no_start.change_pct == -9.0 and no_start.start is None


def test_price_and_change_text():
    q = tk.parse_chart("X", chart(price=63625.5, start=64000))
    assert q.price_text == "$63,625.50" and q.change_text == "−0.59%"
    assert tk.parse_chart("X", chart(price=1.09891, priceHint=4, instrumentType="CURRENCY")).price_text == "1.0989"
    assert tk.parse_chart("X", chart(price=7323.6, instrumentType="INDEX")).price_text == "7,323.60"
    assert tk.parse_chart("X", chart(price=192, currency="EUR")).price_text == "€192.00"
    assert tk.parse_chart("X", chart(price=512, currency="GBp")).price_text == "512.00p"
    assert tk.parse_chart("X", chart(price=10, currency="SEK")).price_text == "10.00 SEK"
    assert tk.parse_chart("X", chart(price=100, start=100)).change_text == "±0.00%"


def test_render():
    q = tk.parse_chart("AAPL", chart())
    long = tk.parse_chart("EURUSD=X", chart(price=123456789.123, closes=(1.0,), longName="A very long instrument name indeed"))
    assert tk.render_list([q, "NOPE", long, q, q], "Today").size == (480, 640)
    assert tk.render_single(q, "Today").size == (480, 640)
    assert tk.render_single(long, "1 month").size == (480, 640)


def test_mode_redraws_only_when_something_visible_changed(monkeypatch):
    feed = {"AAPL": tk.parse_chart("AAPL", chart()), "NOPE": None}
    monkeypatch.setattr(tk, "fetch", lambda symbol, rng: feed[symbol])
    m = tk.Ticker({"symbols": "AAPL, NOPE"})
    assert asyncio.run(m.frame()) is not None and m.status == "AAPL +0.15%"
    m._next = 0
    assert asyncio.run(m.frame()) is None                      # market closed: same numbers, no write
    feed["AAPL"] = tk.parse_chart("AAPL", chart(price=353.0, closes=(351.0, 352.0, 353.0)))
    m._next = 0
    assert asyncio.run(m.frame()) is not None

    none = tk.Ticker({"symbols": "NOPE"})
    assert asyncio.run(none.frame()) is None and none.placeholder[0] == "Symbols not found"


def test_network_trouble_retries(monkeypatch):
    def down(symbol, rng):
        raise OSError("offline")
    monkeypatch.setattr(tk, "fetch", down)
    m = tk.Ticker()
    assert asyncio.run(m.frame()) is None and "unreachable" in m.status


def test_next_shows_one_symbol_at_a_time_and_then_the_list_again(monkeypatch):
    feed = {"AAPL": tk.parse_chart("AAPL", chart()), "MSFT": tk.parse_chart("MSFT", chart(price=410.0, start=400.0)), "NOPE": None}
    asked = []
    monkeypatch.setattr(tk, "fetch", lambda symbol, rng: asked.append(symbol) or feed[symbol])
    m = tk.Ticker({"symbols": "AAPL, NOPE, MSFT"})
    assert m.actions == (("next", "Next"),) and [s.action for s in m.settings if s.kind == "shortcut"] == ["next"]
    listed = asyncio.run(m.frame())
    assert listed is not None and m.status == "AAPL +0.15% · MSFT +2.50%" and len(asked) == 3
    statuses = []
    for _ in range(3):
        m.act("next")
        assert asyncio.run(m.frame()) is not None and asyncio.run(m.frame()) is None
        statuses.append(m.status)
    assert statuses == ["AAPL +0.15% · 1 of 2", "MSFT +2.50% · 2 of 2", "AAPL +0.15% · MSFT +2.50%"]
    assert len(asked) == 3                                   # drawn from what was known: the feed was not asked again

    one = tk.Ticker({"symbols": "AAPL"})                     # a single symbol is large already
    assert asyncio.run(one.frame()) is not None
    one.act("next")
    assert asyncio.run(one.frame()) is None and one.status == "AAPL +0.15%"
