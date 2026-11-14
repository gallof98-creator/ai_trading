from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..domain.models import Instrument, Order


@dataclass(frozen=True)
class Exposure:
    """One open position slice, valued in the portfolio base currency."""

    broker_id: str
    instrument_id: str
    strategy: str | None
    asset_class: str
    currency: str
    signed_notional: float  # >0 long, <0 short
    margin: float = 0.0


@dataclass
class PortfolioState:
    ts: datetime
    equity: float
    day_start_equity: float
    peak_equity: float
    exposures: list[Exposure] = field(default_factory=list)
    # Pairwise return correlations keyed by sorted instrument-id pair. Missing pairs are treated as unknown (0).
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    reconciled: bool = True  # False when internal books and broker-reported positions disagree

    @property
    def margin_used(self) -> float:
        return sum(e.margin for e in self.exposures)

    @property
    def gross_exposure(self) -> float:
        return sum(abs(e.signed_notional) for e in self.exposures)

    def net_for(self, broker_id: str, instrument_id: str) -> float:
        return sum(e.signed_notional for e in self.exposures if e.broker_id == broker_id and e.instrument_id == instrument_id)

    def correlation(self, a: str, b: str) -> float:
        return 1.0 if a == b else self.correlations.get((a, b) if a < b else (b, a), 0.0)


@dataclass
class OrderIntent:
    order: Order
    instrument: Instrument
    price: float  # reference price in quote currency
    fx_rate: float  # quote currency -> base currency
    stop_distance: float | None  # price distance to the protective stop, quote currency
    cost_per_unit: float = 0.0  # estimated round-trip costs per unit (spread, slippage, commissions, FX), quote currency
    data_age_seconds: float | None = None  # age of the live price the order was sized on; None when not a live price
    spread_bps: float | None = None  # live bid/ask spread, when known

    @property
    def signed_notional(self) -> float:
        return self.order.side.sign * self.instrument.notional(self.order.qty, self.price) * self.fx_rate

    @property
    def loss_at_stop(self) -> float | None:
        if not self.stop_distance or self.stop_distance <= 0:
            return None
        return self.order.qty * (self.stop_distance + self.cost_per_unit) * self.instrument.contract_multiplier * self.fx_rate


class EmergencyAction(StrEnum):
    CANCEL_OPEN_ORDERS = "cancel_open_orders"
    FLATTEN_POSITIONS = "flatten_positions"


@dataclass(frozen=True)
class RiskEventRecord:
    ts: datetime
    kind: str  # order_rejected | breaker_tripped | breaker_reset | kill_switch_on | kill_switch_off
    severity: str  # info | warning | critical
    detail: dict


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str]
    risk_increasing: bool
