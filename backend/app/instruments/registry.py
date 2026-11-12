"""Instrument registry and broker symbol mapping."""
from __future__ import annotations

from ..domain.models import AssetClass, Instrument


class UnknownInstrumentError(KeyError):
    pass


class UnmappedSymbolError(KeyError):
    pass


class InstrumentRegistry:
    """Holds normalized instruments and maps them to broker-specific symbols.

    An instrument that has no mapping for a broker is NOT tradable there; adapters
    must resolve symbols through `to_broker`, which raises instead of guessing.
    """

    def __init__(self) -> None:
        self._instruments: dict[str, Instrument] = {}
        self._to_broker: dict[tuple[str, str], str] = {}
        self._from_broker: dict[tuple[str, str], str] = {}

    def register(self, instrument: Instrument, broker_symbols: dict[str, str] | None = None) -> Instrument:
        self._instruments[instrument.id] = instrument
        for broker_id, symbol in (broker_symbols or {}).items():
            self.map_symbol(broker_id, instrument.id, symbol)
        return instrument

    def map_symbol(self, broker_id: str, instrument_id: str, broker_symbol: str) -> None:
        self.get(instrument_id)
        self._to_broker[(broker_id, instrument_id)] = broker_symbol
        self._from_broker[(broker_id, broker_symbol)] = instrument_id

    def get(self, instrument_id: str) -> Instrument:
        try:
            return self._instruments[instrument_id]
        except KeyError:
            raise UnknownInstrumentError(instrument_id) from None

    def all(self) -> list[Instrument]:
        return list(self._instruments.values())

    def is_mapped(self, broker_id: str, instrument_id: str) -> bool:
        return (broker_id, instrument_id) in self._to_broker

    def to_broker(self, broker_id: str, instrument_id: str) -> str:
        try:
            return self._to_broker[(broker_id, instrument_id)]
        except KeyError:
            raise UnmappedSymbolError(f"{instrument_id} is not mapped for broker {broker_id}") from None

    def from_broker(self, broker_id: str, broker_symbol: str) -> str:
        try:
            return self._from_broker[(broker_id, broker_symbol)]
        except KeyError:
            raise UnmappedSymbolError(f"{broker_symbol} is unknown for broker {broker_id}") from None


def default_instruments() -> list[Instrument]:
    """A small starter universe covering every supported asset class."""
    A = AssetClass
    return [
        Instrument(id="CRYPTO:BTC/USDT", asset_class=A.CRYPTO, symbol="BTC/USDT", base_currency="BTC", quote_currency="USDT",
                   calendar="24x7", tick_size=0.01, lot_size=0.00001, min_qty=0.00001),
        Instrument(id="CRYPTO:ETH/USDT", asset_class=A.CRYPTO, symbol="ETH/USDT", base_currency="ETH", quote_currency="USDT",
                   calendar="24x7", tick_size=0.01, lot_size=0.0001, min_qty=0.0001),
        Instrument(id="STOCK:AAPL", asset_class=A.STOCK, symbol="AAPL", quote_currency="USD", exchange="NASDAQ",
                   calendar="XNYS", tick_size=0.01, lot_size=1, min_qty=1, margin_rate=0.5),
        Instrument(id="ETF:SPY", asset_class=A.ETF, symbol="SPY", quote_currency="USD", exchange="ARCA",
                   calendar="XNYS", tick_size=0.01, lot_size=1, min_qty=1, margin_rate=0.5),
        Instrument(id="FOREX:EUR/USD", asset_class=A.FOREX, symbol="EUR/USD", base_currency="EUR", quote_currency="USD",
                   calendar="FX", tick_size=0.00001, lot_size=1000, min_qty=1000, margin_rate=0.0333),
        Instrument(id="FUTURE:ES", asset_class=A.FUTURE, symbol="ES", quote_currency="USD", exchange="CME",
                   calendar="CME", tick_size=0.25, lot_size=1, min_qty=1, contract_multiplier=50, margin_rate=0.06),
    ]
