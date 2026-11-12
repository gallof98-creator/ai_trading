"""Continuous futures series (back-adjusted)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

PRICE_COLS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class Roll:
    ts: pd.Timestamp  # first bar traded in the new contract
    from_contract: str
    to_contract: str
    gap: float  # new minus old close on the last bar before the roll


def build_continuous(contracts: list[tuple[str, pd.Timestamp, pd.DataFrame]], roll_days_before_expiry: int = 5) -> tuple[pd.DataFrame, list[Roll]]:
    """Stitch contracts [(name, expiry, bars)] into one back-adjusted series.

    Each contract is used until `roll_days_before_expiry` days before it expires. At every roll,
    all EARLIER prices are shifted by the price gap between the two contracts, so bar-to-bar price
    differences (and therefore simulated P&L) are preserved. Adjusted price LEVELS before a roll
    are synthetic: do not compute percentage returns across long back-adjusted histories.
    The roll schedule depends only on expiry dates, so it uses no future price information.
    """
    if not contracts:
        raise ValueError("No contracts given")
    contracts = sorted(contracts, key=lambda c: c[1])
    pieces: list[pd.DataFrame] = []
    rolls: list[Roll] = []
    start = None
    for i, (name, expiry, bars) in enumerate(contracts):
        last = i == len(contracts) - 1
        end = bars.index[-1] if last else expiry - pd.Timedelta(days=roll_days_before_expiry)
        piece = bars[(bars.index <= end) & ((bars.index > start) if start is not None else True)]
        if piece.empty:
            raise ValueError(f"Contract {name} has no bars in its active window")
        if pieces:
            prev_name, _, prev_bars = contracts[i - 1]
            anchor = pieces[-1].index[-1]
            if anchor not in bars.index:
                raise ValueError(f"Contracts {prev_name} and {name} do not overlap at {anchor}")
            gap = float(bars.loc[anchor, "close"] - prev_bars.loc[anchor, "close"])
            for p in pieces:
                p[PRICE_COLS] += gap
            rolls.append(Roll(piece.index[0], prev_name, name, gap))
        pieces.append(piece.copy())
        start = end
    return pd.concat(pieces), rolls
