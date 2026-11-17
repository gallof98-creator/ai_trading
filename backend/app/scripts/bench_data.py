"""Measure the data layer: bar store reads and writes, the Parquet lake, DuckDB queries and concurrent ingestion.

    python -m app.scripts.bench_data                       # SQLite in a temporary directory
    python -m app.scripts.bench_data --database-url postgresql+psycopg://qat:...@postgres:5432/qat --bars 200000

Against a real database it works on one scratch instrument id ("BENCH:DATA") and removes it afterwards.
Each timing is the best of `--repeat` runs. The ingestion test uses a local stand-in provider with a fixed
response time, so it measures the pipeline's concurrency and pacing, not any vendor's speed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time

import httpx
import numpy as np
import pandas as pd
from sqlalchemy import select

from ..database.base import Database
from ..database.models import BarRow
from ..datahub.http import ApiClient
from ..datahub.lake import DataLake, now_utc
from ..market_data.quality import DataKind
from ..market_data.sources import Dataset
from ..market_data.store import BarStore
from ..market_data.timeframes import OHLCV

INSTRUMENT = "BENCH:DATA"


def best(fn, repeat: int) -> tuple[float, object]:
    out, times = None, []
    for _ in range(repeat):
        started = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - started)
    return min(times), out


def make_bars(n: int) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0005))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.standard_normal(n)) * 0.0003
    index = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC", name="ts")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + spread), "low": np.minimum(open_, close) * (1 - spread),
                         "close": close, "volume": rng.lognormal(3, 1, n)}, index=index)


def load_with_mapped_objects(session, instrument_id: str, timeframe: str) -> pd.DataFrame:
    """How BarStore.load read bars before this change (one mapped object per bar), kept here as the reference."""
    rows = session.scalars(select(BarRow).where(BarRow.instrument_id == instrument_id, BarRow.timeframe == timeframe).order_by(BarRow.ts_ms)).all()
    idx = pd.to_datetime([r.ts_ms for r in rows], unit="ms", utc=True).rename("ts")
    return pd.DataFrame([[r.open, r.high, r.low, r.close, r.volume] for r in rows], index=idx, columns=OHLCV)


async def simulated_ingestion(series: int, latency: float, concurrency: int, interval: float) -> float:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(latency)
        return httpx.Response(200, json={"ok": True})

    gate = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    async with ApiClient("bench", "https://bench.invalid", interval=interval, transport=httpx.MockTransport(handler)) as client:
        async def one(i: int) -> None:
            async with gate:
                await client.get_json(f"series/{i}")

        await asyncio.gather(*(one(i) for i in range(series)))
    return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bars", type=int, default=200_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--database-url", default="")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        url = args.database_url or f"sqlite:///{tmp}/bench.db"
        db = Database(url)
        if not args.database_url:
            db.create_all()
        store, lake = BarStore(), DataLake(os.path.join(tmp, "lake"))
        bars = make_bars(args.bars)
        results: dict = {"bars": args.bars, "database": url.split(":", 1)[0], "repeat": args.repeat}
        try:
            with db.session() as session:
                store.delete_series(session, INSTRUMENT)
            started = time.perf_counter()
            with db.session() as session:
                store.save(session, INSTRUMENT, "1m", Dataset(bars, DataKind.HISTORICAL, "bench"))
            results["bar_store_write_s"] = round(time.perf_counter() - started, 3)

            with db.session() as session:
                before, reference = best(lambda: load_with_mapped_objects(session, INSTRUMENT, "1m"), args.repeat)
                after, loaded = best(lambda: store.load(session, INSTRUMENT, "1m"), args.repeat)
            pd.testing.assert_frame_equal(reference, loaded.bars, check_exact=True)  # the faster read returns exactly the same frame
            results["bar_store_read_before_s"], results["bar_store_read_after_s"] = round(before, 3), round(after, 3)
            results["bar_store_read_speedup"] = round(before / after, 2)

            rows = bars.reset_index()
            rows["source"], rows["symbol"], rows["timeframe"], rows["received_at"] = "bench", "BENCH/USD", "1m", now_utc()
            started = time.perf_counter()
            lake.write("bars", rows)
            results["lake_write_s"] = round(time.perf_counter() - started, 3)
            read, frame = best(lambda: lake.bars("bench", "BENCH/USD", "1m"), args.repeat)
            np.testing.assert_allclose(frame[OHLCV].to_numpy(), bars[OHLCV].to_numpy())
            results["lake_read_s"] = round(read, 3)
            month = pd.Timestamp("2024-02-01", tz="UTC")
            ranged, _ = best(lambda: lake.bars("bench", "BENCH/USD", "1m", month, month + pd.Timedelta(days=7)), args.repeat)
            with db.session() as session:
                ranged_db, _ = best(lambda: store.load(session, INSTRUMENT, "1m", month, month + pd.Timedelta(days=7)), args.repeat)
            results["week_range_read_bar_store_s"], results["week_range_read_lake_s"] = round(ranged_db, 4), round(ranged, 4)
            results["lake_bytes"] = lake.stats("bars", source="bench")["bytes"]
            results["lake_bytes_per_bar"] = round(results["lake_bytes"] / args.bars, 2)
            if not args.database_url:
                results["sqlite_bytes"] = os.path.getsize(os.path.join(tmp, "bench.db"))

            files = lake.files("bars", source="bench")

            def hourly_duckdb():
                with lake.connect(views=False) as con:
                    return con.execute("SELECT time_bucket(INTERVAL 1 HOUR, ts) AS h, first(open ORDER BY ts), max(high), min(low), last(close ORDER BY ts), "
                                       "sum(volume) FROM read_parquet(?) GROUP BY h ORDER BY h", [files]).fetchall()

            def hourly_pandas():
                with db.session() as session:
                    frame = store.load(session, INSTRUMENT, "1m").bars
                return frame.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})

            duck, a = best(hourly_duckdb, args.repeat)
            pandas_, b = best(hourly_pandas, args.repeat)
            assert len(a) == len(b.dropna())
            results["hourly_aggregate_bar_store_pandas_s"], results["hourly_aggregate_lake_duckdb_s"] = round(pandas_, 3), round(duck, 3)

            for concurrency in (1, 4, 8):
                results[f"ingest_40_series_50ms_concurrency_{concurrency}_s"] = round(asyncio.run(simulated_ingestion(40, 0.05, concurrency, 0.0)), 3)
            results["ingest_40_series_50ms_concurrency_8_paced_10_per_s"] = round(asyncio.run(simulated_ingestion(40, 0.05, 8, 0.1)), 3)
        finally:
            with db.session() as session:
                store.delete_series(session, INSTRUMENT)
            db.engine.dispose()
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
