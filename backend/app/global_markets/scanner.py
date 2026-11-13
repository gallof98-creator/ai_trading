"""Market scanner: filter and rank every instrument that has computed metrics."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import InstrumentMetrics, SavedScan, Security
from ..instruments.registry import LEGACY_MARKET, MARKETS, market_of
from .exchanges import EXCHANGES
from .metrics import DERIVED, FIELDS, META

OPS = {">=", "<=", ">", "<", "==", "!=", "in", "is_true", "is_false"}
ALL_FIELDS = {**FIELDS, **DERIVED, **META}
_MARKET_EXCHANGE = {**{v: k for k, v in LEGACY_MARKET.items()}}

PRESETS: dict[str, dict] = {
    "Top gainers": {"filters": [{"field": "change_1d", "op": ">", "value": 0}], "sort": {"field": "change_1d", "descending": True}},
    "Top losers": {"filters": [{"field": "change_1d", "op": "<", "value": 0}], "sort": {"field": "change_1d", "descending": False}},
    "Unusual volume": {"filters": [{"field": "rel_volume", "op": ">=", "value": 2}], "sort": {"field": "rel_volume", "descending": True}},
    "Breakout candidates": {"filters": [{"field": "breakout_20", "op": "is_true"}, {"field": "rel_volume", "op": ">=", "value": 1.5}],
                            "sort": {"field": "rel_volume", "descending": True}},
    "Strong uptrends": {"filters": [{"field": "trend_up", "op": "is_true"}, {"field": "adx_14", "op": ">=", "value": 25}],
                        "sort": {"field": "rel_strength", "descending": True}},
    "Volatility expansion": {"filters": [{"field": "atr_ratio", "op": ">=", "value": 1.25}], "sort": {"field": "atr_ratio", "descending": True}},
    "Oversold (RSI below 30)": {"filters": [{"field": "rsi_14", "op": "<", "value": 30}], "sort": {"field": "rsi_14", "descending": False}},
    "Gap moves": {"filters": [{"field": "gap", "op": ">=", "value": 0.03}], "sort": {"field": "gap", "descending": True}},
    "Relative strength leaders": {"filters": [{"field": "rel_strength", "op": ">=", "value": 90}], "sort": {"field": "rel_strength", "descending": True}},
}


class ScanError(ValueError):
    pass


def validate_definition(definition: dict) -> dict:
    """Check a scan definition and return it in canonical form. Raises ScanError with a readable reason."""
    if not isinstance(definition, dict):
        raise ScanError("A scan definition must be an object")
    out: dict = {"filters": [], "universe": {}, "limit": 100}
    for f in definition.get("filters") or []:
        field, op = f.get("field"), f.get("op")
        if field not in ALL_FIELDS:
            raise ScanError(f"Unknown field {field!r}")
        if op not in OPS:
            raise ScanError(f"Unknown operator {op!r}")
        kind = ALL_FIELDS[field][1]
        value = f.get("value")
        if op in ("is_true", "is_false"):
            if kind != "bool":
                raise ScanError(f"{field} is not a yes/no field")
            value = None
        elif op == "in":
            if not isinstance(value, list) or not value:
                raise ScanError(f"'in' needs a list of values for {field}")
        elif kind in ("number", "percent"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ScanError(f"{field} needs a number")
        elif kind == "bool":
            raise ScanError(f"Use is_true / is_false with {field}")
        elif op not in ("==", "!="):
            raise ScanError(f"{field} is text: only ==, != and in apply")
        out["filters"].append({"field": field, "op": op, "value": value})
    universe = definition.get("universe") or {}
    for key in ("markets", "countries", "asset_types", "watchlist"):
        if universe.get(key):
            if not isinstance(universe[key], list):
                raise ScanError(f"universe.{key} must be a list")
            out["universe"][key] = [str(x) for x in universe[key]]
    sort = definition.get("sort")
    if sort:
        if sort.get("field") not in ALL_FIELDS:
            raise ScanError(f"Unknown sort field {sort.get('field')!r}")
        out["sort"] = {"field": sort["field"], "descending": bool(sort.get("descending", True))}
    limit = definition.get("limit", 100)
    if not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ScanError("limit must be between 1 and 2000")
    out["limit"] = limit
    return out


def universe_frame(session: Session, registry) -> pd.DataFrame:
    """One row per instrument with metrics, plus descriptive columns and the cross-sectional derived fields."""
    rows = list(session.scalars(select(InstrumentMetrics)))
    if not rows:
        return pd.DataFrame()
    linked = {s.instrument_id: s for s in session.scalars(select(Security).where(Security.instrument_id.is_not(None)))}
    records = []
    for r in rows:
        try:
            inst = registry.get(r.instrument_id)
        except KeyError:
            continue  # instrument removed since the metrics were computed
        market = market_of(inst) or inst.asset_class.value
        exchange = EXCHANGES.get(_MARKET_EXCHANGE.get(market, market))
        sec = linked.get(r.instrument_id)
        records.append({**r.values, "instrument_id": r.instrument_id, "symbol": inst.symbol, "name": sec.name if sec else "", "market": market,
                        "country": sec.country if sec else (exchange.country if exchange else ""), "currency": inst.quote_currency,
                        "asset_type": sec.security_type if sec else inst.asset_class.value, "sector": sec.sector if sec else None,
                        "industry": sec.industry if sec else None, "as_of": r.as_of, "data_kind": r.data_kind,
                        "previous_regime": r.previous_regime, "label": MARKETS.get(market, {}).get("label", market)})
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df
    for col in FIELDS:
        if col not in df:
            df[col] = None
    df["rel_strength"] = pd.to_numeric(df["ret_6m"], errors="coerce").rank(pct=True) * 100
    sector_ret = pd.to_numeric(df["ret_1m"], errors="coerce").groupby(df["sector"]).transform("mean")
    df["group_ret_1m"] = sector_ret.where(df["sector"].notna())
    return df


def run_scan(frame: pd.DataFrame, definition: dict, watchlists: dict[str, list[str]] | None = None) -> dict:
    """Apply a validated definition. A row with a missing value for a filtered field does not match."""
    d = validate_definition(definition)
    if frame.empty:
        return {"total_universe": 0, "matched": 0, "rows": [], "definition": d}
    df = frame
    u = d["universe"]
    if u.get("markets"):
        df = df[df["market"].isin(u["markets"])]
    if u.get("countries"):
        df = df[df["country"].isin(u["countries"])]
    if u.get("asset_types"):
        df = df[df["asset_type"].isin(u["asset_types"])]
    if u.get("watchlist"):
        ids = {i for name in u["watchlist"] for i in (watchlists or {}).get(name, [])}
        df = df[df["instrument_id"].isin(ids)]
    universe = len(df)
    for f in d["filters"]:
        col, op, value = df[f["field"]], f["op"], f["value"]
        kind = ALL_FIELDS[f["field"]][1]
        if op == "is_true":
            mask = col.map(lambda x: x is True)
        elif op == "is_false":
            mask = col.map(lambda x: x is False)
        elif op == "in":
            mask = col.isin(value)
        elif kind in ("number", "percent"):
            num = pd.to_numeric(col, errors="coerce")
            mask = {">=": num >= value, "<=": num <= value, ">": num > value, "<": num < value, "==": num == value, "!=": num != value}[op] & num.notna()
        else:
            mask = (col == value) if op == "==" else ((col != value) & col.notna())
        df = df[mask.fillna(False).astype(bool)]
    if d.get("sort"):
        key = d["sort"]["field"]
        df = df.assign(_k=pd.to_numeric(df[key], errors="coerce") if ALL_FIELDS[key][1] in ("number", "percent") else df[key])
        df = df[df["_k"].notna()].sort_values("_k", ascending=not d["sort"]["descending"], kind="stable").drop(columns="_k")
    matched = len(df)
    rows = df.head(d["limit"]).astype(object).where(df.head(d["limit"]).notna(), None).to_dict("records")
    return {"total_universe": universe, "matched": matched, "rows": rows, "definition": d}


def run_saved_scans(session: Session, registry, notify, now: datetime | None = None) -> list[dict]:
    """Event-triggered run of every saved scan marked `run_after_update`; stores the result and notifies."""
    now = now or datetime.now(timezone.utc)
    scans = list(session.scalars(select(SavedScan).where(SavedScan.run_after_update.is_(True))))
    if not scans:
        return []
    from ..database.models import Watchlist

    frame = universe_frame(session, registry)
    lists = {w.name: w.instrument_ids for w in session.scalars(select(Watchlist))}
    out = []
    for scan in scans:
        result = run_scan(frame, scan.definition, lists)
        top = [r["instrument_id"] for r in result["rows"][:10]]
        scan.last_run, scan.last_result = now, {"matched": result["matched"], "total_universe": result["total_universe"], "top": top}
        out.append({"name": scan.name, "matched": result["matched"]})
        if scan.notify and result["matched"]:
            notify(now, "scanner", "info", f"Scan '{scan.name}': {result['matched']} matches", ", ".join(top))
    return out
