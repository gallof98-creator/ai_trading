"""Historical data lake: Parquet files on local disk, read with DuckDB (in-process, no server).

Layout: <root>/<dataset>/<part>/<part>/.../<bucket>.parquet, where the parts are the columns named in the
dataset's `parts` (for bars: source / timeframe / symbol) and the bucket is a calendar period of the
event time. Data from different sources never shares a file, so two providers' observations of the
same thing are never merged; comparing them is an explicit query.

Timestamps (all UTC, microsecond resolution) kept on every row:
    ts            when the event happened (a bar's open time, a trade, an observation's publication ...)
    received_at   when the response carrying the row arrived from the provider
    ingested_at   when the row was written here
    available_at  when the information became public, on datasets where that differs from `ts`
                  (macro releases and revisions, company filings); backtests must join on this one
"""
from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..market_data.timeframes import TIMEFRAMES

STAMPS = ("received_at", "ingested_at")


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    keys: tuple[str, ...]  # identify a row within one series
    parts: tuple[str, ...]  # columns that identify a series; they form the directory path
    time: str = "ts"  # event-time column used for buckets and time filters
    bucket: str = "year"  # year | month | day | auto (month below one day, year otherwise) | none
    compare: tuple[str, ...] = ()  # value columns; a re-fetched row that differs in these counts as a revision
    description: str = ""


DATASETS: dict[str, DatasetSpec] = {s.name: s for s in (
    DatasetSpec("bars", ("ts",), ("source", "timeframe", "symbol"), bucket="auto", compare=("open", "high", "low", "close", "volume"),
                description="OHLCV bars per source and venue symbol; ts is the bar's open time"),
    DatasetSpec("trades", ("ts", "trade_id"), ("source", "symbol"), bucket="day", description="Individual trades of a venue"),
    DatasetSpec("orderbook", ("ts",), ("source", "symbol"), bucket="day", description="Order book snapshots (top levels) of a venue"),
    DatasetSpec("macro", ("series_id", "date", "realtime_start"), ("source", "series_id"), time="date", bucket="none", compare=("value",),
                description="Macroeconomic observations with their vintages; available_at is the publication time"),
    DatasetSpec("fundamentals", ("concept", "unit", "period_start", "period_end", "accession"), ("source", "cik"), time="period_end", bucket="none",
                compare=("value",), description="Reported company facts (XBRL) with the filing that disclosed them"),
    DatasetSpec("filings", ("accession",), ("source", "cik"), time="ts", bucket="none", description="Company filing index; ts is the acceptance time"),
    DatasetSpec("crypto_market", ("ts",), ("source", "coin_id"), bucket="year", compare=("price", "market_cap", "volume"),
                description="Aggregated (cross-exchange) price, market capitalisation and volume per coin"),
    DatasetSpec("crypto_venues", ("ts", "venue", "pair"), ("source", "coin_id"), bucket="month",
                description="Per-exchange volume, last price and spread for a coin (cross-exchange liquidity snapshots)"),
    DatasetSpec("company_profile", ("ts", "symbol"), ("source",), bucket="year", description="Company reference data snapshots"),
    DatasetSpec("features", ("ts",), ("feature_set", "source", "timeframe", "symbol"), bucket="auto", description="Materialised feature sets"),
)}


def utc(values) -> pd.Series | pd.DatetimeIndex:
    """Timezone-aware UTC timestamps at microsecond resolution. Naive input is refused: guessing a timezone shifts every row."""
    out = pd.to_datetime(values, utc=False)
    tz = out.dt.tz if isinstance(out, pd.Series) else out.tz
    if tz is None:
        raise ValueError("timestamps are timezone-naive; state the source timezone explicitly")
    return (out.dt.tz_convert("UTC").dt.as_unit("us") if isinstance(out, pd.Series) else out.tz_convert("UTC").as_unit("us"))


def now_utc() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc)).as_unit("us")


def safe_part(value) -> str:
    """A directory name for a series key. The real value stays in the file's own column."""
    text = re.sub(r"[^A-Za-z0-9._-]", "-", str(value)).strip(".")
    if not text:
        raise ValueError(f"cannot build a path from {value!r}")
    return text


def _bucket(spec: DatasetSpec, frame: pd.DataFrame) -> pd.Series:
    kind = spec.bucket
    if kind == "none":
        return pd.Series("all", index=frame.index)
    ts = frame[spec.time]
    if kind == "auto":
        tf = frame["timeframe"].iloc[0] if "timeframe" in frame.columns else "1d"
        kind = "month" if tf in TIMEFRAMES and TIMEFRAMES[tf] < pd.Timedelta(days=1) else "year"
    fmt = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}[kind]
    return ts.dt.strftime(fmt)


@dataclass
class WriteResult:
    rows_in: int = 0
    rows_new: int = 0
    rows_revised: int = 0
    rows_unchanged: int = 0
    files: int = 0

    def add(self, other: "WriteResult") -> None:
        for name in ("rows_in", "rows_new", "rows_revised", "rows_unchanged", "files"):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def to_dict(self) -> dict:
        return {"rows_in": self.rows_in, "rows_new": self.rows_new, "rows_revised": self.rows_revised, "rows_unchanged": self.rows_unchanged,
                "files_written": self.files}


class DataLake:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._local = threading.Lock()

    # ------------------------------------------------------------------ writing
    @contextmanager
    def _locked(self, directory: Path):
        """One writer per series directory at a time, across threads and (on POSIX) across processes."""
        directory.mkdir(parents=True, exist_ok=True)
        with self._local:
            handle = open(directory / ".lock", "a+")
            try:
                try:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX)
                except ImportError:  # no advisory locks on this platform: the in-process lock still applies
                    pass
                yield
            finally:
                handle.close()

    def write(self, dataset: str, frame: pd.DataFrame) -> WriteResult:
        """Insert rows, or replace rows with the same key. A row whose values are identical to the stored one is
        left as it was (its original timestamps are kept); one that differs replaces it and is counted as a revision."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        spec = DATASETS[dataset]
        result = WriteResult(rows_in=len(frame))
        if frame.empty:
            return result
        missing = [c for c in (*spec.parts, *spec.keys, spec.time) if c not in frame.columns]
        if missing:
            raise ValueError(f"{dataset}: missing columns {missing}")
        if not isinstance(frame[spec.time].dtype, pd.DatetimeTZDtype):
            raise ValueError(f"{dataset}: {spec.time} must be timezone-aware timestamps; naive times are refused rather than guessed")
        frame = frame.copy()
        if "ingested_at" not in frame.columns:
            frame["ingested_at"] = now_utc()
        for column in frame.columns:
            if isinstance(frame[column].dtype, pd.DatetimeTZDtype):
                frame[column] = utc(frame[column])
        frame["_bucket"] = _bucket(spec, frame)
        keys = list(spec.keys)
        for values, group in frame.groupby([*spec.parts, "_bucket"], sort=False, dropna=False):
            *parts, bucket = values
            directory = self.root.joinpath(dataset, *(safe_part(p) for p in parts))
            path = directory / f"{bucket}.parquet"
            new = group.drop(columns="_bucket").drop_duplicates(subset=keys, keep="last")
            with self._locked(directory):
                if path.exists():
                    old = pq.read_table(path).to_pandas()
                    merged, stats = _merge(old, new, keys, [c for c in spec.compare if c in new.columns and c in old.columns])
                else:
                    merged, stats = new, (len(new), 0, 0)
                result.rows_new, result.rows_revised, result.rows_unchanged = (result.rows_new + stats[0], result.rows_revised + stats[1],
                                                                              result.rows_unchanged + stats[2])
                if stats[0] or stats[1] or not path.exists():
                    merged = merged.sort_values(keys, kind="stable").reset_index(drop=True)
                    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                    pq.write_table(pa.Table.from_pandas(merged, preserve_index=False), tmp, compression="zstd")
                    os.replace(tmp, path)  # readers see the old file or the new one, never a partial one
                    result.files += 1
        return result

    # ------------------------------------------------------------------ reading
    def files(self, dataset: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, **parts) -> list[str]:
        """Files of the series selected by `parts` (unspecified parts match everything), skipping buckets outside [start, end]."""
        spec = DATASETS[dataset]
        unknown = set(parts) - set(spec.parts)
        if unknown:
            raise ValueError(f"{dataset} has no series key {sorted(unknown)}")
        base = self.root / dataset
        if not base.exists():
            return []
        pattern = "/".join(safe_part(parts[p]) if parts.get(p) is not None else "*" for p in spec.parts)
        out = []
        for path in base.glob(f"{pattern}/*.parquet"):
            bucket = path.stem
            if bucket != "all":
                if start is not None and bucket < start.strftime("%Y-%m-%d")[: len(bucket)]:
                    continue
                if end is not None and bucket > end.strftime("%Y-%m-%d")[: len(bucket)]:
                    continue
            out.append(str(path))
        return sorted(out)

    def read(self, dataset: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, columns: list[str] | None = None,
             limit: int | None = None, newest_first: bool = False, **parts) -> pd.DataFrame:
        """Rows of the selected series with event time in [start, end], ordered by key."""
        spec = DATASETS[dataset]
        files = self.files(dataset, start, end, **parts)
        if not files:
            return pd.DataFrame()
        select = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        where, args = [], []
        if start is not None:
            where.append(f'"{spec.time}" >= ?')
            args.append(start.to_pydatetime())
        if end is not None:
            where.append(f'"{spec.time}" <= ?')
            args.append(end.to_pydatetime())
        direction = "DESC" if newest_first else "ASC"
        order = ", ".join(f'"{k}" {direction}' for k in dict.fromkeys((spec.time, *spec.keys)))
        sql = (f"SELECT {select} FROM read_parquet(?, union_by_name=true)" + (f" WHERE {' AND '.join(where)}" if where else "")
               + f" ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else ""))
        with self.connect(views=False) as con:
            # Through Arrow rather than DuckDB's own pandas conversion, which relies on pandas calls that are being removed.
            frame = con.sql(sql, params=[files, *args]).to_arrow_table().to_pandas()
        return _normalise(frame)

    def bars(self, source: str, symbol: str, timeframe: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
        """OHLCV indexed by bar open time (UTC), in the shape the platform's engines expect, plus any extra columns stored."""
        frame = self.read("bars", start, end, source=source, symbol=symbol, timeframe=timeframe)
        if frame.empty:
            return frame
        frame = frame.set_index("ts")
        frame.index.name = "ts"
        return frame

    def stats(self, dataset: str, **parts) -> dict | None:
        """Row count and time span of one series, taken from the files' metadata."""
        spec = DATASETS[dataset]
        files = self.files(dataset, **parts)
        if not files:
            return None
        with self.connect(views=False) as con:
            n, first, last = con.execute(f'SELECT count(*), min("{spec.time}"), max("{spec.time}") FROM read_parquet(?, union_by_name=true)', [files]).fetchone()
        return {"rows": int(n), "first": pd.Timestamp(first).tz_convert("UTC") if first is not None else None,
                "last": pd.Timestamp(last).tz_convert("UTC") if last is not None else None,
                "bytes": sum(os.path.getsize(f) for f in files), "files": len(files)}

    @contextmanager
    def connect(self, views: bool = True):
        """A DuckDB connection for analytical queries. With `views`, every dataset that has files is exposed as a
        view of the same name (bars, trades, macro ...), so research code can join them in SQL."""
        import duckdb

        con = duckdb.connect()
        try:
            con.execute("SET TimeZone = 'UTC'")
            if views:
                for name, spec in DATASETS.items():
                    depth = "/".join("*" for _ in spec.parts) + "/*.parquet"
                    if (self.root / name).exists() and any((self.root / name).glob(depth)):
                        pattern = str(self.root / name / depth).replace("'", "''")
                        con.execute(f"CREATE VIEW \"{name}\" AS SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
            yield con
        finally:
            con.close()

    def series(self, dataset: str) -> list[dict]:
        """Every series stored for a dataset, as its key values (read from the directory tree)."""
        spec = DATASETS[dataset]
        base = self.root / dataset
        if not base.exists():
            return []
        out = []
        for directory in sorted({p.parent for p in base.glob("/".join("*" for _ in spec.parts) + "/*.parquet")}):
            out.append(dict(zip(spec.parts, directory.relative_to(base).parts)))
        return out


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    for column in frame.columns:
        if isinstance(frame[column].dtype, pd.DatetimeTZDtype):
            frame[column] = frame[column].dt.tz_convert("UTC").dt.as_unit("us")
    return frame


def _merge(old: pd.DataFrame, new: pd.DataFrame, keys: list[str], compare: list[str]) -> tuple[pd.DataFrame, tuple[int, int, int]]:
    """(merged rows, (new, revised, unchanged))."""
    old = _normalise(old)
    old_index, new_index = pd.MultiIndex.from_frame(old[keys]), pd.MultiIndex.from_frame(new[keys])
    known = new_index.isin(old_index)
    fresh = new[~known]
    overlap = new[known]
    revised = overlap.iloc[0:0]
    if len(overlap) and compare:  # without declared value columns a stored row is never replaced
        left = overlap.set_index(keys)[compare]
        right = old.set_index(keys)[compare].loc[left.index]
        differs = ~((left.to_numpy() == right.to_numpy()) | (pd.isna(left).to_numpy() & pd.isna(right).to_numpy())).all(axis=1)
        revised = overlap[differs]
    if len(revised):
        old = old[~old_index.isin(pd.MultiIndex.from_frame(revised[keys]))]
    pieces = [p for p in (old, revised, fresh) if len(p)]
    merged = pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else pieces[0]
    return merged, (len(fresh), len(revised), len(overlap) - len(revised))
