"""Company events and fundamentals for the entry filters (see filters.py): kept current, stored, and turned into the
blocks applied by the validation, the opportunity scan and the trading engine.

Sources, both official:
  * SEC EDGAR (no key; the SEC requires a User-Agent naming who asks, with a contact e-mail): the filing index of each
    followed US stock, for its results releases, and its reported XBRL figures, for the fundamentals;
  * Alpaca news (the key already used for Alpaca data): recent news per instrument, for the live news pause.
Stored compactly in the settings table: per stock its release times and only the reported figures the filters read.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select

from ..core.logging import get_logger
from ..database.models import AppSetting
from ..domain.models import AssetClass
from . import filters as flt

log = get_logger(__name__)
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
NEWS_KEEP = timedelta(hours=48)
FILINGS_EVERY = timedelta(hours=20)
FACTS_EVERY = timedelta(days=30)


def news_symbol(inst) -> str:
    return inst.symbol.replace("/", "") if inst.asset_class is AssetClass.CRYPTO else inst.symbol


class CompanyData:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.companies: dict[str, dict] = {}   # ticker -> {"cik", "releases": [iso], "facts": rows, "filings_at", "facts_at", "error"}
        self.news: dict[str, list[dict]] = {}  # news symbol -> [{"ts", "headline", "source", "symbols"}], newest first
        self.news_at: str | None = None
        self.tickers: dict[str, dict] = {}     # ticker -> {"cik", "title"} from the SEC
        self.tickers_at: str | None = None
        self._frames: dict[str, pd.DataFrame] = {}
        self._lock = threading.Lock()
        # Injected by tests; default to the real sources.
        self.fetch_tickers: Callable[[str], dict] | None = None
        self.fetch_filings: Callable[[str, str, str], pd.DataFrame] | None = None
        self.fetch_facts: Callable[[str, str, str, pd.Series], pd.DataFrame] | None = None
        self.fetch_news: Callable[[list[str], datetime, dict], list[dict]] | None = None

    # ------------------------------------------------------------------ storage
    def load(self) -> None:
        with self.rt.db.session() as session:
            for row in session.scalars(select(AppSetting).where(AppSetting.key.like("company:%"))):
                name = row.key.split(":", 1)[1]
                if name == "_news":
                    self.news, self.news_at = dict(row.value.get("items", {})), row.value.get("at")
                elif name == "_tickers":
                    self.tickers, self.tickers_at = dict(row.value.get("tickers", {})), row.value.get("at")
                else:
                    self.companies[name] = dict(row.value)
        self._frames.clear()

    def _save(self, key: str, value: dict) -> None:
        def write(session) -> None:
            row = session.get(AppSetting, f"company:{key}")
            if row is None:
                session.add(AppSetting(key=f"company:{key}", value=value))
            else:
                row.value = value

        self.rt.journal._write(write)

    # ------------------------------------------------------------------ what the filters read
    def settings(self) -> flt.FilterSettings:
        return self.rt.user_settings.autopilot.filters

    def _stock(self, instrument_id: str):
        try:
            inst = self.rt.registry.get(instrument_id)
        except KeyError:
            return None
        return inst if inst.asset_class is AssetClass.STOCK else None

    def releases(self, ticker: str) -> list[pd.Timestamp]:
        return [pd.Timestamp(t) for t in self.companies.get(ticker, {}).get("releases", [])]

    def facts(self, ticker: str) -> pd.DataFrame | None:
        rows = self.companies.get(ticker, {}).get("facts")
        if not rows:
            return None
        key = f"{ticker}:{self.companies[ticker].get('facts_at')}"
        with self._lock:
            if key not in self._frames:
                self._frames = {k: v for k, v in self._frames.items() if not k.startswith(f"{ticker}:")}
                self._frames[key] = flt.facts_frame(rows)
            return self._frames[key]

    def signature(self, instrument_id: str, timeframe: str) -> dict:
        """Which point-in-time filters a validation of this series includes: it is redone when this changes."""
        from ..market_data.timeframes import tf_delta

        cfg, inst = self.settings(), self._stock(instrument_id)
        ticker = inst.symbol if inst else None
        return {"earnings": bool(inst and cfg.earnings and self.companies.get(ticker, {}).get("releases")),
                "fundamentals": bool(inst and cfg.fundamentals and tf_delta(timeframe) >= pd.Timedelta(days=1) and self.companies.get(ticker, {}).get("facts"))}

    def masks(self, instrument_id: str, timeframe: str, index: pd.DatetimeIndex) -> tuple[pd.Series | None, pd.Series | None, dict]:
        """Entries refused on each bar (by bar start time), decided at the bar's close on what was known then."""
        sig = self.signature(instrument_id, timeframe)
        if not (sig["earnings"] or sig["fundamentals"]):
            return None, None, sig
        cfg, ticker = self.settings(), self.rt.registry.get(instrument_id).symbol
        closes = flt.bar_close_times(index, timeframe)
        blocked_any = blocked_long = None
        if sig["earnings"]:
            blocked_any = pd.Series(flt.blocked_by_windows(closes, flt.earnings_windows(self.releases(ticker), cfg)), index=index)
        if sig["fundamentals"]:
            blocked_long = pd.Series(flt.fundamentals_blocked(self.facts(ticker), closes, cfg), index=index)
        return blocked_any, blocked_long, sig

    def fundamentals_now(self, instrument_id: str, now: datetime) -> flt.Snapshot | None:
        inst = self._stock(instrument_id)
        facts = self.facts(inst.symbol) if inst else None
        return flt.snapshot(facts, pd.Timestamp(now)) if facts is not None else None

    def live_block(self, instrument_id: str, now: datetime, long: bool, timeframe: str | None) -> str | None:
        """Why a new position may not be opened now, or None. Never applies to closing orders (the caller checks)."""
        from ..market_data.timeframes import tf_delta

        cfg, ts = self.settings(), pd.Timestamp(now)
        try:
            inst = self.rt.registry.get(instrument_id)
        except KeyError:
            return None
        if inst.asset_class is AssetClass.STOCK:
            if cfg.earnings and (reason := flt.window_at(ts, flt.earnings_windows(self.releases(inst.symbol), cfg))):
                return f"earnings filter: {reason}"
            if cfg.fundamentals and long and timeframe and tf_delta(timeframe) >= pd.Timedelta(days=1):
                snap = self.fundamentals_now(instrument_id, now)
                if snap is not None and (why := snap.failures(cfg)):
                    return "fundamentals filter: " + "; ".join(why)
        if cfg.news and cfg.news_pause_minutes:
            since = ts - pd.Timedelta(minutes=cfg.news_pause_minutes)
            for item in self.news.get(news_symbol(inst), []):
                t = pd.Timestamp(item["ts"])
                if since <= t <= ts and len(item.get("symbols", [])) <= cfg.news_max_symbols:
                    return f"news filter: \"{item['headline'][:120]}\" ({item.get('source') or 'news'}, {t:%H:%M} UTC)"
        return None

    # ------------------------------------------------------------------ refreshing
    def _user_agent(self) -> str:
        return self.rt.settings.sec_user_agent or self.rt.user_settings.autopilot.sec_contact

    def _sec(self, call):
        from ..datahub.sources.sec import SecSource

        source = SecSource(self._user_agent())

        async def run():
            async with source.client() as client:
                return await call(source, client)

        return asyncio.run(run())

    def refresh_sec(self, instrument_ids: list[str], now: datetime, budget: int = 40) -> dict:
        """Filing index (daily) and reported figures (after a new 10-Q/10-K, or monthly) of the followed US stocks."""
        cfg = self.settings()
        if not (cfg.earnings or cfg.fundamentals):
            return {"skipped": "earnings and fundamentals filters are off"}
        stocks = [i.symbol for i in (self._stock(iid) for iid in instrument_ids) if i]
        if not stocks:
            return {"skipped": "no US stock followed"}
        tickers_fresh = self.tickers_at and now - datetime.fromisoformat(self.tickers_at) < timedelta(days=7)
        if not tickers_fresh:
            try:
                fetch = self.fetch_tickers or (lambda ua: self._sec(lambda s, c: s.tickers(c)))
                self.tickers, self.tickers_at = fetch(self._user_agent()), now.isoformat()
                self._save("_tickers", {"tickers": self.tickers, "at": self.tickers_at})
            except Exception as exc:  # no network, User-Agent refused
                return {"error": f"SEC ticker list unavailable: {type(exc).__name__}: {str(exc)[:200]}"}
        due = []
        for t in stocks:
            c = self.companies.get(t, {})
            if not c.get("filings_at") or now - datetime.fromisoformat(c["filings_at"]) >= FILINGS_EVERY:
                due.append(t)
        filings_done = facts_done = 0
        missing, errors = [], []
        for ticker in due[:budget]:
            info = self.tickers.get(ticker.replace(".", "-")) or self.tickers.get(ticker)
            if info is None:
                missing.append(ticker)
                self.companies[ticker] = {**self.companies.get(ticker, {}), "filings_at": now.isoformat(), "error": "not in the SEC company list"}
                continue
            company = dict(self.companies.get(ticker, {}), cik=info["cik"])
            try:
                fetch = self.fetch_filings or (lambda cik, tk, ua: self._sec(lambda s, c: s.filings(c, cik, tk)))
                filings = fetch(info["cik"], ticker, self._user_agent())
                company["releases"] = [t.isoformat() for t in flt.releases_from_filings(filings)]
                company["filings_at"], company["error"] = now.isoformat(), None
                filings_done += 1
                periodic = filings[filings["form"].isin(["10-Q", "10-K", "10-Q/A", "10-K/A"])]
                newest = pd.to_datetime(periodic["available_at"], utc=True).max() if len(periodic) else None
                stale = (not company.get("facts_at") or now - datetime.fromisoformat(company["facts_at"]) >= FACTS_EVERY
                         or (newest is not None and pd.Timestamp(company["facts_at"]) < newest))
                if cfg.fundamentals and stale:
                    accepted = filings.set_index("accession")["available_at"]
                    fetch_f = self.fetch_facts or (lambda cik, tk, ua, acc: self._sec(lambda s, c: s.facts(c, cik, tk, acc)))
                    facts = fetch_f(info["cik"], ticker, self._user_agent(), accepted)
                    keep = facts[(facts["concept"].isin(flt.CONCEPTS)) & (facts["unit"] == "USD")]
                    company["facts"] = [[r.concept, r.period_start.isoformat(), r.period_end.isoformat(), float(r.value), r.available_at.isoformat()]
                                        for r in keep.itertuples()]
                    company["facts_at"] = now.isoformat()
                    facts_done += 1
            except Exception as exc:
                company["filings_at"], company["error"] = now.isoformat(), f"{type(exc).__name__}: {str(exc)[:200]}"
                errors.append(f"{ticker} ({company['error'][:80]})")
            self.companies[ticker] = company
            self._save(ticker, company)
        return {"stocks": len(stocks), "filings_refreshed": filings_done, "fundamentals_refreshed": facts_done, "still_due": max(0, len(due) - budget),
                "not_at_sec": missing[:20], "errors": errors[:10]}

    def refresh_news(self, instrument_ids: list[str], now: datetime) -> dict:
        cfg = self.settings()
        if not cfg.news:
            return {"skipped": "news filter is off"}
        known = {i.id: i for i in self.rt.registry.all()}
        symbols = sorted({news_symbol(known[x]) for x in instrument_ids if x in known})
        if not symbols:
            return {"skipped": "nothing followed"}
        since = max(pd.Timestamp(self.news_at) if self.news_at else pd.Timestamp(now) - pd.Timedelta(hours=24),
                    pd.Timestamp(now) - pd.Timedelta(hours=24)).to_pydatetime()
        creds = next((c for c, _ in self.rt.alpaca_credentials() if c.get("api_key") and c.get("api_secret")), None)
        if creds is None and self.fetch_news is None:
            return {"error": "no Alpaca key stored: news needs one"}
        items: list[dict] = []
        try:
            for k in range(0, len(symbols), 50):
                items += (self.fetch_news or alpaca_news)(symbols[k:k + 50], since, creds or {})
        except Exception as exc:
            return {"error": f"Alpaca news unavailable: {type(exc).__name__}: {str(exc)[:200]}"}
        horizon = pd.Timestamp(now) - NEWS_KEEP
        merged = {s: [x for x in v if pd.Timestamp(x["ts"]) >= horizon] for s, v in self.news.items()}
        seen = {(s, x["id"]) for s, v in merged.items() for x in v if "id" in x}
        added = 0
        for item in items:
            for s in item.get("symbols", []):
                if s in symbols and (s, item.get("id")) not in seen:
                    merged.setdefault(s, []).append({k: item.get(k) for k in ("id", "ts", "headline", "source", "symbols")})
                    seen.add((s, item.get("id")))
                    added += 1
        self.news = {s: sorted(v, key=lambda x: x["ts"], reverse=True)[:50] for s, v in merged.items() if v}
        self.news_at = now.isoformat()
        self._save("_news", {"items": self.news, "at": self.news_at})
        return {"symbols": len(symbols), "new_items": added}

    def status(self) -> dict:
        with_releases = sum(bool(c.get("releases")) for c in self.companies.values())
        with_facts = sum(bool(c.get("facts")) for c in self.companies.values())
        errors = {t: c["error"] for t, c in self.companies.items() if c.get("error")}
        return {"companies": len(self.companies), "with_earnings_dates": with_releases, "with_fundamentals": with_facts,
                "news_symbols": len(self.news), "news_updated": self.news_at, "errors": dict(list(errors.items())[:20]),
                "sec_contact": self._user_agent()}


def alpaca_news(symbols: list[str], since: datetime, creds: dict, client=None, pages: int = 5) -> list[dict]:
    """Recent news for these symbols from Alpaca (sorted newest first; at most `pages` pages of 50)."""
    import httpx

    own = client is None
    client = client or httpx.Client(timeout=15)
    out, token = [], None
    try:
        for _ in range(pages):
            params = {"symbols": ",".join(symbols), "start": pd.Timestamp(since).strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 50, "sort": "desc"}
            if token:
                params["page_token"] = token
            r = client.get(NEWS_URL, params=params, headers={"APCA-API-KEY-ID": creds.get("api_key", ""), "APCA-API-SECRET-KEY": creds.get("api_secret", "")})
            if r.status_code in (401, 403):
                raise PermissionError(f"Alpaca refused the key for news ({r.status_code})")
            r.raise_for_status()
            body = r.json()
            for n in body.get("news", []):
                out.append({"id": n.get("id"), "ts": pd.Timestamp(n.get("created_at")).tz_convert("UTC").isoformat(), "headline": n.get("headline") or "",
                            "source": n.get("source") or "", "symbols": list(n.get("symbols") or [])})
            token = body.get("next_page_token")
            if not token:
                break
    finally:
        if own:
            client.close()
    return out


