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

    def unregister(self, instrument_id: str) -> None:
        self._instruments.pop(instrument_id, None)
        for key in [k for k in self._to_broker if k[1] == instrument_id]:
            self._from_broker.pop((key[0], self._to_broker.pop(key)), None)

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


# Stock markets that can be added from the dashboard: exchange calendar, trading currency and the
# ticker suffix used to keep identifiers unique across markets (e.g. STOCK:ENI.MI).
# London is deliberately absent: its shares are quoted in pence, which needs dedicated handling.
MARKETS: dict[str, dict] = {
    "US": {"label": "United States", "calendar": "XNYS", "currency": "USD", "suffix": "", "tick_size": 0.01},
    "IT": {"label": "Italy (Borsa Italiana)", "calendar": "XMIL", "currency": "EUR", "suffix": ".MI", "tick_size": 0.0001},
    "DE": {"label": "Germany (Xetra)", "calendar": "XETR", "currency": "EUR", "suffix": ".DE", "tick_size": 0.001},
    "FR": {"label": "France (Euronext Paris)", "calendar": "XPAR", "currency": "EUR", "suffix": ".PA", "tick_size": 0.001},
    "NL": {"label": "Netherlands (Euronext Amsterdam)", "calendar": "XAMS", "currency": "EUR", "suffix": ".AS", "tick_size": 0.001},
    "ES": {"label": "Spain (BME)", "calendar": "XMAD", "currency": "EUR", "suffix": ".MC", "tick_size": 0.001},
    "CH": {"label": "Switzerland (SIX)", "calendar": "XSWX", "currency": "CHF", "suffix": ".SW", "tick_size": 0.01},
}


def make_equity(ticker: str, market: str, asset_class: AssetClass = AssetClass.STOCK) -> Instrument:
    """Build a normalized stock/ETF instrument for one of the supported markets."""
    import re

    if market not in MARKETS:
        raise ValueError(f"Unknown market {market!r}; supported: {', '.join(MARKETS)}")
    if asset_class not in (AssetClass.STOCK, AssetClass.ETF):
        raise ValueError("Only stocks and ETFs can be added this way")
    ticker = ticker.strip().upper()
    m = MARKETS[market]
    if m["suffix"] and ticker.endswith(m["suffix"]):
        ticker = ticker[: -len(m["suffix"])]
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", ticker):
        raise ValueError(f"{ticker!r} is not a valid ticker")
    return Instrument(id=f"{asset_class.value.upper()}:{ticker}{m['suffix']}", asset_class=asset_class, symbol=ticker,
                      quote_currency=m["currency"], exchange=m["calendar"], calendar=m["calendar"], tick_size=m["tick_size"],
                      lot_size=1, min_qty=1, margin_rate=0.5)
