"""SEC EDGAR (data.sec.gov): the official filing index and the figures companies report in XBRL.

No API key. The SEC requires a User-Agent that names who is asking and a contact e-mail, and allows at
most 10 requests a second; this source stays at about 6. Without a configured User-Agent it is disabled.

Point in time: a reported figure becomes public when its filing is accepted by EDGAR. `available_at` is
that acceptance time. The acceptance times in the SEC's JSON are written with a "Z" suffix but have been
observed to be US Eastern clock times; they are read as Eastern, which is the later of the two readings,
so nothing is ever treated as known too early. Where the acceptance time of a filing is not in the index
that was downloaded, 18:00 New York on the filing date is used.
"""
from __future__ import annotations

import pandas as pd

from ..http import ApiClient, SourceAuthError, SourceError, SourceNotFound
from ..lake import now_utc

DATA_URL = "https://data.sec.gov"
WWW_URL = "https://www.sec.gov"
NY = "America/New_York"


def cik10(cik) -> str:
    return f"{int(cik):010d}"


class SecSource:
    name = "sec"

    def __init__(self, user_agent: str, data_url: str = DATA_URL, www_url: str = WWW_URL) -> None:
        if not user_agent or "@" not in user_agent:
            raise SourceAuthError("SEC EDGAR needs a User-Agent with a name and a contact e-mail (QAT_SEC_USER_AGENT)")
        self._agent, self.data_url, self.www_url = user_agent, data_url, www_url

    def client(self, **kw) -> ApiClient:
        return ApiClient("SEC EDGAR", self.data_url, interval=0.17, headers={"User-Agent": self._agent, "Accept-Encoding": "gzip, deflate"}, **kw)

    async def tickers(self, client: ApiClient) -> dict[str, dict]:
        """ticker -> {cik, title}, from the SEC's own list."""
        body = await client.get_json("files/company_tickers.json", base_url=self.www_url)
        if not isinstance(body, dict):
            raise SourceError("SEC EDGAR returned an unexpected ticker list")
        return {str(r["ticker"]).upper(): {"cik": cik10(r["cik_str"]), "title": str(r.get("title") or "")} for r in body.values()
                if isinstance(r, dict) and r.get("ticker") and r.get("cik_str") is not None}

    async def filings(self, client: ApiClient, cik: str, ticker: str = "") -> pd.DataFrame:
        """The most recent filings of a company (the index page the SEC serves first: up to about 1,000 filings)."""
        body = await client.get_json(f"submissions/CIK{cik10(cik)}.json")
        recent = (body.get("filings") or {}).get("recent") if isinstance(body, dict) else None
        if not isinstance(recent, dict) or "accessionNumber" not in recent:
            raise SourceError("SEC EDGAR returned an unexpected filing index")
        received = now_utc()
        n = len(recent["accessionNumber"])
        column = lambda name: list(recent.get(name) or [None] * n)  # noqa: E731
        frame = pd.DataFrame({"accession": column("accessionNumber"), "form": column("form"), "filing_date": column("filingDate"),
                              "report_date": column("reportDate"), "accepted": column("acceptanceDateTime"),
                              "primary_document": column("primaryDocument"), "items": column("items"), "is_xbrl": column("isXBRL")})
        clock = pd.to_datetime(frame["accepted"].astype("string").str.replace("Z", "", regex=False).str.slice(0, 19), errors="coerce")
        frame["ts"] = clock.dt.tz_localize(NY, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC").dt.as_unit("us")
        frame = frame[frame["ts"].notna() & frame["accession"].notna()].drop(columns="accepted")
        for name in ("filing_date", "report_date"):
            frame[name] = pd.to_datetime(frame[name].replace("", None), errors="coerce").dt.tz_localize("UTC").dt.as_unit("us")
        frame["is_xbrl"] = frame["is_xbrl"].fillna(0).astype(int).astype(bool)
        frame["available_at"] = frame["ts"]
        frame["source"], frame["cik"], frame["ticker"], frame["received_at"] = self.name, cik10(cik), ticker.upper(), received
        return frame.reset_index(drop=True)

    async def facts(self, client: ApiClient, cik: str, ticker: str = "", accepted: pd.Series | None = None) -> pd.DataFrame:
        """Every XBRL fact the company has reported. `accepted` maps accession number -> acceptance time (from `filings`)."""
        try:
            body = await client.get_json(f"api/xbrl/companyfacts/CIK{cik10(cik)}.json")
        except SourceNotFound:
            raise SourceNotFound("SEC EDGAR has no XBRL facts for this company") from None
        if not isinstance(body, dict) or not isinstance(body.get("facts"), dict):
            raise SourceError("SEC EDGAR returned unexpected company facts")
        received = now_utc()
        rows = []
        for taxonomy, concepts in body["facts"].items():
            for concept, detail in concepts.items():
                for unit, items in (detail.get("units") or {}).items():
                    for f in items:
                        if f.get("val") is None or not f.get("end") or not f.get("accn") or not f.get("filed"):
                            continue
                        rows.append((taxonomy, concept, unit, f.get("start") or f["end"], f["end"], f["val"], f.get("fy"), f.get("fp"), f.get("form"),
                                     f["filed"], f["accn"], f.get("frame")))
        frame = pd.DataFrame(rows, columns=["taxonomy", "concept", "unit", "period_start", "period_end", "value", "fiscal_year", "fiscal_period", "form",
                                            "filed", "accession", "frame"])
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        for name in ("period_start", "period_end"):
            frame[name] = pd.to_datetime(frame[name], errors="coerce").dt.tz_localize("UTC").dt.as_unit("us")
        filed = pd.to_datetime(frame["filed"], errors="coerce")
        frame = frame[frame["value"].notna() & frame["period_end"].notna() & filed.notna()].copy()
        filed = filed.loc[frame.index]
        fallback = (filed + pd.Timedelta(hours=18)).dt.tz_localize(NY).dt.tz_convert("UTC").dt.as_unit("us")
        known = frame["accession"].map(accepted) if accepted is not None and len(accepted) else pd.Series(pd.NaT, index=frame.index)
        known = pd.to_datetime(known, utc=True).dt.as_unit("us")
        frame["available_at"] = known.fillna(fallback)
        frame["availability"] = known.notna().map({True: "accepted", False: "filing_date_rule"})
        frame["filed"] = filed.dt.tz_localize("UTC").dt.as_unit("us")
        frame["duration_days"] = (frame["period_end"] - frame["period_start"]).dt.days
        frame["fiscal_year"] = pd.to_numeric(frame["fiscal_year"], errors="coerce").astype("Int64")
        frame["source"], frame["cik"], frame["ticker"], frame["received_at"] = self.name, cik10(cik), ticker.upper(), received
        return frame.reset_index(drop=True)
