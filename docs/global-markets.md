# Global Equity Market Intelligence module

Status by phase. "Done" means implemented and covered by automated tests; anything not verified
against a real service says so.

| Phase | Scope | Status |
|---|---|---|
| 1 | Global catalog: exchange registry, discovery, identifiers, types, history, de-duplication, synchronisation, coverage report, dashboard section | **Done** |
| 2 | Prices at scale: bulk downloads, price support for every registered exchange (calendars, currencies, pence quotes), storage for 100,000+ instruments | Not started |
| 3 | Market scanner: daily metrics for every instrument, filters, saved and scheduled scans | Not started |
| 4 | AI stock analyst and transparent rankings | Not started |
| 5 | Cross-sectional strategies and portfolio optimisation, connected to the risk engine and backtester | Not started |
| 6 | Remaining dashboard parts: heatmaps, top movers, watchlists | Not started |

Live trading is not touched by this module.

## Phase 1: what exists

- **Exchange registry** (`app/global_markets/exchanges.py`): 43 exchanges in North America, Europe, Asia, Oceania and other regions, each with its
  ISO MIC, country, currency, timezone and the code every provider uses for it.
- **Discovery** (`discovery.py`): one interface, three providers.

  | Provider | Cost | Lists | Identifiers | Checked against the real service |
  |---|---|---|---|---|
  | Alpha Vantage | free | United States | none (added through OpenFIGI) | Yes: 14,523 listings imported |
  | Finnhub | free | all registered exchanges it has a code for | FIGI, share-class FIGI; ISIN on some plans | No: written from documentation |
  | EODHD | paid | all registered exchanges it has a code for | ISIN | No: the demo key cannot call symbol lists |
  | OpenFIGI | free | (identifier mapping only) | FIGI, composite FIGI, share-class FIGI, security type | Yes |

- **Catalog** (`catalog.py`, table `securities`): one row per listing with a platform id that never changes. A listing is recognised by FIGI, then
  ISIN on that exchange, then ticker; a ticker match is refused when both sides carry different ISINs or FIGIs (a reused ticker is a different
  security). Every change is written to `security_events`: listed, delisted, relisted, ticker changed, name changed, type changed, identifier added.
- **Delisting**: a security a provider stops listing is marked missing for that provider and becomes delisted only when no provider lists it any
  more. If a provider answers with less than half of what it listed before, the delisting step is skipped as a probable incomplete answer.
- **Instrument types**: common stock, preferred stock, ETF, REIT, ADR, depositary receipt, closed-end fund, other. Unknown types are "other",
  never assumed to be stocks. Equity-only counts include common stock only. Alpha Vantage only says "Stock" or "ETF": OpenFIGI then separates
  REITs and ADRs as identifiers are added.
- **Synchronisation**: background job per provider over the chosen exchanges, with a result per exchange, followed by identifier enrichment.
  `QAT_CATALOG_SYNC_HOURS` repeats it automatically.
- **Coverage report** (`GET /api/global/coverage` and the dashboard): per exchange and provider, what the last synchronisation actually returned,
  counts of discovered, active and delisted listings, identifiers, price history. It never claims completeness.
- **Trading universe**: catalog securities can be added to the trading universe (and then get prices and backtests) on the seven markets the
  engines support today: United States, Italy, Germany (Xetra), France, Netherlands, Spain, Switzerland.

## Known limits of Phase 1

- **Provider codes for exchanges are partly from memory** (especially OpenFIGI and EODHD codes for less common exchanges). A wrong code shows up
  as a failed or empty synchronisation in the coverage report, not as wrong data; it then needs correcting in the registry.
- **Finnhub and EODHD discovery have not run against the real services.** The saved Finnhub key was rejected in the build environment.
- **Delistings are only known from the first synchronisation onwards**, and mergers are not identified as such: an acquired company simply
  appears as delisted. Corporate actions are not ingested yet.
- **Sector, industry and market capitalisation are empty**: no connected free source provides them. SEC EDGAR for US companies is planned.
- **No real-time or delayed quotes** are connected for the catalog; the report states zero for both.
- **Identifier enrichment without an OpenFIGI key is slow** (about 250 securities a minute); a free key makes it about 40 times faster.
- **Prices and trading for the other 36 exchanges** (London, Tokyo, Hong Kong and so on) are not available until Phase 2.
- Synchronisation runs as a background thread inside the API process, not on a separate worker queue.
