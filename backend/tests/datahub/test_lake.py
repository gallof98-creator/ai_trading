"""The Parquet lake: idempotent writes, revisions counted, sources kept apart, time filters, DuckDB views."""
import threading

import numpy as np
import pandas as pd
import pytest

from app.datahub.lake import DataLake, now_utc, safe_part, utc


def bars(n: int, start: str = "2024-01-31 23:00", source: str = "binance", symbol: str = "BTC/USDT", base: float = 100.0, timeframe: str = "1m") -> pd.DataFrame:
    ts = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq="1min" if timeframe == "1m" else "1D")
    close = base + np.arange(n) * 0.1
    return pd.DataFrame({"ts": ts, "open": close - 0.05, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 3.0, "source": source,
                         "symbol": symbol, "timeframe": timeframe, "received_at": now_utc()})


def test_write_then_read_round_trips_and_buckets_by_month(tmp_path):
    lake = DataLake(tmp_path)
    frame = bars(120)  # crosses from January into February
    result = lake.write("bars", frame)
    assert (result.rows_in, result.rows_new, result.rows_revised, result.files) == (120, 120, 0, 2)
    assert sorted(p.name for p in (tmp_path / "bars" / "binance" / "1m" / "BTC-USDT").glob("*.parquet")) == ["2024-01.parquet", "2024-02.parquet"]
    back = lake.read("bars", source="binance", symbol="BTC/USDT", timeframe="1m")
    assert len(back) == 120 and back["ts"].is_monotonic_increasing and str(back["ts"].dtype) == "datetime64[us, UTC]"
    np.testing.assert_allclose(back["close"].to_numpy(), frame["close"].to_numpy())
    assert set(back["symbol"]) == {"BTC/USDT"}  # the real symbol is kept in the file; only the directory name is sanitised
    assert back["ingested_at"].notna().all() and back["received_at"].notna().all()
    indexed = lake.bars("binance", "BTC/USDT", "1m")
    assert indexed.index.name == "ts" and list(indexed.columns[:5]) == ["open", "high", "low", "close", "volume"]


def test_rewriting_is_idempotent_and_a_changed_value_is_counted_as_a_revision(tmp_path):
    lake = DataLake(tmp_path)
    frame = bars(50, start="2024-03-01 00:00")
    lake.write("bars", frame)
    first = lake.read("bars", source="binance")
    again = lake.write("bars", frame.assign(received_at=now_utc()))
    assert (again.rows_new, again.rows_revised, again.rows_unchanged, again.files) == (0, 0, 50, 0)  # nothing rewritten
    pd.testing.assert_frame_equal(lake.read("bars", source="binance"), first)  # the original arrival timestamps are kept

    changed = frame.iloc[40:].copy()
    changed.loc[changed.index[0], "close"] += 1.0
    extra = bars(5, start="2024-03-01 00:50")
    result = lake.write("bars", pd.concat([changed, extra]))
    assert (result.rows_new, result.rows_revised, result.rows_unchanged) == (5, 1, 9)
    now = lake.read("bars", source="binance")
    assert len(now) == 55 and now["ts"].is_unique
    assert now.loc[now["ts"] == frame["ts"].iloc[40], "close"].item() == pytest.approx(frame["close"].iloc[40] + 1.0)


def test_sources_are_never_merged(tmp_path):
    """The same moment observed by two venues stays two rows in two files; reading one source never returns the other."""
    lake = DataLake(tmp_path)
    lake.write("bars", bars(10, source="binance", symbol="BTC/USDT", base=100))
    lake.write("bars", bars(10, source="kraken", symbol="BTC/USD", base=101))
    assert lake.read("bars", source="binance")["close"].iloc[0] == 100 and lake.read("bars", source="kraken")["close"].iloc[0] == 101
    everything = lake.read("bars")
    assert len(everything) == 20 and everything.groupby("ts")["source"].nunique().eq(2).all()
    assert lake.series("bars") == [{"source": "binance", "timeframe": "1m", "symbol": "BTC-USDT"}, {"source": "kraken", "timeframe": "1m", "symbol": "BTC-USD"}]


def test_time_filters_prune_files_and_rows(tmp_path):
    lake = DataLake(tmp_path)
    lake.write("bars", bars(3 * 1440, start="2024-01-30 00:00"))
    start, end = pd.Timestamp("2024-02-01 00:00", tz="UTC"), pd.Timestamp("2024-02-01 00:09", tz="UTC")
    assert [p.rsplit("/", 1)[1] for p in lake.files("bars", start, end, source="binance")] == ["2024-02.parquet"]
    window = lake.read("bars", start, end, source="binance")
    assert len(window) == 10 and window["ts"].iloc[0] == start and window["ts"].iloc[-1] == end
    newest = lake.read("bars", source="binance", limit=3, newest_first=True, columns=["ts", "close"])
    assert list(newest.columns) == ["ts", "close"] and newest["ts"].is_monotonic_decreasing and len(newest) == 3
    stats = lake.stats("bars", source="binance", symbol="BTC/USDT", timeframe="1m")
    assert stats["rows"] == 3 * 1440 and stats["first"] == pd.Timestamp("2024-01-30", tz="UTC") and stats["files"] == 2 and stats["bytes"] > 0
    assert lake.stats("bars", source="nobody") is None and lake.read("bars", source="nobody").empty


def test_daily_bars_are_bucketed_by_year_and_macro_in_one_file(tmp_path):
    lake = DataLake(tmp_path)
    lake.write("bars", bars(400, start="2023-06-01", timeframe="1d"))
    assert sorted(p.name for p in (tmp_path / "bars" / "binance" / "1d" / "BTC-USDT").glob("*.parquet")) == ["2023.parquet", "2024.parquet"]
    day = pd.Timestamp("2024-01-01", tz="UTC")
    macro = pd.DataFrame({"source": "fred", "series_id": "CPIAUCSL", "date": [day, day], "value": [308.4, 309.7],
                          "realtime_start": [pd.Timestamp("2024-02-13", tz="UTC"), pd.Timestamp("2024-03-12", tz="UTC")]})
    assert lake.write("macro", macro).rows_new == 2  # two vintages of one observation are two rows
    assert [p.name for p in (tmp_path / "macro" / "fred" / "CPIAUCSL").glob("*.parquet")] == ["all.parquet"]
    assert lake.write("macro", macro.assign(value=[308.4, 309.9])).rows_revised == 1


def test_bad_input_is_refused(tmp_path):
    lake = DataLake(tmp_path)
    with pytest.raises(ValueError, match="missing columns"):
        lake.write("bars", bars(3).drop(columns="symbol"))
    naive = bars(3)
    naive["ts"] = naive["ts"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        lake.write("bars", naive)
    with pytest.raises(ValueError, match="timezone-naive"):
        utc(pd.Series(pd.to_datetime(["2024-01-01"])))
    with pytest.raises(ValueError):
        lake.files("bars", venue="x")
    assert safe_part("BTC/USDT") == "BTC-USDT" and safe_part("../../etc") == "-..-etc"
    with pytest.raises(ValueError):
        safe_part("..")
    assert lake.write("bars", bars(0)).rows_in == 0


def test_duckdb_views_join_datasets(tmp_path):
    lake = DataLake(tmp_path)
    lake.write("bars", bars(60, source="binance", base=100))
    lake.write("bars", bars(60, source="kraken", symbol="BTC/USD", base=100.5))
    with lake.connect() as con:
        assert {r[0] for r in con.execute("SHOW TABLES").fetchall()} == {"bars"}
        rows = con.execute("SELECT a.ts, b.close - a.close FROM bars a JOIN bars b USING (ts) WHERE a.source = 'binance' AND b.source = 'kraken' ORDER BY 1").fetchall()
        assert len(rows) == 60 and rows[0][1] == pytest.approx(0.5) and rows[0][0].tzinfo is not None
        hourly = con.execute("SELECT count(*), max(high) FROM bars WHERE source = 'binance' GROUP BY time_bucket(INTERVAL 1 HOUR, ts) ORDER BY 1").fetchall()
        assert sum(r[0] for r in hourly) == 60


def test_concurrent_writers_to_one_series_lose_nothing(tmp_path):
    lake = DataLake(tmp_path)
    chunks = [bars(60, start=f"2024-05-01 {h:02d}:00") for h in range(8)]
    threads = [threading.Thread(target=lake.write, args=("bars", c)) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    back = lake.read("bars", source="binance")
    assert len(back) == 480 and back["ts"].is_unique
    assert not list((tmp_path / "bars").rglob("*.tmp"))  # no half-written file is left behind
