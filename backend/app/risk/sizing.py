"""Position sizing. Pure functions; no state, no I/O."""
from __future__ import annotations

from ..domain.models import Instrument


def position_size(equity: float, risk_fraction: float, stop_distance: float, price: float, instrument: Instrument,
                  fx_rate: float, max_notional: float | None = None, realized_vol: float | None = None,
                  target_vol: float | None = None) -> float:
    """Quantity such that being stopped out loses `risk_fraction` of equity.

    qty = equity * risk_fraction / (stop_distance * multiplier * fx_rate), then
      - scaled down (never up) by target_vol / realized_vol when both are given,
      - capped so notional (base currency) does not exceed `max_notional`,
      - rounded DOWN to the lot size; 0 if below the instrument minimum.
    """
    if min(equity, risk_fraction, stop_distance, price, fx_rate) <= 0:
        return 0.0
    qty = equity * risk_fraction / (stop_distance * instrument.contract_multiplier * fx_rate)
    if realized_vol and target_vol and realized_vol > target_vol:
        qty *= target_vol / realized_vol
    if max_notional is not None:
        qty = min(qty, max(max_notional, 0.0) / (price * instrument.contract_multiplier * fx_rate))
    qty = instrument.round_qty(qty)
    return qty if qty > 0 and qty >= instrument.min_qty else 0.0
