"""Historical bar storage and cache (database-backed)."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..database.models import BarRow, BarSeries
from .quality import DataKind
from .sources import Dataset
from .timeframes import OHLCV, epoch_ns


class BarStore:
    def save(self, session: Session, instrument_id: str, timeframe: str, dataset: Dataset) -> int:
        """Upsert bars. Rows in the dataset's time range are replaced, so re-imports are idempotent."""
        bars = dataset.bars
        if bars.empty:
            return 0
        ts_ms = epoch_ns(bars.index) // 1_000_000
        session.execute(delete(BarRow).where(BarRow.instrument_id == instrument_id, BarRow.timeframe == timeframe,
                                             BarRow.ts_ms >= int(ts_ms[0]), BarRow.ts_ms <= int(ts_ms[-1])))
        values = bars[OHLCV].to_numpy()
        session.bulk_insert_mappings(BarRow, [
            {"instrument_id": instrument_id, "timeframe": timeframe, "ts_ms": int(t), "open": r[0], "high": r[1], "low": r[2],
             "close": r[3], "volume": r[4], "kind": dataset.kind.value, "source": dataset.source}
            for t, r in zip(ts_ms, values)
        ])
        session.flush()
        self._refresh_summary(session, instrument_id, timeframe)
        return len(bars)

    def load(self, session: Session, instrument_id: str, timeframe: str, start: pd.Timestamp | None = None,
             end: pd.Timestamp | None = None) -> Dataset | None:
        q = select(BarRow).where(BarRow.instrument_id == instrument_id, BarRow.timeframe == timeframe)
        if start is not None:
            q = q.where(BarRow.ts_ms >= int(start.as_unit("ns").value // 1_000_000))
        if end is not None:
            q = q.where(BarRow.ts_ms <= int(end.as_unit("ns").value // 1_000_000))
        rows = session.scalars(q.order_by(BarRow.ts_ms)).all()
        if not rows:
            return None
        kinds, sources = {r.kind for r in rows}, {r.source for r in rows}
        # A range stitched from different provenances is reported as the weakest of them.
        kind = DataKind.SIMULATED if "simulated" in kinds else DataKind(sorted(kinds)[0]) if len(kinds) == 1 else DataKind.HISTORICAL
        idx = pd.to_datetime([r.ts_ms for r in rows], unit="ms", utc=True).rename("ts")
        bars = pd.DataFrame([[r.open, r.high, r.low, r.close, r.volume] for r in rows], index=idx, columns=OHLCV)
        return Dataset(bars, kind, "+".join(sorted(sources)))

    def _refresh_summary(self, session: Session, instrument_id: str, timeframe: str) -> None:
        """Recompute the summary row for one series (cheap: uses the bars primary key)."""
        where = (BarRow.instrument_id == instrument_id, BarRow.timeframe == timeframe)
        n, first, last = session.execute(select(func.count(), func.min(BarRow.ts_ms), func.max(BarRow.ts_ms)).where(*where)).one()
        row = session.get(BarSeries, (instrument_id, timeframe))
        if not n:
            if row is not None:
                session.delete(row)
            return
        kinds = set(session.scalars(select(BarRow.kind).where(*where).distinct()))
        sources = sorted(set(session.scalars(select(BarRow.source).where(*where).distinct())))
        kind = "simulated" if "simulated" in kinds else next(iter(kinds)) if len(kinds) == 1 else "historical"
        values = {"bars": n, "first_ms": first, "last_ms": last, "kind": kind, "source": "+".join(sources)[:64]}
        if row is None:
            session.add(BarSeries(instrument_id=instrument_id, timeframe=timeframe, **values))
        else:
            for k, v in values.items():
                setattr(row, k, v)

    def delete_series(self, session: Session, instrument_id: str, timeframe: str | None = None) -> None:
        """Delete one series, or every timeframe of an instrument. The only supported way to remove bars."""
        bars = delete(BarRow).where(BarRow.instrument_id == instrument_id)
        summary = delete(BarSeries).where(BarSeries.instrument_id == instrument_id)
        if timeframe is not None:
            bars, summary = bars.where(BarRow.timeframe == timeframe), summary.where(BarSeries.timeframe == timeframe)
        session.execute(bars)
        session.execute(summary)
        session.flush()

    def coverage_for(self, session: Session, instrument_id: str) -> list[dict]:
        rows = session.scalars(select(BarSeries).where(BarSeries.instrument_id == instrument_id))
        return [{"instrument_id": r.instrument_id, "timeframe": r.timeframe, "kind": r.kind, "bars": r.bars} for r in rows]

    def coverage(self, session: Session) -> list[dict]:
        return [
            {"instrument_id": r.instrument_id, "timeframe": r.timeframe, "kind": r.kind, "bars": r.bars, "source": r.source,
             "first": pd.Timestamp(r.first_ms, unit="ms", tz="UTC").isoformat(), "last": pd.Timestamp(r.last_ms, unit="ms", tz="UTC").isoformat()}
            for r in session.scalars(select(BarSeries).order_by(BarSeries.instrument_id, BarSeries.timeframe))
        ]
