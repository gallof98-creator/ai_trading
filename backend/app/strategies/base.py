"""Strategy plugin framework.

A strategy turns OHLCV history into a desired position direction per bar (+1 long, -1 short,
0 flat). The value for bar t may only use bars up to and including t; the engines act on it at
the NEXT bar. Sizing, risk limits and execution are deliberately not the strategy's business.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from . import indicators as ind


class StrategyParams(BaseModel):
    """Common exit/risk parameters. Subclasses add their own; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    atr_period: int = Field(14, ge=2, le=200)
    stop_atr: float = Field(2.0, gt=0, le=20, description="Stop-loss distance in ATRs (always required)")
    take_profit_atr: float | None = Field(4.0, gt=0, le=50, description="Take-profit distance in ATRs; null disables")
    trailing_atr: float | None = Field(None, gt=0, le=20, description="Trailing-stop distance in ATRs; null disables")
    allow_short: bool = True


class Strategy(ABC):
    name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    category: ClassVar[str]
    description: ClassVar[str] = ""
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, params: dict | StrategyParams | None = None) -> None:
        self.params = params if isinstance(params, StrategyParams) else self.Params(**(params or {}))

    @abstractmethod
    def target(self, df: pd.DataFrame) -> pd.Series | np.ndarray:
        """Desired direction per bar in {-1, 0, +1}. Must be causal."""

    def warmup(self) -> int:
        """Bars needed before the strategy can produce a signal."""
        ints = [v for k, v in self.params.model_dump().items() if isinstance(v, int) and not isinstance(v, bool)]
        return 3 * max(ints, default=1) + 5

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Signal frame aligned to `df`: target, action, atr and stop/take-profit/trailing distances."""
        p = self.params
        target = pd.Series(np.asarray(self.target(df)), index=df.index).fillna(0).astype(int).clip(-1, 1)
        if not p.allow_short:
            target = target.clip(lower=0)
        atr = ind.atr(df, p.atr_period)
        target = target.where(atr.notna() & (atr > 0), 0)  # no stop distance => no position
        change = target.diff().fillna(target)
        out = pd.DataFrame({"target": target, "atr": atr, "close": df["close"]}, index=df.index)
        out["action"] = np.select([change > 0, change < 0], ["BUY", "SELL"], "HOLD")
        out["stop_distance"] = atr * p.stop_atr
        out["take_profit_distance"] = atr * p.take_profit_atr if p.take_profit_atr else np.nan
        out["trailing_distance"] = atr * p.trailing_atr if p.trailing_atr else np.nan
        return out

    @classmethod
    def describe(cls) -> dict:
        return {"name": cls.name, "version": cls.version, "category": cls.category, "description": cls.description,
                "params_schema": cls.Params.model_json_schema(), "defaults": cls.Params().model_dump()}
