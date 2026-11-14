"""Timestamped FX rates with conversion costs.

A conversion at time T only ever uses a rate observed at or before T, and fails loudly when
the newest such rate is older than `max_age` instead of quietly using stale data.
"""
from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# Rates entered as an explicit, user-stated assumption (backtests, simulated replays). They never
# expire, and results that use them are flagged.
STATIC_SOURCE = "static-assumption"


# Some exchanges quote prices in a subunit of the currency (London in pence). 1 subunit = factor units of the currency.
SUBUNITS: dict[str, tuple[str, float]] = {"GBX": ("GBP", 0.01), "ZAC": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}


class FxRateUnavailable(LookupError):
    pass


@dataclass(frozen=True)
class FxQuote:
    rate: float  # 1 unit of `from` = rate units of `to`
    ts: datetime  # observation time of the oldest leg used
    source: str


class FxRateProvider:
    def __init__(self, conversion_cost_bps: float = 0.0, max_age: timedelta = timedelta(days=5), pivot: str = "EUR",
                 pegs: dict[str, str] | None = None) -> None:
        """`pegs` maps a currency to one it is ASSUMED to trade 1:1 with (e.g. USDT -> USD).
        It is an explicit modelling assumption supplied by configuration, never a default."""
        self.conversion_cost_bps = conversion_cost_bps
        self.max_age = max_age
        self.pivot = pivot
        self.pegs = dict(pegs or {})
        self._ts: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        self._rows: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)

    def add_rate(self, base: str, quote: str, ts: datetime, rate: float, source: str) -> None:
        """Record: at `ts`, 1 `base` = `rate` `quote`."""
        if ts.tzinfo is None:
            raise ValueError("FX rate timestamp must be timezone-aware")
        if not rate > 0:
            raise ValueError("FX rate must be positive")
        key = (base, quote)
        i = bisect.bisect_right(self._ts[key], ts)
        self._ts[key].insert(i, ts)
        self._rows[key].insert(i, (rate, source))

    def _direct(self, a: str, b: str, ts: datetime) -> FxQuote | None:
        """The newest observation at or before `ts`, whichever way round the pair was recorded (a rate entered as
        USD/EUR and a later one published as EUR/USD describe the same thing: the later one wins)."""
        best = None
        for key, invert in (((a, b), False), ((b, a), True)):
            times = self._ts.get(key)
            if not times:
                continue
            i = bisect.bisect_right(times, ts) - 1
            if i < 0:
                continue
            rate, source = self._rows[key][i]
            if best is None or times[i] > best[0]:
                best = (times[i], 1 / rate if invert else rate, source)
        if best is None:
            return None
        when, rate, source = best
        if ts - when > self.max_age and source != STATIC_SOURCE:
            raise FxRateUnavailable(f"Newest {a}/{b} rate at {ts.isoformat()} is from {when.isoformat()} (older than {self.max_age})")
        return FxQuote(rate, when, source)

    def quote(self, from_ccy: str, to_ccy: str, ts: datetime) -> FxQuote:
        if ts.tzinfo is None:
            raise ValueError("Conversion timestamp must be timezone-aware")
        from_main, from_factor = SUBUNITS.get(from_ccy, (from_ccy, 1.0))
        to_main, to_factor = SUBUNITS.get(to_ccy, (to_ccy, 1.0))
        if (from_factor, to_factor) != (1.0, 1.0):
            inner = self.quote(from_main, to_main, ts)
            return FxQuote(inner.rate * from_factor / to_factor, inner.ts, "unit" if inner.source == "identity" else inner.source)
        a, b = self.pegs.get(from_ccy, from_ccy), self.pegs.get(to_ccy, to_ccy)
        if a == b:
            return FxQuote(1.0, ts, "identity" if from_ccy == to_ccy else "peg")
        direct = self._direct(a, b, ts)
        if direct:
            return direct
        if self.pivot not in (a, b):
            leg1, leg2 = self._direct(a, self.pivot, ts), self._direct(self.pivot, b, ts)
            if leg1 and leg2:
                return FxQuote(leg1.rate * leg2.rate, min(leg1.ts, leg2.ts), f"{leg1.source}+{leg2.source}")
        raise FxRateUnavailable(f"No {from_ccy}->{to_ccy} rate available at or before {ts.isoformat()}")

    def convert(self, amount: float, from_ccy: str, to_ccy: str, ts: datetime, with_cost: bool = True) -> float:
        """Convert, charging the conversion cost against the holder (gains shrink, losses grow)."""
        q = self.quote(from_ccy, to_ccy, ts)
        gross = amount * q.rate
        if not with_cost or q.source == "identity":
            return gross
        return gross - abs(gross) * self.conversion_cost_bps / 10_000


ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def parse_ecb_reference_rates(xml_text: str) -> list[tuple[str, str, datetime, float]]:
    """Parse the ECB euro reference-rate XML into (base, quote, ts, rate) rows (base is EUR).
    ECB publishes around 16:00 CET; rows are stamped 15:00 UTC of the reference date."""
    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    rows = []
    for day in ET.fromstring(xml_text).findall(".//e:Cube[@time]", ns):
        ts = datetime.strptime(day.attrib["time"], "%Y-%m-%d").replace(hour=15, tzinfo=timezone.utc)
        for cube in day.findall("e:Cube", ns):
            rows.append(("EUR", cube.attrib["currency"], ts, float(cube.attrib["rate"])))
    return rows
