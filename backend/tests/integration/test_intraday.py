"""Official intraday bars from Alpaca: the client, the minute refresh, restart safety, provenance, history download, freshness."""
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.database.models import BarRow
from app.domain.models import AssetClass
from app.execution.runtime import TradingRuntime
from app.market_data.alpaca_bars import AlpacaAuthError, AlpacaBars, AlpacaBarsError, FeedNotAllowed, describe_source, in_session
from app.market_data.calendars import get_calendar
from app.market_data.live import FeedStatus, Tick
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset
from app.market_data.timeframes import tf_delta
from app.security.vault import CredentialVault
from sqlalchemy import func, select
from tests.conftest import login
from tests.integration.test_live import Feeder

NOW = datetime(2026, 11, 12, 16, 0, 30, tzinfo=timezone.utc)  # a Thursday, 11:00 in New York: the market is open
AAPL, MSFT, BTC = "STOCK:AAPL", "STOCK:MSFT", "CRYPTO:BTC/USD"
KEY, SECRET = "PK-SECRET-KEY-ID", "SK-VERY-SECRET-VALUE"


def ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def truth(start: pd.Timestamp, end: pd.Timestamp, timeframe: str, shift: float = 0.0) -> pd.DataFrame:
    """The 'market': a deterministic bar for every period, so any two requests for the same bar agree."""
    index = pd.date_range(start.ceil(tf_delta(timeframe)), end, freq=tf_delta(timeframe), name="ts")
    n = (index.astype("int64") // 60_000_000_000).to_numpy() if len(index) else np.array([], dtype="int64")
    o = 100 + shift + (n % 97) / 10
    c = o + ((n % 7) - 3) / 20
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.05, "low": np.minimum(o, c) - 0.05, "close": c, "volume": 1000.0 + n % 50}, index=index)


# ---------------------------------------------------------------- the HTTP client
def alpaca_api(calls, now=NOW, page=10_000, script=None):
    """Alpaca's bars endpoints, serving `truth` with real pagination. `script` is a list of status codes to answer first."""
    script = list(script or [])

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        calls.append((request.url.path, q, request.headers.get("apca-api-key-id")))
        if script:
            code = script.pop(0)
            if code != 200:
                return httpx.Response(code, json={"message": "limit exceeded" if code == 429 else "subscription does not permit querying recent SIP data"},
                                      headers={"Retry-After": "2"} if code == 429 else {})
        tf = {"1Min": "1m", "5Min": "5m", "15Min": "15m"}[q["timeframe"]]
        offset = int(q.get("page_token", 0))
        rows = []
        for symbol in q["symbols"].split(","):
            frame = truth(pd.Timestamp(q["start"]), pd.Timestamp(q["end"]), tf)
            if q["sort"] == "desc":
                frame = frame.iloc[::-1]
            rows += [(symbol, t, r) for t, r in frame.iterrows()]
        part = rows[offset: offset + page]
        bars: dict = {}
        for symbol, t, r in part:
            bars.setdefault(symbol, []).append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume, "n": 5, "vw": r.close})
        return httpx.Response(200, json={"bars": bars, "next_page_token": str(offset + page) if offset + page < len(rows) else None})

    return handler


def client(calls, **kw) -> AlpacaBars:
    sleeps = kw.pop("sleeps", [])
    api = alpaca_api(calls, page=kw.pop("page", 10_000), script=kw.pop("script", None))
    return AlpacaBars(KEY, SECRET, client=httpx.Client(transport=httpx.MockTransport(api)), min_interval=0, sleep=sleeps.append, clock=lambda: NOW, **kw)


def test_client_pages_through_many_symbols_and_never_returns_a_forming_bar():
    calls = []
    bars = client(calls, page=50)
    pages = list(bars.pages(["AAPL", "MSFT"], "1m", ts("2026-11-12 15:00"), None, "iex"))
    path, q, key = calls[0]
    assert path == "/v2/stocks/bars" and key == KEY and q["symbols"] == "AAPL,MSFT" and q["timeframe"] == "1Min"
    assert q["feed"] == "iex" and q["adjustment"] == "split" and q["sort"] == "asc" and q["limit"] == "10000" and q["start"] == "2026-11-12T15:00:00Z"
    assert len(calls) == 3 and calls[1][1]["page_token"] == "50"  # 61 bars per symbol, 50 per page
    merged = {s: pd.concat([p[s] for p in pages if s in p]) for s in ("AAPL", "MSFT")}
    # 15:00 .. 15:59 are complete at 16:00:30; the 16:00 bar is still being built and is left out.
    assert all(len(f) == 60 and f.index[0] == ts("2026-11-12 15:00") and f.index[-1] == ts("2026-11-12 15:59") and f.index.is_unique for f in merged.values())
    pd.testing.assert_frame_equal(merged["AAPL"], truth(ts("2026-11-12 15:00"), ts("2026-11-12 15:59"), "1m"), check_freq=False)
    assert list(bars.pages([], "1m", ts("2026-11-12 15:00"))) == [] and list(bars.pages(["AAPL"], "1m", ts("2026-11-12 17:00"))) == []
    with pytest.raises(AlpacaBarsError):
        list(bars.pages(["AAPL"], "4h", ts("2026-11-12 15:00")))


def test_client_keeps_the_consolidated_feed_fifteen_minutes_behind_and_asks_crypto_differently():
    calls = []
    bars = client(calls)
    sip = pd.concat(p["AAPL"] for p in bars.pages(["AAPL"], "1m", ts("2026-11-12 15:00"), None, "sip"))
    assert calls[0][1]["feed"] == "sip" and calls[0][1]["end"] == "2026-11-12T15:44:30Z" and sip.index[-1] == ts("2026-11-12 15:44")
    back = list(bars.pages(["AAPL"], "5m", ts("2026-11-12 15:00"), ts("2026-11-12 15:30"), "iex", newest_first=True))
    assert calls[-1][1]["sort"] == "desc" and back[0]["AAPL"].index.is_monotonic_increasing and len(back[0]["AAPL"]) == 7
    list(bars.pages(["BTC/USD"], "1m", ts("2026-11-12 15:50"), None, "crypto"))
    path, q, _ = calls[-1]
    assert path == "/v1beta3/crypto/us/bars" and "feed" not in q and "adjustment" not in q


def test_client_retries_rate_limits_and_server_errors_and_classifies_refusals_without_leaking_the_key():
    calls, sleeps = [], []
    bars = client(calls, sleeps=sleeps, script=[429, 503, 200])
    out = list(bars.pages(["AAPL"], "1m", ts("2026-11-12 15:55"), None, "iex"))
    assert len(out) == 1 and len(calls) == 3 and sleeps == [2.0, 2.0]  # Retry-After obeyed, then a backoff after the 503
    assert (bars.requests, bars.rate_limited, bars.retries) == (3, 1, 2)
    for code, error in ((401, AlpacaAuthError), (403, FeedNotAllowed), (422, AlpacaBarsError)):
        with pytest.raises(error) as info:
            list(client([], script=[code]).pages(["AAPL"], "1m", ts("2026-11-12 15:00"), None, "sip"))
        assert KEY not in str(info.value) and SECRET not in str(info.value)
    with pytest.raises(AlpacaBarsError, match="rate limit"):
        list(client([], sleeps=[], script=[429] * 9, max_retries=2).pages(["AAPL"], "1m", ts("2026-11-12 15:55")))

    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url} with {request.headers['apca-api-secret-key']}")

    offline = AlpacaBars(KEY, SECRET, client=httpx.Client(transport=httpx.MockTransport(down)), min_interval=0, sleep=lambda s: None, max_retries=1, clock=lambda: NOW)
    with pytest.raises(AlpacaBarsError) as info:
        list(offline.pages(["AAPL"], "1m", ts("2026-11-12 15:55")))
    assert SECRET not in str(info.value) and "alpaca.markets" not in str(info.value) and info.value.__cause__ is None
    # Crypto data needs no key: a refused key is dropped and the request repeated without it.
    calls = []
    crypto = client(calls, script=[401])
    assert len(list(crypto.pages(["BTC/USD"], "1m", ts("2026-11-12 15:55"), None, "crypto"))) == 1
    assert calls[0][2] == KEY and calls[1][2] is None


def test_session_filter_and_feed_labels():
    index = pd.DatetimeIndex([ts("2026-11-12 14:29"), ts("2026-11-12 14:30"), ts("2026-11-12 20:59"), ts("2026-11-12 21:00"), ts("2026-11-14 15:00")])
    assert in_session(index, get_calendar("XNYS")).tolist() == [False, True, True, False, False]  # 09:30-16:00 New York, weekdays
    assert in_session(index, get_calendar("24x7")).all()
    summer = pd.DatetimeIndex([ts("2026-07-01 13:30"), ts("2026-07-01 14:29")])  # daylight time: the session starts an hour earlier in UTC
    assert in_session(summer, get_calendar("XNYS")).tolist() == [True, True]
    assert "IEX" in describe_source("alpaca:iex") and "delayed 15 minutes" in describe_source("alpaca:sip-delayed")
    assert describe_source("alpaca:iex+live:alpaca-stocks").count(";") == 1 and describe_source("csv-import") == "csv-import"


# ---------------------------------------------------------------- the service on a runtime
class FakeBars:
    """Stands in for AlpacaBars: serves `truth`, counts requests, and can be made to fail."""

    def __init__(self, clock, page_rows=10_000):
        self.clock, self.page_rows = clock, page_rows
        self.calls, self.requests, self.rate_limited = [], 0, 0
        self.fail: Exception | None = None
        self.fail_after: int | None = None  # raise once this many more pages have been served
        self.refuse_sip = False
        self.shift = 0.0

    def pages(self, symbols, timeframe, start, end=None, feed="iex", newest_first=False):
        now = pd.Timestamp(self.clock())
        end = min(end, now) if end is not None else now
        if feed == "sip":
            end = min(end, now - pd.Timedelta(minutes=16))
        self.calls.append({"symbols": list(symbols), "tf": timeframe, "start": start, "end": end, "feed": feed, "newest_first": newest_first})
        self.requests += 1
        if self.fail is not None:
            raise self.fail
        if feed == "sip" and self.refuse_sip:
            raise FeedNotAllowed("Alpaca refused the request: subscription does not permit querying SIP data")
        if end < start:
            return
        delta = tf_delta(timeframe)
        for symbol in symbols:
            frame = truth(start, end, timeframe, self.shift)
            frame = frame[frame.index + delta <= now]
            order = frame.iloc[::-1] if newest_first else frame
            for i in range(0, len(order), self.page_rows):
                if self.fail_after is not None:
                    if self.fail_after == 0:
                        raise AlpacaBarsError("Alpaca could not be reached (ConnectError)")
                    self.fail_after -= 1
                yield {symbol: order.iloc[i:i + self.page_rows].sort_index()}


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    now = [NOW]
    runtime = TradingRuntime(db, Settings(_env_file=None, credential_key=CredentialVault.generate_key(), enabled_providers="alpaca"), clock=lambda: now[0])
    runtime.start()
    runtime.create_paper_account("paper-main", "Main", "admin")
    runtime.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", NOW - timedelta(hours=2))
    runtime.set_provider_key("alpaca", KEY, "admin", SECRET)
    runtime.add_instruments(["AAPL", "MSFT"], "US", "stock", "admin")
    runtime.add_instruments(["BTC/USD"], "", "crypto", "admin")
    feeder = Feeder(runtime.live_hub)
    runtime.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.asset_class in (AssetClass.STOCK, AssetClass.CRYPTO) else None)]
    fake = FakeBars(runtime.clock)
    runtime.intraday.client_factory = lambda group, key, secret, interval: fake
    runtime.now, runtime.fake = now, fake
    return runtime


def seed(rt, iid, timeframe, start, end, source="alpaca:iex", stocks=True):
    frame = truth(ts(start), ts(end), timeframe)
    if stocks:
        frame = frame[in_session(frame.index, get_calendar("XNYS")).to_numpy()]
    with rt.db.session() as s:
        rt.store.save(s, iid, timeframe, Dataset(frame, DataKind.HISTORICAL, source))
    return frame


def stored(rt, iid, timeframe="1m"):
    with rt.db.session() as s:
        return rt.store.load(s, iid, timeframe)


def sources(rt, iid, timeframe="1m") -> dict:
    with rt.db.session() as s:
        return dict(s.execute(select(BarRow.source, func.count()).where(BarRow.instrument_id == iid, BarRow.timeframe == timeframe).group_by(BarRow.source)).all())


def test_minute_refresh_appends_only_new_official_bars_and_labels_their_feed(rt):
    seed(rt, AAPL, "1m", "2026-11-12 14:30", "2026-11-12 15:49")
    seed(rt, AAPL, "5m", "2026-11-12 14:30", "2026-11-12 15:40")
    seed(rt, BTC, "1m", "2026-11-12 15:00", "2026-11-12 15:54", source="alpaca:crypto", stocks=False)
    out = rt.intraday.tick()
    assert out["stocks"]["bars"] == 10 + 3 and out["crypto"]["bars"] == 5  # AAPL 1m 15:50..15:59, 5m 15:45..15:55; BTC 15:55..15:59
    by_tf = {(c["tf"], c["feed"]): c for c in rt.fake.calls}
    assert by_tf[("1m", "iex")]["start"] == ts("2026-11-12 15:50") and by_tf[("5m", "iex")]["start"] == ts("2026-11-12 15:45")
    assert by_tf[("1m", "crypto")]["symbols"] == ["BTC/USD"] and by_tf[("1m", "iex")]["symbols"] == ["AAPL"]  # MSFT has nothing stored and is not followed
    data = stored(rt, AAPL)
    assert data.bars.index[-1] == ts("2026-11-12 15:59") and data.bars.index.is_unique and len(data.bars) == 90
    pd.testing.assert_frame_equal(data.bars.iloc[-10:], truth(ts("2026-11-12 15:50"), ts("2026-11-12 15:59"), "1m"), check_freq=False)
    assert sources(rt, AAPL) == {"alpaca:iex": 90} and sources(rt, BTC) == {"alpaca:crypto": 60}
    with rt.db.session() as s:
        summary = {(c["instrument_id"], c["timeframe"]): c for c in rt.store.coverage(s)}
    assert summary[(AAPL, "1m")]["bars"] == 90 and summary[(AAPL, "1m")]["last"].startswith("2026-11-12T15:59")  # the summary follows without a recount

    before = rt.fake.requests
    again = rt.intraday.tick()  # the same minute: nothing new can exist, so nothing is asked
    assert again == {"stocks": {"bars": 0, "requests": 0, "failed": 0}, "crypto": {"bars": 0, "requests": 0, "failed": 0}} and rt.fake.requests == before
    rt.now[0] = NOW + timedelta(minutes=1)
    assert rt.intraday.tick()["stocks"]["bars"] == 1 and len(stored(rt, AAPL).bars) == 91  # one more minute, one more bar, no duplicate
    status = rt.intraday.status()["groups"]
    assert status["stocks"]["state"] == "ok" and status["stocks"]["feed"] == "alpaca:iex" and status["crypto"]["feed"] == "alpaca:crypto"
    assert status["stocks"]["bars"] == 14 and status["stocks"]["last_error"] == ""


def test_after_an_interruption_the_gap_is_fetched_and_extended_hours_are_left_out(rt):
    seed(rt, AAPL, "1m", "2026-11-10 14:30", "2026-11-10 20:59")   # last stored bar: Tuesday's close; the application was off since then
    seed(rt, MSFT, "1m", "2026-10-01 14:30", "2026-10-01 20:59")   # six weeks behind: more than a refresh reaches back
    out = rt.intraday.tick()
    aapl, msft = stored(rt, AAPL).bars, stored(rt, MSFT).bars
    # Wednesday's full session (390 minutes) and Thursday up to 15:59 (90 minutes); nothing outside 09:30-16:00 New York.
    assert len(aapl) == 390 + 390 + 90 and aapl.index.is_unique and in_session(aapl.index, get_calendar("XNYS")).all()
    assert aapl.index[-1] == ts("2026-11-12 15:59") and len(aapl["2026-11-11":"2026-11-11"]) == 390
    behind = next(c for c in rt.fake.calls if c["symbols"] == ["AAPL", "MSFT"] or c["symbols"] == ["MSFT", "AAPL"])
    assert behind["start"] == pd.Timestamp(NOW) - pd.Timedelta(days=7)  # MSFT's gap is capped at seven days...
    assert msft.index[0] == ts("2026-10-01 14:30") and msft[msft.index > ts("2026-10-02")].index[0] >= ts("2026-11-05 16:00")  # ...the older part waits for the history download
    new_msft = msft[msft.index > ts("2026-10-02")]
    assert out["stocks"]["bars"] == len(aapl) - 390 + len(new_msft), new_msft.groupby(new_msft.index.date).size().to_dict()


def test_a_failed_refresh_keeps_what_it_has_reports_the_error_and_recovers(rt):
    seed(rt, AAPL, "1m", "2026-11-12 14:30", "2026-11-12 15:49")
    rt.fake.fail = AlpacaBarsError("Alpaca rate limit reached")
    out = rt.intraday.tick()
    assert out["stocks"] == {"error": "Alpaca rate limit reached", "bars": 0} and len(stored(rt, AAPL).bars) == 80
    status = rt.intraday.status()["groups"]["stocks"]
    assert status["state"] == "error" and status["last_error"] == "Alpaca rate limit reached" and status["last_success"] is None
    assert rt.intraday.official(AAPL) is False
    rt.fake.fail = None
    rt.now[0] = NOW + timedelta(minutes=3)
    assert rt.intraday.tick()["stocks"]["bars"] == 13  # everything missed is fetched by the next successful refresh
    assert rt.intraday.status()["groups"]["stocks"]["state"] == "ok" and rt.intraday.official(AAPL) is True


def test_no_key_means_no_stock_bars_but_crypto_carries_on(rt):
    rt.set_provider_key("alpaca", None, "admin")
    seed(rt, AAPL, "1m", "2026-11-12 14:30", "2026-11-12 15:49")
    seed(rt, BTC, "1m", "2026-11-12 15:00", "2026-11-12 15:54", source="alpaca:crypto", stocks=False)
    out = rt.intraday.tick()
    assert out["stocks"] == {"skipped": "no Alpaca key"} and out["crypto"]["bars"] == 5
    assert rt.intraday.status()["groups"]["stocks"]["state"] == "no key"


def test_outside_the_session_stocks_are_asked_once_and_then_left_alone(rt):
    rt.now[0] = datetime(2026, 11, 14, 12, 0, 30, tzinfo=timezone.utc)  # Saturday
    seed(rt, AAPL, "1m", "2026-11-13 14:30", "2026-11-13 20:49")
    first = rt.intraday.tick()
    assert first["stocks"]["bars"] == 10  # the end of Friday's session, missed while the application was off
    rt.now[0] += timedelta(minutes=1)
    assert rt.intraday.tick()["stocks"] == {"skipped": "market closed"} and rt.intraday.status()["groups"]["stocks"]["state"] == "market closed"


def feed_tick(rt, iid, price, at, size=10.0):
    rt.live_hub.publish(Tick(iid, price, at, at, "manual-feed", FeedStatus.LIVE, "trade", size))


def test_followed_instruments_get_official_bars_through_the_engine_and_rebuilt_bars_are_only_a_fallback(rt):
    rt.live_subscribe([AAPL], "admin")
    out = rt.intraday.tick()   # nothing stored yet: the first refresh brings the last day of official bars
    first = stored(rt, AAPL)
    assert out["stocks"]["bars"] == len(first.bars) and first.bars.index[-1] == ts("2026-11-12 15:59") and sources(rt, AAPL) == {"alpaca:iex": len(first.bars)}
    assert rt.bars[(AAPL, "1m")].index[-1] == ts("2026-11-12 15:59")  # the engine's bar cache was fed, as it was by rebuilt bars
    assert rt.intraday.official(AAPL)

    # Live ticks for 16:00 and 16:01: the minute they complete is NOT stored from ticks while official bars are arriving.
    feed_tick(rt, AAPL, 500.0, NOW)
    feed_tick(rt, AAPL, 501.0, NOW + timedelta(seconds=45))
    assert stored(rt, AAPL).bars.index[-1] == ts("2026-11-12 15:59")
    rt.now[0] = NOW + timedelta(minutes=1)
    assert rt.intraday.tick()["stocks"]["bars"] == 1
    bar = stored(rt, AAPL).bars.loc[ts("2026-11-12 16:00")]
    assert bar["close"] == truth(ts("2026-11-12 16:00"), ts("2026-11-12 16:00"), "1m")["close"].iloc[0] and bar["close"] != 500.0  # Alpaca's bar, not the rebuilt one

    # Alpaca becomes unreachable for several minutes: rebuilt bars are stored again, labelled as such.
    rt.fake.fail = AlpacaBarsError("Alpaca could not be reached (ConnectError)")
    for minute in (2, 3, 4, 5):
        rt.now[0] = NOW + timedelta(minutes=minute)
        rt.intraday.tick()
        feed_tick(rt, AAPL, 600.0 + minute, rt.now[0])
    assert not rt.intraday.official(AAPL)
    labels = sources(rt, AAPL)
    assert labels["live:manual-feed"] >= 1 and stored(rt, AAPL).bars["close"].iloc[-1] > 600

    # It comes back: the published bars replace the rebuilt ones for the same minutes.
    rt.fake.fail = None
    rt.now[0] = NOW + timedelta(minutes=6)
    rt.intraday.tick()
    final = stored(rt, AAPL)
    assert sources(rt, AAPL) == {"alpaca:iex": len(final.bars)} and final.bars["close"].max() < 200 and final.bars.index.is_unique
    assert final.bars.index[-1] == ts("2026-11-12 16:05")


def test_late_official_bars_never_trade(rt, monkeypatch):
    rt.live_subscribe([BTC], "admin")
    seen = []
    real = rt.ingest

    def spy(instrument_id, timeframe, dataset, trade=True, **kw):
        seen.append((dataset.bars.index[0], dataset.bars.index[-1], trade))
        return real(instrument_id, timeframe, dataset, trade=trade, **kw)

    monkeypatch.setattr(rt, "ingest", spy)
    rt.intraday.tick()
    late = [s for s in seen if not s[2]]
    fresh = [s for s in seen if s[2]]
    assert late and late[0][1] < ts("2026-11-12 15:57") and len(fresh) == 1 and fresh[0][0] >= ts("2026-11-12 15:57")  # only the minutes that just closed act


# ---------------------------------------------------------------- history download
def test_history_is_downloaded_backwards_survives_an_interruption_and_is_not_fetched_twice(rt):
    rt.fake.page_rows = 2000
    rt.fake.fail_after = 3   # the connection drops after three pages
    rt.intraday.backfill([AAPL], ["1m"], months=1, actor="admin")
    rt.intraday.wait(60)
    job = rt.intraday.status()["history_job"]
    assert job["status"] == "finished" and job["failed"] == [{"series": f"{AAPL} 1m", "reason": "Alpaca could not be reached (ConnectError)"}]
    partial = stored(rt, AAPL).bars
    # What arrived is one contiguous block ending at the present: no hole in the middle of the history.
    assert partial.index[-1] == ts("2026-11-12 15:59") and partial.index[0] > ts("2026-10-20")
    per_day = partial.groupby(partial.index.date).size()
    assert (per_day.iloc[1:-1] == 390).all() and rt.intraday._history() == {}  # not recorded as complete

    rt.fake.fail_after = None
    rt.intraday.backfill([AAPL], ["1m"], months=1, actor="admin")
    rt.intraday.wait(60)
    job = rt.intraday.status()["history_job"]
    full = stored(rt, AAPL).bars
    assert job["status"] == "finished" and job["failed"] == [] and job["bars"] == len(full) - len(partial)  # only the missing part was downloaded
    resumed = rt.fake.calls[-2]
    assert resumed["newest_first"] and resumed["end"] == partial.index[0] - pd.Timedelta(minutes=1)
    assert full.index[0] >= ts("2026-10-12") and full.index[0] < ts("2026-10-14") and full.index.is_unique and in_session(full.index, get_calendar("XNYS")).all()
    days = full.groupby(full.index.date).size()
    assert (days.iloc[:-1] == 390).all() and sources(rt, AAPL) == {"alpaca:iex": len(full)}
    record = rt.intraday._history()[f"{AAPL}|1m"]
    assert record["feed"] == "alpaca:iex" and record["months"] == 1

    before = rt.fake.requests
    rt.intraday.backfill([AAPL], ["1m"], months=1, actor="admin")
    rt.intraday.wait(60)
    assert rt.intraday.status()["history_job"]["bars"] == 0 and rt.fake.requests - before <= 1  # nothing older is asked for again
    with pytest.raises(ValueError, match="Intraday timeframes"):
        rt.intraday.backfill([AAPL], ["1d"], 1, "admin")
    with pytest.raises(ValueError, match="covered"):
        rt.intraday.backfill(["STOCK:NOPE"], ["1m"], 1, "admin")


def test_consolidated_history_is_labelled_delayed_replaces_iex_bars_and_falls_back_when_refused(rt):
    seed(rt, AAPL, "5m", "2026-11-12 14:30", "2026-11-12 15:55")   # bars the minute refresh took from IEX
    rt.fake.shift = 1.0  # the consolidated tape shows (slightly) different prices
    rt.intraday.backfill([AAPL], ["5m"], months=1, actor="admin", feed="sip")
    rt.intraday.wait(60)
    labels = sources(rt, AAPL, "5m")
    data = stored(rt, AAPL, "5m")
    # Everything older than the 15-minute limit now comes from the consolidated tape; the newest bars are still IEX.
    assert labels["alpaca:iex"] == 3 and labels["alpaca:sip-delayed"] == len(data.bars) - 3 and data.source == "alpaca:iex+alpaca:sip-delayed"
    assert data.bars.index[-4] == ts("2026-11-12 15:40") and data.bars["open"].iloc[-4] == truth(ts("2026-11-12 15:40"), ts("2026-11-12 15:40"), "5m", 1.0)["open"].iloc[0]
    assert data.bars["open"].iloc[-3] == truth(ts("2026-11-12 15:45"), ts("2026-11-12 15:45"), "5m", 0.0)["open"].iloc[0]  # still the IEX bar
    with rt.db.session() as s:
        kinds = set(s.scalars(select(BarRow.kind).where(BarRow.instrument_id == AAPL, BarRow.source == "alpaca:sip-delayed")))
    assert kinds == {"delayed"}

    rt.fake.refuse_sip = True
    rt.intraday.backfill([MSFT], ["15m"], months=1, actor="admin", feed="sip")
    rt.intraday.wait(60)
    job = rt.intraday.status()["history_job"]
    assert job["failed"] == [] and job["feed"] == "iex" and "IEX history is downloaded instead" in job["notes"][0]
    assert set(sources(rt, MSFT, "15m")) == {"alpaca:iex"}


def test_freshness_reports_last_update_feed_and_age_per_instrument(rt):
    seed(rt, AAPL, "1m", "2026-11-12 14:30", "2026-11-12 15:49")
    seed(rt, BTC, "1m", "2026-11-12 15:00", "2026-11-12 15:40", source="live:alpaca-crypto", stocks=False)
    before = {r["instrument_id"]: r for r in rt.intraday.freshness()["instruments"]}
    assert before[AAPL]["state"] == "late" and before[AAPL]["age_seconds"] == 630.0 and before[AAPL]["last_update_kind"] == "newest stored bar"
    assert before[BTC]["state"] == "stale" and "Rebuilt from live crypto" in before[BTC]["series"][0]["feed"]
    rt.intraday.tick()
    out = rt.intraday.freshness()
    rows = {r["instrument_id"]: r for r in out["instruments"]}
    assert out["us_market_open"] is True and set(rows) == {AAPL, BTC}
    aapl = rows[AAPL]
    assert aapl["state"] == "current" and aapl["age_seconds"] == 0.0 and aapl["last_update_kind"] == "received" and aapl["official_bars"] is True
    assert aapl["feed"].startswith("IEX") and aapl["series"][0] == {"timeframe": "1m", "bars": 90, "last_bar_close": "2026-11-12T16:00:00+00:00", "age_seconds": 30.0,
                                                                    "source": "alpaca:iex", "feed": "IEX (one US exchange), official bars", "kind": "historical"}
    assert rows[BTC]["feed"].startswith("Alpaca crypto") and rows[BTC]["series"][0]["source"] == "alpaca:crypto"  # the rebuilt minutes were replaced by the published bars
    rt.now[0] = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    weekend = {r["instrument_id"]: r for r in rt.intraday.freshness()["instruments"]}
    assert weekend[AAPL]["state"] == "market closed" and weekend[BTC]["state"] == "stale"  # crypto never closes: silence there is a problem


def test_intraday_endpoints(client):
    runtime = client.app.state.container.runtime
    admin = login(client)
    assert client.get("/api/live/freshness").status_code == 401
    out = client.get("/api/live/freshness", headers=admin).json()
    assert out["instruments"] == [] and "us_market_open" in out
    status = client.get("/api/live/intraday", headers=admin).json()
    assert status["minute_update"] is True and status["history_job"] is None
    assert client.post("/api/live/intraday/backfill", headers=admin, json={"instrument_ids": ["STOCK:NOPE"], "timeframes": ["1m"]}).status_code == 400
    assert client.post("/api/live/intraday/backfill", headers=admin, json={"instrument_ids": ["STOCK:AAPL"], "timeframes": ["1m"], "feed": "nasdaq"}).status_code == 422
    fake = FakeBars(runtime.clock)
    runtime.intraday.client_factory = lambda group, key, secret, interval: fake
    started = client.post("/api/live/intraday/backfill", headers=admin, json={"instrument_ids": ["CRYPTO:BTC/USDT"], "timeframes": ["15m"], "months": 1})
    assert started.status_code == 400  # quoted in USDT: not one of Alpaca's USD pairs
    assert client.post("/api/live/intraday/refresh", headers=admin).json() == {}
    assert client.post("/api/live/intraday/cancel", headers=admin).status_code == 204
