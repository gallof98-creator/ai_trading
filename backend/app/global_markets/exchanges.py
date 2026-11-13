"""Exchange registry.

One entry per market that a connected provider can list. `code` is the platform's short code;
`providers` holds each provider's own code for the same market. Whether a provider REALLY covers
an exchange is never assumed from this table: the coverage report is built from what each
synchronisation actually returned.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Exchange:
    code: str
    name: str
    country: str  # ISO 3166-1 alpha-2
    currency: str  # main trading currency
    region: str
    mic: str  # primary ISO 10383 market identifier
    timezone: str
    providers: dict[str, str] = field(default_factory=dict)
    market: str | None = None  # platform market code (instruments.registry.MARKETS) when prices/backtests are supported
    note: str = ""


def _x(code, name, country, currency, region, mic, tz, finnhub=None, eodhd=None, openfigi=None, market=None, note="", **extra) -> Exchange:
    providers = {k: v for k, v in {"finnhub": finnhub, "eodhd": eodhd, "openfigi": openfigi, **extra}.items() if v}
    return Exchange(code, name, country, currency, region, mic, tz, providers, market, note)


NA, EU, AS, OC, OT = "North America", "Europe", "Asia", "Oceania", "Other"
EXCHANGES: dict[str, Exchange] = {e.code: e for e in [
    # --- North America
    _x("US", "United States (NYSE, Nasdaq, NYSE American, NYSE Arca, Cboe)", "US", "USD", NA, "XNYS", "America/New_York",
       finnhub="US", eodhd="US", openfigi="US", alphavantage="US", market="US", note="One consolidated listing; each security carries its own venue (MIC)."),
    _x("TO", "Toronto Stock Exchange", "CA", "CAD", NA, "XTSE", "America/Toronto", finnhub="TO", eodhd="TO", openfigi="CT"),
    _x("V", "TSX Venture Exchange", "CA", "CAD", NA, "XTSX", "America/Toronto", finnhub="V", eodhd="V", openfigi="CV"),
    # --- Europe
    _x("L", "London Stock Exchange", "GB", "GBP", EU, "XLON", "Europe/London", finnhub="L", eodhd="LSE", openfigi="LN",
       note="Most shares are quoted in pence (GBX)."),
    _x("DE", "Xetra", "DE", "EUR", EU, "XETR", "Europe/Berlin", finnhub="DE", eodhd="XETRA", openfigi="GY", market="DE"),
    _x("F", "Frankfurt Stock Exchange", "DE", "EUR", EU, "XFRA", "Europe/Berlin", finnhub="F", eodhd="F", openfigi="GF"),
    _x("PA", "Euronext Paris", "FR", "EUR", EU, "XPAR", "Europe/Paris", finnhub="PA", eodhd="PA", openfigi="FP", market="FR"),
    _x("AS", "Euronext Amsterdam", "NL", "EUR", EU, "XAMS", "Europe/Amsterdam", finnhub="AS", eodhd="AS", openfigi="NA", market="NL"),
    _x("BR", "Euronext Brussels", "BE", "EUR", EU, "XBRU", "Europe/Brussels", finnhub="BR", eodhd="BR", openfigi="BB"),
    _x("MI", "Euronext Milan (Borsa Italiana)", "IT", "EUR", EU, "XMIL", "Europe/Rome", finnhub="MI", eodhd="MI", openfigi="IM", market="IT"),
    _x("LS", "Euronext Lisbon", "PT", "EUR", EU, "XLIS", "Europe/Lisbon", finnhub="LS", eodhd="LS", openfigi="PL"),
    _x("IR", "Euronext Dublin", "IE", "EUR", EU, "XDUB", "Europe/Dublin", finnhub="IR", eodhd="IR", openfigi="ID"),
    _x("SW", "SIX Swiss Exchange", "CH", "CHF", EU, "XSWX", "Europe/Zurich", finnhub="SW", eodhd="SW", openfigi="SE", market="CH"),
    _x("MC", "Bolsa de Madrid", "ES", "EUR", EU, "XMAD", "Europe/Madrid", finnhub="MC", eodhd="MC", openfigi="SM", market="ES"),
    _x("ST", "Nasdaq Stockholm", "SE", "SEK", EU, "XSTO", "Europe/Stockholm", finnhub="ST", eodhd="ST", openfigi="SS"),
    _x("HE", "Nasdaq Helsinki", "FI", "EUR", EU, "XHEL", "Europe/Helsinki", finnhub="HE", eodhd="HE", openfigi="FH"),
    _x("CO", "Nasdaq Copenhagen", "DK", "DKK", EU, "XCSE", "Europe/Copenhagen", finnhub="CO", eodhd="CO", openfigi="DC"),
    _x("IC", "Nasdaq Iceland", "IS", "ISK", EU, "XICE", "Atlantic/Reykjavik", finnhub="IC", eodhd="IC", openfigi="IR"),
    _x("OL", "Oslo Børs", "NO", "NOK", EU, "XOSL", "Europe/Oslo", finnhub="OL", eodhd="OL", openfigi="NO"),
    _x("VI", "Vienna Stock Exchange", "AT", "EUR", EU, "XWBO", "Europe/Vienna", finnhub="VI", eodhd="VI", openfigi="AV"),
    _x("WA", "Warsaw Stock Exchange", "PL", "PLN", EU, "XWAR", "Europe/Warsaw", finnhub="WA", eodhd="WAR", openfigi="PW"),
    _x("AT", "Athens Stock Exchange", "GR", "EUR", EU, "XATH", "Europe/Athens", finnhub="AT", eodhd="AT", openfigi="GA"),
    _x("IS", "Borsa Istanbul", "TR", "TRY", EU, "XIST", "Europe/Istanbul", finnhub="IS", eodhd="IS", openfigi="TI"),
    # --- Asia
    _x("T", "Tokyo Stock Exchange", "JP", "JPY", AS, "XTKS", "Asia/Tokyo", finnhub="T", eodhd="TSE", openfigi="JT"),
    _x("HK", "Hong Kong Stock Exchange", "HK", "HKD", AS, "XHKG", "Asia/Hong_Kong", finnhub="HK", eodhd="HK", openfigi="HK"),
    _x("SS", "Shanghai Stock Exchange", "CN", "CNY", AS, "XSHG", "Asia/Shanghai", finnhub="SS", eodhd="SHG", openfigi="CG"),
    _x("SZ", "Shenzhen Stock Exchange", "CN", "CNY", AS, "XSHE", "Asia/Shanghai", finnhub="SZ", eodhd="SHE", openfigi="CS"),
    _x("NS", "National Stock Exchange of India", "IN", "INR", AS, "XNSE", "Asia/Kolkata", finnhub="NS", eodhd="NSE", openfigi="IS"),
    _x("BO", "Bombay Stock Exchange", "IN", "INR", AS, "XBOM", "Asia/Kolkata", finnhub="BO", eodhd="BSE", openfigi="IB"),
    _x("SI", "Singapore Exchange", "SG", "SGD", AS, "XSES", "Asia/Singapore", finnhub="SI", eodhd="SG", openfigi="SP"),
    _x("KS", "Korea Exchange (KOSPI)", "KR", "KRW", AS, "XKRX", "Asia/Seoul", finnhub="KS", eodhd="KO", openfigi="KP"),
    _x("KQ", "Korea Exchange (KOSDAQ)", "KR", "KRW", AS, "XKOS", "Asia/Seoul", finnhub="KQ", eodhd="KQ", openfigi="KQ"),
    _x("TW", "Taiwan Stock Exchange", "TW", "TWD", AS, "XTAI", "Asia/Taipei", finnhub="TW", eodhd="TW", openfigi="TT"),
    _x("BK", "Stock Exchange of Thailand", "TH", "THB", AS, "XBKK", "Asia/Bangkok", finnhub="BK", eodhd="BK", openfigi="TB"),
    _x("JK", "Indonesia Stock Exchange", "ID", "IDR", AS, "XIDX", "Asia/Jakarta", finnhub="JK", eodhd="JK", openfigi="IJ"),
    _x("KL", "Bursa Malaysia", "MY", "MYR", AS, "XKLS", "Asia/Kuala_Lumpur", finnhub="KL", eodhd="KLSE", openfigi="MK"),
    _x("TA", "Tel Aviv Stock Exchange", "IL", "ILS", AS, "XTAE", "Asia/Jerusalem", finnhub="TA", eodhd="TA", openfigi="IT"),
    # --- Oceania
    _x("AX", "Australian Securities Exchange", "AU", "AUD", OC, "XASX", "Australia/Sydney", finnhub="AX", eodhd="AU", openfigi="AT"),
    _x("NZ", "New Zealand Exchange", "NZ", "NZD", OC, "XNZE", "Pacific/Auckland", finnhub="NZ", eodhd="NZ", openfigi="NZ"),
    # --- Other
    _x("SA", "B3 (Brazil)", "BR", "BRL", OT, "BVMF", "America/Sao_Paulo", finnhub="SA", eodhd="SA", openfigi="BS"),
    _x("MX", "Mexican Stock Exchange", "MX", "MXN", OT, "XMEX", "America/Mexico_City", finnhub="MX", eodhd="MX", openfigi="MM"),
    _x("JO", "Johannesburg Stock Exchange", "ZA", "ZAR", OT, "XJSE", "Africa/Johannesburg", finnhub="JO", eodhd="JSE", openfigi="SJ"),
    _x("SN", "Santiago Stock Exchange", "CL", "CLP", OT, "XSGO", "America/Santiago", finnhub="SN", eodhd="SN", openfigi="CC"),
]}

US_VENUE_MIC = {"NYSE": "XNYS", "NASDAQ": "XNAS", "NYSE ARCA": "ARCX", "NYSE MKT": "XASE", "NYSE AMERICAN": "XASE", "AMEX": "XASE", "BATS": "BATS",
                "CBOE": "BATS"}


def get_exchange(code: str) -> Exchange:
    try:
        return EXCHANGES[code]
    except KeyError:
        raise ValueError(f"Unknown exchange {code!r}") from None


def exchanges_for(provider: str) -> list[Exchange]:
    return [e for e in EXCHANGES.values() if provider in e.providers]
