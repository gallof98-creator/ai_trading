"""Phase 4 of the global module: transparent rankings and whole-universe analysis."""
import numpy as np
import pandas as pd
import pytest

from app.global_markets import rankings
from app.global_markets.metrics import FIELDS


def universe(n=40, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "instrument_id": [f"STOCK:T{i:02d}" for i in range(n)], "symbol": [f"T{i:02d}" for i in range(n)], "name": "", "market": ["US"] * (n // 2) + ["IT"] * (n - n // 2),
        "country": ["US"] * (n // 2) + ["IT"] * (n - n // 2), "currency": "USD", "asset_type": "common_stock", "sector": [["Tech", "Energy", None][i % 3] for i in range(n)],
        "industry": None, "as_of": "2026-11-12", "data_kind": "historical", "previous_regime": "sideways", "regime": ["strong_uptrend", "sideways"] * (n // 2),
        "close": 50.0, "dollar_volume_20": 5e6, "mom_12_1": np.linspace(-0.5, 0.9, n), "volatility_20": rng.uniform(0.1, 0.6, n), "ret_1m": rng.normal(0, 0.05, n),
        "ret_3m": rng.normal(0, 0.1, n), "ret_6m": rng.normal(0, 0.2, n), "ret_5d": rng.normal(0, 0.03, n), "dist_high_52w": -rng.uniform(0, 0.4, n),
        "dist_sma_50": rng.normal(0, 0.05, n), "dist_sma_200": rng.normal(0, 0.1, n), "adx_14": rng.uniform(10, 40, n), "change_1d": rng.normal(0, 0.01, n),
        "rel_volume": rng.uniform(0.5, 1.5, n), "atr_ratio": rng.uniform(0.8, 1.2, n), "breakout_20": False, "breakdown_20": False})
    for col in FIELDS:
        if col not in df:
            df[col] = None
    return df


def test_ranking_is_the_documented_formula():
    df = universe()
    out = rankings.rank(df, "momentum")
    assert [r["symbol"] for r in out["rows"][:3]] == ["T39", "T38", "T37"] and out["rows"][-1]["symbol"] == "T00"  # higher 12-1 momentum ranks higher
    assert (out["universe"], out["eligible"], out["ranked"]) == (40, 40, 40) and out["rows"][0]["percentile"] == 100.0
    low_vol = rankings.rank(df, "low_volatility")
    assert low_vol["rows"][0]["instrument_id"] == df.loc[df.volatility_20.idxmin(), "instrument_id"]  # direction -1: lowest volatility first

    multi = rankings.rank(df, "multi_factor")
    z = {f: rankings.zscores(df[f]) for f in ("mom_12_1", "volatility_20", "dist_high_52w", "ret_1m")}
    expected = 0.4 * z["mom_12_1"] - 0.2 * z["volatility_20"] + 0.2 * z["dist_high_52w"] - 0.2 * z["ret_1m"]
    for row in multi["rows"]:
        i = int(row["symbol"][1:])
        assert row["score"] == pytest.approx(expected.iloc[i]) and sum(row["contributions"].values()) == pytest.approx(row["score"])
    assert [f["metric"] for f in multi["criteria"]] == ["mom_12_1", "volatility_20", "dist_high_52w", "ret_1m"] and "not a forecast" in multi["disclaimer"]
    with pytest.raises(ValueError):
        rankings.rank(df, "crystal_ball")
    assert {m["name"] for m in rankings.describe_models()} == set(rankings.MODELS)


def test_eligibility_excludes_illiquid_cheap_non_equity_and_incomplete_instruments():
    df = universe()
    df.loc[0, "close"] = 0.4            # penny stock
    df.loc[1, "dollar_volume_20"] = 5e4  # illiquid
    df.loc[2, "asset_type"] = "etf"     # not an equity
    df.loc[3, "mom_12_1"] = None        # too little history for the factor
    out = rankings.rank(df, "momentum")
    assert (out["eligible"], out["ranked"]) == (37, 36)
    assert not {r["symbol"] for r in out["rows"]} & {"T00", "T01", "T02", "T03"}
    assert rankings.rank(df, "momentum", markets=["IT"])["ranked"] == 20
    assert rankings.rank(df.iloc[:1], "momentum")["rows"] == []  # nothing to compare against
    assert rankings.rank(pd.DataFrame(), "momentum")["rows"] == []


def test_zscores_are_winsorised_and_safe_on_constants():
    s = pd.Series([1.0] * 50 + [1000.0])
    z = rankings.zscores(s)
    assert z.iloc[-1] < 10  # one outlier cannot dominate the score
    assert (rankings.zscores(pd.Series([5.0] * 10)) == 0).all()


def test_market_overview_breadth_groups_and_abnormal_moves():
    df = universe()
    df["change_1d"] = [0.01] * 25 + [-0.01] * 14 + [0.0]
    df.loc[5, "change_1d"] = 0.45       # an abnormal jump
    df.loc[6, "rel_volume"] = 6.0
    df.loc[7, "atr_ratio"] = 2.0
    df.loc[8, "breakout_20"] = True
    df.loc[9, "regime"] = "high_volatility"
    df.loc[10, "asset_type"] = "etf"    # ETFs are not part of equity breadth
    df["dist_sma_50"] = [0.05] * 30 + [-0.05] * 10
    ov = rankings.market_overview(df)
    b = ov["breadth"]
    assert (ov["instruments"], ov["equities"], b["instruments"]) == (40, 39, 39)
    assert (b["advancers"], b["decliners"], b["unchanged"]) == (24, 14, 1) and b["above_sma_50"] == pytest.approx(29 / 39)
    assert [g["group"] for g in ov["groups"]] in (["US", "IT"], ["IT", "US"]) and sum(g["instruments"] for g in ov["groups"]) == 39
    assert ov["top_gainers"][0]["symbol"] == "T05" and ov["abnormal_moves"][0]["symbol"] == "T05" and ov["abnormal_moves"][0]["robust_z"] > 5
    assert [r["symbol"] for r in ov["unusual_volume"]] == ["T06"] and [r["symbol"] for r in ov["volatility_expansion"]] == ["T07"]
    assert [r["symbol"] for r in ov["breakout_candidates"]] == ["T08"] and b["breakouts_20d"] == 1
    assert ov["regime_changes_total"] == 20 and {"from": "sideways", "to": "high_volatility"}.items() <= next(c for c in ov["regime_changes"] if c["symbol"] == "T09").items()
    by_sector = rankings.market_overview(df, "sector")
    assert {g["group"] for g in by_sector["groups"]} == {"Tech", "Energy", "(unknown)"} and by_sector["sector_coverage"]["without"] == 13
    assert rankings.market_overview(df, markets=["IT"])["equities"] == 20
    with pytest.raises(ValueError):
        rankings.market_overview(df, "astrology")
    assert rankings.market_overview(pd.DataFrame())["instruments"] == 0

    report = rankings.overview_report(ov, rankings.rank(df, "momentum"))
    for text in ("## Breadth", "## Unusual volume", "T06", "## Abnormal price moves", "## Ranking: Momentum (12-1)", "do not guarantee", "not a forecast"):
        assert text in report
    assert "No metrics" in rankings.overview_report({"instruments": 0})
