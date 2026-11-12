"""Commission models and currency conversion."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommissionModel:
    """commission = max(minimum, notional * rate + |qty| * per_unit), in quote currency."""

    rate: float = 0.0  # fraction of notional (crypto, forex)
    per_unit: float = 0.0  # per share / per contract (stocks, futures)
    minimum: float = 0.0

    def cost(self, qty: float, price: float, multiplier: float = 1.0) -> float:
        if qty == 0:
            return 0.0
        return max(self.minimum, abs(qty) * price * multiplier * self.rate + abs(qty) * self.per_unit)


DEFAULT_COMMISSIONS = {
    "crypto": CommissionModel(rate=0.001),
    "stock": CommissionModel(per_unit=0.005, minimum=1.0),
    "etf": CommissionModel(per_unit=0.005, minimum=1.0),
    "forex": CommissionModel(rate=0.00002, minimum=2.0),
    "future": CommissionModel(per_unit=2.25),
}


class FxConverter:
    def __init__(self, rates_to_base: dict[str, float], base: str = "EUR") -> None:
        self.base = base
        self._rates = {**rates_to_base, base: 1.0}

    def rate(self, currency: str) -> float:
        try:
            return self._rates[currency]
        except KeyError:
            raise ValueError(f"No FX rate configured for {currency}->{self.base}") from None

    def to_base(self, amount: float, currency: str) -> float:
        return amount * self.rate(currency)
