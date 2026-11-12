"""Historical bar storage and cache (database-backed)."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..database.models import BarRow
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

    def coverage(self, session: Session) -> list[dict]:
        q = select(BarRow.instrument_id, BarRow.timeframe, BarRow.kind, func.count(), func.min(BarRow.ts_ms), func.max(BarRow.ts_ms)) \
            .group_by(BarRow.instrument_id, BarRow.timeframe, BarRow.kind)
        return [
            {"instrument_id": i, "timeframe": tf, "kind": k, "bars": n,
             "first": pd.Timestamp(a, unit="ms", tz="UTC").isoformat(), "last": pd.Timestamp(b, unit="ms", tz="UTC").isoformat()}
            for i, tf, k, n, a, b in session.execute(q)
        ]
