"""Live-trading authorization gate.

A `LiveAuthorization` can only be obtained from deployment settings in which the operator set
both QAT_LIVE_TRADING_ENABLED and the confirmation phrase. Real-money adapters require one;
nothing in the AI, strategy or paper code paths can construct it from user input.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.config import Settings


class LiveTradingNotAuthorized(PermissionError):
    pass


@dataclass(frozen=True)
class LiveAuthorization:
    environment: str
    _token: object

    @property
    def valid(self) -> bool:
        return self._token is _SENTINEL


_SENTINEL = object()


def authorize_live_trading(settings: Settings) -> LiveAuthorization:
    if not settings.live_trading_authorized:
        raise LiveTradingNotAuthorized(
            "Live trading is disabled. It requires QAT_LIVE_TRADING_ENABLED=true and the confirmation phrase in the live deployment's environment.")
    return LiveAuthorization(settings.environment, _SENTINEL)


def require_valid(authorization: LiveAuthorization | None) -> None:
    if authorization is None or not isinstance(authorization, LiveAuthorization) or not authorization.valid:
        raise LiveTradingNotAuthorized("A valid live-trading authorization is required for real-money connections")
