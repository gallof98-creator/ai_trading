"""What each data source offers on its free plan, and what it needs before it can be switched on.

These facts are shown in the dashboard API and in docs/data-platform.md. They describe free tiers as
documented by the providers in November 2026; plans change, so the request budgets are settings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    datasets: tuple[str, ...]
    needs: str  # "" (nothing) | api_key | user_agent | alpaca
    timeliness: str  # how current the data is
    history: str
    rate_limit: str
    licence: str
    limitations: str
    signup_url: str = ""
    daily_budget: int | None = None  # requests; enforced before every call
    monthly_budget: int | None = None
    verified: str = "Response handling is tested against the documented response shapes. Not yet exercised against the live service from this installation: run the provider test once after setup."

    def to_dict(self) -> dict:
        return asdict(self)


PROVIDERS: dict[str, ProviderInfo] = {p.name: p for p in (
    ProviderInfo(
        "fred", "FRED / ALFRED (St. Louis Fed)", ("macro",), "api_key",
        timeliness="Published figures, on the day of release. Not a real-time feed.",
        history="Full history of each series (decades). Revised series come with every published vintage (ALFRED).",
        rate_limit="120 requests a minute per key; paced at about 109.",
        licence="Free with a key. FRED's terms require the notice 'This product uses the FRED® API but is not endorsed or certified by the "
                "Federal Reserve Bank of St. Louis'; some series belong to third parties with their own terms.",
        limitations="Only the release date is recorded, not the time of day, so figures are treated as known from 18:00 New York on that date. "
                    "Daily market series are not downloaded with vintages (too many); they use a next-business-day rule.",
        signup_url="https://fred.stlouisfed.org/docs/api/api_key.html"),
    ProviderInfo(
        "sec", "SEC EDGAR", ("filings", "fundamentals"), "user_agent",
        timeliness="Filings appear within seconds of acceptance; the XBRL figures within about a minute. Polled, not streamed.",
        history="Filing index: the most recent filings of each company (about 1,000). XBRL figures: from 2009 onwards, when tagging began.",
        rate_limit="10 requests a second for everyone; paced at about 6.",
        licence="Public domain, no key. A User-Agent naming the requester and a contact e-mail is mandatory.",
        limitations="Only what companies tag in XBRL: figures as reported, with restatements as separate facts. No ratios, no estimates. "
                    "Foreign filers reporting under IFRS use different concept names."),
    ProviderInfo(
        "kraken", "Kraken (public market data)", ("bars", "trades", "orderbook"), "",
        timeliness="Real time at the moment of each request (polled).",
        history="Candles: only the most recent 720 per interval (12 hours of one-minute bars). Trades: full history, 1,000 per request.",
        rate_limit="Public endpoints are limited per address; about one request a second, paced by CCXT.",
        licence="Public endpoints, no key. Subject to Kraken's API terms; redistribution of the data is not granted.",
        limitations="No historical order books. Deep intraday candles have to be rebuilt from the trade history."),
    ProviderInfo(
        "binance", "Binance (public market data)", ("bars", "trades", "orderbook"), "",
        timeliness="Real time at the moment of each request (polled).",
        history="Candles back to each pair's listing (2017 for the oldest), 1,000 per request. Aggregated trades in one-hour windows.",
        rate_limit="Weight-based per address (several thousand a minute); paced by CCXT far below the limit.",
        licence="Public market-data host (data-api.binance.vision), no key. Subject to Binance's terms; redistribution is not granted.",
        limitations="No historical order books. Prices are in USDT or other quote assets, not US dollars: never mixed with USD venues. "
                    "The main Binance API is not reachable from some countries; the market-data host used here normally is."),
    ProviderInfo(
        "coingecko", "CoinGecko", ("crypto_market", "crypto_venues"), "",
        timeliness="Aggregates refreshed about once a minute. Not a tradable price.",
        history="One year of daily and hourly history on the free plan.",
        rate_limit="Without a key: shared and slow (paced at one request every 7 s). With a free Demo key: paced at one every 2.5 s.",
        licence="Free plan needs attribution ('Data provided by CoinGecko') wherever the data is shown, and is not licensed for commercial redistribution.",
        limitations="A blended price across exchanges; five-minute data only for the last day.",
        signup_url="https://www.coingecko.com/en/api/pricing", monthly_budget=9000),
    ProviderInfo(
        "fmp", "Financial Modeling Prep", ("company_profile",), "api_key",
        timeliness="Reference data, refreshed by the vendor daily.",
        history="Snapshots from the day they are first collected here (the vendor does not publish a history of profiles).",
        rate_limit="250 requests a day on the free plan.",
        licence="Free plan is for personal use; no redistribution or display rights.",
        limitations="Used for company reference data only. Its financial statements carry no filing time and the free plan limits them to "
                    "five annual periods, so reported figures come from SEC EDGAR instead. Calendars and peers are paid features.",
        signup_url="https://site.financialmodelingprep.com/developer/docs", daily_budget=240),
    ProviderInfo(
        "alpaca", "Alpaca (intraday history)", ("bars",), "alpaca",
        timeliness="Consolidated tape delayed by 15 minutes on the free plan (crypto bars are current).",
        history="Minute bars from 2016 for US stocks and ETFs; crypto from 2021.",
        rate_limit="About 200 requests a minute; 10,000 bars per request.",
        licence="For the account holder's own use under Alpaca's market-data agreement; no redistribution.",
        limitations="Not real time for stocks. Adjusted prices change after later splits and dividends: re-download a series after a corporate action.",
        signup_url="https://alpaca.markets"),
)}

# One small request per source, to confirm that it answers and that the plan includes the data.
PROBES: dict[str, dict] = {
    "fred": {"dataset": "macro", "keys": ["DGS10"], "options": {"pit": "lag"}},
    "sec": {"dataset": "filings", "keys": ["AAPL"]},
    "kraken": {"dataset": "bars", "keys": ["BTC/USD"], "timeframe": "1m", "options": {"days": 1}},
    "binance": {"dataset": "bars", "keys": ["BTC/USDT"], "timeframe": "1m", "options": {"days": 1}},
    "coingecko": {"dataset": "crypto_market", "keys": ["bitcoin"], "options": {"days": 1}},
    "fmp": {"dataset": "company_profile", "keys": ["AAPL"]},
    "alpaca": {"dataset": "bars", "keys": ["SPY"], "timeframe": "1m", "options": {"days": 1}},
}
