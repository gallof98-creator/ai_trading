"""Data-quality checks and the versioned feature sets: causality, point-in-time correctness, no silent merging."""
import numpy as np
import pandas as pd
import pytest

from app.datahub import features, quality
from app.datahub.lake import DataLake


def ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC").as_unit("us")


def make_bars(n: int = 3000, seed: int = 3, start: str = "2024-03-04 00:00", vwap: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0008))
    open_ = np.concatenate([[100.0], close[:-1]])
    frame = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.0004, "low": np.minimum(open_, close) * 0.9996, "close": close,
                          "volume": rng.lognormal(2, 0.6, n)}, index=pd.date_range(ts(start), periods=n, freq="1min", name="ts"))
    if vwap:
        frame["vwap"] = (frame["high"] + frame["low"]) / 2
    return frame


# ---------------------------------------------------------------- quality
def test_bar_checks_remove_and_count_bad_rows_and_report_gaps_and_outliers():
    frame = make_bars(600).reset_index()
    frame.loc[100, "high"] = frame.loc[100, "low"] - 1  # impossible bar
    frame.loc[200, "close"] = np.nan
    frame = frame.drop(index=[300, 301, 302])  # a three-bar gap
    frame.loc[400, ["open", "high", "low", "close"]] = frame.loc[400, ["open", "high", "low", "close"]] * 1.25  # a 25% spike: well formed, suspicious
    spike = frame.loc[400, "ts"]
    frame = pd.concat([frame, frame.iloc[[10, 11]], frame.iloc[[50]].assign(ts=frame["ts"].iloc[50] + pd.Timedelta(seconds=7))])
    clean, report = quality.check_bars(frame, "1m")
    assert report["rows_in"] == 600 and report["duplicates_removed"] == 2 and report["invalid_rows_removed"] == 2 and report["off_grid_removed"] == 1
    assert report["missing_bars"] == 5 and report["ok"] is False  # the gap plus the two rows removed as invalid
    assert report["outliers"] >= 1 and any(e["ts"] == spike.isoformat() for e in report["outlier_examples"])
    assert len(clean) == report["rows_out"] == 595 and clean["ts"].is_unique and clean["ts"].is_monotonic_increasing
    assert spike in set(clean["ts"])  # the outlier is reported, not deleted: it may be real
    good, ok = quality.check_bars(make_bars(300).reset_index(), "1m")
    assert ok["ok"] is True and ok["outliers"] == 0 and len(good) == 300
    assert quality.check_bars(frame.iloc[0:0], "1m")[1]["rows_out"] == 0


def test_stock_bars_are_checked_against_the_exchange_session():
    index = pd.date_range(ts("2024-03-04 14:30"), ts("2024-03-04 20:59"), freq="1min")  # one full New York session
    frame = pd.DataFrame({"ts": index, "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 5.0})
    assert quality.check_bars(frame, "1m", "XNYS")[1]["missing_bars"] == 0
    assert quality.check_bars(frame.drop(index=range(60, 70)), "1m", "XNYS")[1]["missing_bars"] == 10


def test_generic_row_checks_freshness_and_continuity():
    frame = pd.DataFrame({"id": ["a", "a", "b", "c"], "value": [1.0, 2.0, np.nan, 4.0], "ts": [ts("2024-01-02"), ts("2024-01-02"), ts("2024-01-01"), ts("2024-01-03")]})
    clean, report = quality.check_rows(frame, ["id"], "value", "ts")
    assert (report["duplicates_removed"], report["invalid_rows_removed"], report["rows_out"], report["ok"]) == (1, 1, 2, False)
    assert list(clean["value"]) == [2.0, 4.0]  # of two rows with one key, the later one is kept
    now = ts("2024-01-01 12:00")
    assert quality.freshness(now - pd.Timedelta(seconds=90), 60, now) == {"age_seconds": 90.0, "stale": False}
    assert quality.freshness(now - pd.Timedelta(seconds=200), 60, now)["stale"] is True
    assert quality.freshness(None, 60, now) == {"age_seconds": None, "stale": None}
    assert quality.continuity(ts("2024-01-01"), ts("2024-01-01 01:39"), 90, "1m") == {"expected": 100, "coverage": 0.9}
    assert not quality.robust_outliers(pd.Series([1.0] * 100)).any()  # constant series: no scale, nothing flagged


# ---------------------------------------------------------------- bar features
def test_every_feature_set_is_registered_with_a_version():
    described = {d["name"]: d for d in features.describe()}
    assert set(described) == {"bars_intraday", "cross_venue", "cross_asset_correlation", "orderbook", "trade_flow", "macro"}
    assert all(d["id"] == f"{d['name']}@v{d['version']}" and d["columns"] and d["description"] for d in described.values())
    assert features.compute("bars_intraday", make_bars(100)).attrs["feature_set"] == "bars_intraday@v1"


@pytest.mark.parametrize("vwap", [False, True])
def test_bar_features_never_use_later_bars(vwap):
    """Cutting the future off must not change any value already computed: nothing looks ahead."""
    bars = make_bars(3000, vwap=vwap)
    full = features.compute("bars_intraday", bars, rv_window=30, rvol_sessions=1, periods_per_year=525_600)
    for cut in (800, 1441, 2500):
        part = features.compute("bars_intraday", bars.iloc[:cut], rv_window=30, rvol_sessions=1, periods_per_year=525_600)
        pd.testing.assert_frame_equal(part, full.iloc[:cut], check_exact=False, rtol=1e-12)


def test_bar_feature_values():
    bars = make_bars(3 * 1440)
    f = features.compute("bars_intraday", bars, rv_window=20)
    assert f["ret_1"].iloc[5] == pytest.approx(np.log(bars["close"].iloc[5] / bars["close"].iloc[4]))
    # Session VWAP restarts at midnight UTC: the first bar of a day has only itself in it.
    first_of_day = bars.index.get_loc(ts("2024-03-05 00:00"))
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    assert f["vwap_session"].iloc[first_of_day] == pytest.approx(typical.iloc[first_of_day])
    day = bars.loc["2024-03-05"]
    assert f["vwap_session"].loc["2024-03-05"].iloc[-1] == pytest.approx((((day["high"] + day["low"] + day["close"]) / 3) * day["volume"]).sum() / day["volume"].sum())
    assert f["rv"].iloc[100] == pytest.approx(np.sqrt((f["ret_1"].iloc[81:101] ** 2).sum()))
    assert f["rvol_roll"].iloc[100] == pytest.approx(bars["volume"].iloc[100] / bars["volume"].iloc[80:100].mean())
    assert f["rvol_tod"].iloc[:5 * 1440 // 5].isna().all()  # needs five earlier sessions of the same minute: none in the first days
    assert "rv_ann" not in f.columns and not f["bar_gap"].any()
    gappy = features.compute("bars_intraday", bars.drop(bars.index[500:520]))
    assert gappy["bar_gap"].sum() == 1


def test_cross_venue_divergence_uses_only_common_timestamps():
    a = make_bars(600, seed=1)["close"]
    b = (a * 1.001).drop(a.index[100:150])  # the second venue is 10 bp higher and has a gap
    f = features.compute("cross_venue", {"binance:BTC/USDT": a, "kraken:BTC/USD": b}, window=50)
    assert len(f) == 550 and not f.index.isin(a.index[100:150]).any()  # a missing bar is not filled from the other venue
    assert f["div_bps_binance:BTC/USDT__kraken:BTC/USD"].iloc[0] == pytest.approx((1 / 1.001 - 1) * 1e4)
    corr = features.compute("cross_asset_correlation", {"x": a, "y": a * 2, "z": make_bars(600, seed=9)["close"]}, window=60)
    assert corr["corr_x__y"].iloc[-1] == pytest.approx(1.0) and abs(corr["corr_x__z"].iloc[-1]) < 0.6 and corr["corr_x__y"].iloc[:59].isna().all()


def test_order_book_and_trade_flow_features():
    book = pd.DataFrame({"ts": [ts("2024-03-04 10:00")], "best_bid": [100.0], "best_ask": [100.5], "bid_px": [[100.0, 99.5]], "bid_sz": [[1.0, 2.0]],
                         "ask_px": [[100.5, 101.0]], "ask_sz": [[3.0, 3.0]]})
    f = features.compute("orderbook", book).iloc[0]
    assert f["mid"] == 100.25 and f["spread_bps"] == pytest.approx(0.5 / 100.25 * 1e4)
    assert f["imbalance_1"] == pytest.approx(-0.5) and f["imbalance_all"] == pytest.approx((3 - 6) / 9)
    assert f["microprice"] == pytest.approx((100.0 * 3 + 100.5 * 1) / 4)  # more size on the ask: fair value sits nearer the bid
    trades = pd.DataFrame({"ts": [ts("2024-03-04 10:00:05"), ts("2024-03-04 10:00:40"), ts("2024-03-04 10:01:10"), ts("2024-03-04 10:02:30")],
                           "price": [100.0, 101.0, 102.0, 103.0], "amount": [1.0, 3.0, 2.0, 5.0], "side": ["buy", "sell", "buy", "buy"]})
    flow = features.compute("trade_flow", trades, "1m")
    assert list(flow.index) == [ts("2024-03-04 10:00"), ts("2024-03-04 10:01")]  # the last, possibly unfinished, bar is left out
    first = flow.iloc[0]
    assert (first["volume"], first["buy_volume"], first["sell_volume"], first["trades"]) == (4.0, 1.0, 3.0, 2)
    assert first["volume_imbalance"] == pytest.approx(-0.5) and first["vwap"] == pytest.approx((100 + 303) / 4)
    assert len(features.compute("trade_flow", trades, "1m", drop_incomplete=False)) == 3


# ---------------------------------------------------------------- macro, point in time
def macro_rows() -> pd.DataFrame:
    def row(series, date, value, published, kind="vintage"):
        return {"source": "fred", "series_id": series, "date": ts(date), "value": value, "realtime_start": ts(published[:10]), "available_at": ts(published),
                "availability": kind}

    rows = [row("CPIAUCSL", "2023-01-01", 299.170, "2023-02-14 23:00"), row("CPIAUCSL", "2023-02-01", 300.840, "2023-03-14 22:00"),
            row("CPIAUCSL", "2024-01-01", 308.417, "2024-02-13 23:00"), row("CPIAUCSL", "2024-01-01", 309.685, "2024-03-12 22:00"),
            row("CPIAUCSL", "2024-02-01", 310.326, "2024-03-12 22:00"),
            # A revision of an OLD observation published later: must not change "the latest value"
            row("CPIAUCSL", "2023-01-01", 299.500, "2024-03-20 22:00")]
    for day, ten, two in (("2024-03-07", 4.09, 4.50), ("2024-03-08", 4.09, 4.48), ("2024-03-11", 4.10, 4.51), ("2024-03-13", 4.19, 4.61)):
        published = (pd.Timestamp(day) + pd.offsets.BDay(1) + pd.Timedelta(hours=22)).strftime("%Y-%m-%d %H:%M")
        rows += [row("DGS10", day, ten, published, "lag_rule"), row("DGS2", day, two, published, "lag_rule")]
    return pd.DataFrame(rows)


def test_macro_values_appear_only_once_published():
    index = pd.DatetimeIndex([ts("2024-02-13 22:59"), ts("2024-02-13 23:00"), ts("2024-03-12 21:59"), ts("2024-03-12 22:00"), ts("2024-03-25")], name="ts")
    f = features.compute("macro", macro_rows(), index)
    assert f.attrs["feature_set"] == "macro@v1"
    # One minute before January CPI is published, the newest figure is still February 2023's.
    assert list(f["CPIAUCSL"]) == [300.840, 308.417, 308.417, 310.326, 310.326]
    assert f["CPIAUCSL_age_days"].iloc[1] == 0 and f["CPIAUCSL_age_days"].iloc[2] == pytest.approx(27.9583, abs=1e-3)
    # Year-on-year uses the figures published at the time: the first print before the revision, never the revised one early.
    assert np.isnan(f["CPIAUCSL_yoy"].iloc[0])
    assert f["CPIAUCSL_yoy"].iloc[1] == pytest.approx(308.417 / 299.170 - 1)
    assert f["CPIAUCSL_yoy"].iloc[3] == pytest.approx(310.326 / 300.840 - 1)
    # A day's yields are known the next business evening: at 21:59 on Tuesday the newest are still Friday's.
    assert f["DGS10"].iloc[2] == 4.09 and f["yc_10y_2y"].iloc[2] == pytest.approx(4.09 - 4.48)
    assert f["DGS10"].iloc[3] == 4.10 and f["yc_10y_2y"].iloc[3] == pytest.approx(4.10 - 4.51) and f["DGS10_chg_1"].iloc[3] == pytest.approx(0.01)
    assert f["DGS10"].iloc[4] == 4.19 and f["DGS10_chg_1"].iloc[4] == pytest.approx(0.09)
    assert np.isnan(f["DGS10"].iloc[0]) and np.isnan(f["yc_10y_2y"].iloc[0])  # nothing is known before the first publication


def test_macro_features_do_not_change_when_later_publications_are_removed():
    rows = macro_rows()
    index = pd.date_range(ts("2024-02-01"), ts("2024-03-30"), freq="6h", name="ts")
    full = features.compute("macro", rows, index)
    for cut in (ts("2024-02-20"), ts("2024-03-12 22:00"), ts("2024-03-15")):
        past = features.compute("macro", rows[rows["available_at"] <= cut], index[index <= cut])
        pd.testing.assert_frame_equal(past, full.loc[index <= cut, past.columns], check_exact=False, rtol=1e-12)


def test_scheduled_release_times_can_replace_the_conservative_evening():
    rows = macro_rows()
    index = pd.DatetimeIndex([ts("2024-02-13 13:29"), ts("2024-02-13 13:30")], name="ts")  # 08:29 and 08:30 New York
    conservative = features.compute("macro", rows, index)
    scheduled = features.compute("macro", rows, index, release_times={"CPIAUCSL": "08:30"})
    assert list(conservative["CPIAUCSL"]) == [300.840, 300.840] and list(scheduled["CPIAUCSL"]) == [300.840, 308.417]


def test_latest_known_ignores_revisions_of_older_observations():
    cpi = macro_rows().query("series_id == 'CPIAUCSL'")
    state = features.latest_known(cpi)
    assert list(state["value"]) == [299.170, 300.840, 308.417, 310.326]  # the 20 March revision of January 2023 changes nothing here
    change = features.change_known(cpi, 12)
    assert change["value"].iloc[-1] == pytest.approx(310.326 / 300.840 - 1)


# ---------------------------------------------------------------- fundamentals, point in time; materialised features
def test_fundamentals_show_a_restatement_only_from_its_own_filing():
    def fact(value, accession, available):
        return {"concept": "Revenues", "unit": "USD", "period_start": ts("2023-10-01"), "period_end": ts("2023-12-30"), "value": value,
                "accession": accession, "available_at": ts(available)}

    facts = pd.DataFrame([fact(119575e6, "a-24", "2024-02-01 23:03"), fact(119600e6, "a-25", "2025-01-31 23:00"),
                          {**fact(364980e6, "a-24k", "2024-11-01 10:01"), "concept": "Assets", "period_start": ts("2024-09-28"), "period_end": ts("2024-09-28")}])
    assert features.fundamentals_asof(facts, ts("2024-02-01 23:00")).empty
    mid = features.fundamentals_asof(facts, ts("2024-06-30"))
    assert list(mid["value"]) == [119575e6]
    late = features.fundamentals_asof(facts, ts("2025-02-01"))
    assert dict(zip(late["concept"], late["value"])) == {"Assets": 364980e6, "Revenues": 119600e6}
    assert list(features.fundamentals_asof(facts, ts("2025-02-01"), ["Assets"])["concept"]) == ["Assets"]


def test_features_can_be_stored_under_their_versioned_id(tmp_path):
    lake = DataLake(tmp_path)
    frame = features.compute("bars_intraday", make_bars(200))
    result = features.materialize(lake, "bars_intraday", frame, "binance", "1m", "BTC/USDT")
    assert result.rows_new == 200
    back = lake.read("features", feature_set="bars_intraday@v1", source="binance", timeframe="1m", symbol="BTC/USDT")
    assert len(back) == 200 and back["ret_1"].iloc[5] == pytest.approx(frame["ret_1"].iloc[5]) and set(back["feature_set"]) == {"bars_intraday@v1"}
    assert lake.read("features", feature_set="bars_intraday@v2").empty  # another version is another series
