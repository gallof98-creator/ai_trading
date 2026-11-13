# Global Equity Market Intelligence module

Status by phase. "Done" means implemented and covered by automated tests; anything not verified
against a real service says so.

| Phase | Scope | Status |
|---|---|---|
| 1 | Global catalog: exchange registry, discovery, identifiers, types, history, de-duplication, synchronisation, coverage report, dashboard section | **Done** |
| 2 | Prices at scale: bulk downloads, incremental updates, instruments and currency units for every registered exchange, provider usage counters | **Done**, with the limits listed below |
| 3 | Market scanner: daily metrics for every instrument, filters, saved and scheduled scans | Done |
| 4 | AI stock analyst and transparent rankings | Done |
| 5 | Cross-sectional strategies and portfolio optimisation, connected to the risk engine and backtester | Done |
| 6 | Remaining dashboard parts: heatmaps, top movers, watchlists | Done |

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
- **Trading universe**: catalog securities of any registered exchange can be added to the trading universe; prices then come from whichever
  provider covers that exchange (see Phase 2).

## Phase 2: what exists

- **Alpaca connector** (free account): daily bars for US stocks and ETFs, up to 50 symbols per request, adjusted for splits and dividends,
  history from 2016. About 8,000 US stocks need a few hundred requests instead of one request each.
- **Batched downloads**: the download job groups instruments by the provider's batch size, stores each instrument in its own transaction,
  lists every symbol the provider had no data for, and stops (resumable) when a plan's allowance runs out.
- **Incremental updates** ("Update prices"): only the most recent days are requested and appended. The overlap with stored bars is compared
  first: if the vendor changed past prices (a split or dividend re-adjusts the history), that instrument's whole history is downloaded again,
  so a series never mixes two adjustment bases. A gap between stored and new bars also triggers a full download.
- **Every registered exchange can hold instruments**: all 43 exchanges are markets for the trading universe, with identifiers such as
  `STOCK:7203.T`, `STOCK:VOD.L`, `STOCK:0700.HK`. London, Johannesburg and Tel Aviv prices are quoted in pence, cents and agorot and are
  converted with the unit factor.
- **Provider usage**: requests today and distinct symbols this month are counted per provider and shown next to its limits.
- **Coverage report**: names, per exchange, the providers that can supply prices, and counts stale series (newest bar older than a week).

Measured on SQLite on the build machine: storing 10 years of daily bars takes about 0.03 s per instrument (87,000 bars a second) and
0.33 GB per 1,000 instruments; listing coverage and loading one series stay in the millisecond range because of the series summary table.

### Known limits of Phase 2

- **Alpaca has not run against the real service** (it was not reachable from the build environment): press "Test connection" first.
- **Outside the United States only EODHD (paid) has prices.** The other 42 exchanges can be catalogued and their securities added to the
  universe, but their histories stay empty without a paid key or imported files.
- **Storage is still one relational table.** At the measured size, 10 years for 8,000 US stocks is about 3 GB and 100,000 global listings
  about 35 GB. Postgres can hold that, but it has not been load-tested here, and SQLite should not be used at that size. A partitioned or
  columnar store is the next step if the full global universe is downloaded.
- **Markets added in this phase use simplified rules**: Monday-to-Friday calendar without holidays or session hours, a lot size of one
  share and a generic tick size. That is adequate for daily-bar research; it is not adequate for sending orders to those exchanges.
- **Securities in a different currency than their exchange's default** (for example dollar lines in London) are valued in the exchange's
  default unit unless the listing provider reports their currency.
- **EODHD's one-request-per-exchange daily file is not used yet**, because it cannot be verified without a paid key; updates go symbol by
  symbol for that provider.
- Downloads still run as a background thread inside the API process, not on a separate worker queue.

## Known limits of Phase 1

- **Provider codes for exchanges are partly from memory** (especially OpenFIGI and EODHD codes for less common exchanges). A wrong code shows up
  as a failed or empty synchronisation in the coverage report, not as wrong data; it then needs correcting in the registry.
- **Finnhub and EODHD discovery have not run against the real services.** The saved Finnhub key was rejected in the build environment.
- **Delistings are only known from the first synchronisation onwards**, and mergers are not identified as such: an acquired company simply
  appears as delisted. Corporate actions are not ingested yet.
- **Sector, industry and market capitalisation are empty**: no connected free source provides them. SEC EDGAR for US companies is planned.
- **No real-time or delayed quotes** are connected for the catalog; the report states zero for both.
- **Identifier enrichment without an OpenFIGI key is slow** (about 250 securities a minute); a free key makes it about 40 times faster.
- Synchronisation runs as a background thread inside the API process, not on a separate worker queue.

## Phase 3: scanner

- **Daily metrics** (`app/global_markets/metrics.py`): about thirty values per instrument (returns, relative volume, volatility, ATR, RSI, MACD,
  moving-average distances, ADX, 12-1 momentum, 52-week high, breakouts, regime). A value at date D uses bars up to D only; a field
  that needs more history than is stored is empty, never estimated. Recomputed automatically after each price download.
- **Scanner** (`scanner.py`, `/api/scanner/*`): filters on any metric, sorting, universe by market, country, type or watchlist; nine presets;
  saved scans that run after every update and notify new matches; watchlists.

## Phase 4: analysis and rankings

- **Rankings** (`rankings.py`): seven documented models (momentum 12-1, relative strength, trend quality, low volatility, short-term reversal,
  breakout, multi-factor). Each factor is winsorised at the 2.5th/97.5th percentile, standardised across eligible instruments, signed and
  weighted; the per-factor contributions are returned with each row. Eligibility: equity-like, price and traded-value floors.
  Every ranking shows whether it has been backtested on your data and with what result.
- **Market overview**: breadth, performance by exchange / country / sector, top movers, unusual volume, volatility expansion, breakout
  candidates, abnormal moves (robust z-score above 5 against the day's cross-section), regime counts and changes. A written report can be
  stored with the other research reports.
- This is descriptive statistics. No language model writes it and nothing in it is a forecast.

## Phase 5: portfolio strategies

- **Selection and weights** (`cross_section.py`): the ranking model picks the top N; weights are equal, inverse-volatility or long-only
  minimum-variance; caps per instrument, country, currency and sector; optional volatility target and country/sector rotation.
  A cap only ever reduces weight: the remainder stays in cash.
- **Backtest**: decisions at the close of the first trading day of each week or month, fills at the next open, whole shares, flat cost per
  traded value, participation limit against 20-day traded value, protective stop per position (filled at the stop, or at the open after a gap).
  Compared with an equal-weight universe and split into two sub-periods. Tests check it against a hand calculation and against tampered future data.
- **Paper rebalancing** (`rebalance.py`): a proposal lists the orders, each sized by the tightest of target weight, risk per trade at the stop
  and the exposure headroom, then judged by the central risk engine. Nothing is ordered until a person sends the proposal; each order is
  judged again at that moment. Orders fill at the open of the next stored daily bar and each purchase gets a protective stop.
  Only paper accounts are accepted; a live account is refused.

### Known limits of Phases 3-5

- **Survivorship bias**: the catalog holds what is listed now plus delistings seen since the first sync. Backtests therefore miss companies
  that disappeared earlier, which flatters results by an unknown amount.
- **No currency conversion in backtests and rankings**: prices in different currencies are compared in local terms (as if currency were hedged
  at no cost), and the price / traded-value floors are in local units, so they are far looser for currencies such as JPY. Restrict a test to
  markets with one currency for clean numbers. Paper rebalancing does convert, using the platform's exchange rates.
- **Sectors are mostly empty**: none of the free sources connected supplies sector or industry, so sector heatmaps, sector caps and sector
  rotation only work for instruments that have one. The SEC EDGAR enrichment discussed for US companies is not built.
- **No fundamentals**: value and quality factors are not available; every model is price- and volume-based.
- **Positions are not resized between rebalances** and buys refused for lack of room are not retried automatically: prepare a new proposal
  after the sells have filled.
- **At most 2,000 instruments per backtest**, loaded in memory; a larger selection is refused rather than sampled.
- **Daily data only**: paper orders of these strategies fill on the next daily bar you download, not in real time.
- Rebalancing is not scheduled: a person prepares and sends each proposal.
