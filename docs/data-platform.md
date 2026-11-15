# Data platform

Research datasets from free, official APIs, stored in a local Parquet lake and queried with DuckDB.
Code: `backend/app/datahub`. API: `/api/data/*`. Worker: the `data-worker` service.

    providers -> ingestion (async, paced) -> validation and normalisation -> lake (Parquet) + catalog (Postgres)
              -> versioned features -> research, backtesting, Quant AI

It is separate from trading on purpose. The package imports nothing from `brokers`, `execution`, `risk`,
`strategies` or `signals` (a test enforces this), holds no trading credentials and cannot place an order.
Strategies, signal rules, risk parameters and execution logic are unchanged.

## What already existed, and what was added

| Layer | Before | Now |
|---|---|---|
| Providers | Alpaca (daily bars, live stream), Alpha Vantage, Tiingo, EODHD: daily prices of listed instruments | The same, plus the sources below. No provider is duplicated: Alpaca intraday history reuses the Alpaca account, crypto venues reuse CCXT |
| Storage | `bars` table in Postgres (OHLCV per instrument) | Unchanged for trading and backtests. Research data goes to Parquet files; Postgres holds only the catalog and the ingestion log |
| Quality | `validate_bars` (duplicates, impossible bars, gaps) | Reused, plus off-grid timestamps, outliers, freshness, continuity, provider availability, latency |
| Features | `machine_learning/features.py` (daily-style bar features for the ML models) | Unchanged. New versioned sets for intraday, order book, trade flow, cross-venue and point-in-time macro |
| Workers | One Celery worker for everything | `worker` (research, queue `celery`) and `data-worker` (collection, queue `ingest`, runs the schedule) |

## Sources

All free. A source is used only when it is listed in `QAT_DATA_SOURCES` **and** has what it needs;
otherwise it stays off and requests to it are refused. `GET /api/data/providers` shows the live state.

| Source | Needs | Data | History | Timeliness | Limit (enforced) | Licence notes |
|---|---|---|---|---|---|---|
| FRED / ALFRED | Free API key | Macro series with every published vintage | Decades | Day of release | 120 requests/min, paced at ~109 | Attribution notice required; some series are third-party |
| SEC EDGAR | `QAT_SEC_USER_AGENT` (name + e-mail), no key | Filing index, reported XBRL figures | Filings: most recent ~1,000 per company. Figures: 2009 onwards | Seconds to a minute after acceptance, polled | 10 requests/s, paced at ~6 | Public domain |
| Kraken | Nothing | Bars, trades, order-book snapshots | Bars: last 720 per interval only. Trades: full | Real time when polled | ~1 request/s (CCXT) | No redistribution |
| Binance | Nothing | Bars, trades, order-book snapshots | Bars back to listing | Real time when polled | Weight-based, paced by CCXT | No redistribution. Market-data host only |
| CoinGecko | Nothing; optional free Demo key | Aggregated price, market cap, per-venue volume and spread | 1 year | ~1 minute | Monthly budget 9,000 requests | Attribution required; no commercial redistribution |
| Financial Modeling Prep | Free API key | Company reference data (sector, industry, market cap, beta) | From first collection | Daily | Daily budget 240 requests | Personal use |
| Alpaca | The Alpaca keys already configured | Minute bars for US stocks/ETFs (with VWAP and trade count) and crypto | Stocks from 2016 | **Delayed 15 minutes** on the free plan | ~200 requests/min | Own use only |

Things the free tiers do **not** give, stated plainly:

- No real-time consolidated US equity tape. Alpaca's free plan is 15 minutes delayed for history; its live stream is IEX only.
- No historical order books anywhere. Depth exists only from the moment collection is switched on.
- No US equity bid/ask history. Quote data on the free Alpaca plan is too limited to build a spread series; this is not implemented.
- No economic-surprise features: they need consensus forecasts, which no free official source publishes. Not implemented rather than approximated.
- FMP financial statements are not used: they carry no filing time, so they cannot be made point-in-time. EDGAR is used instead.
- Kraken's candle endpoint ignores the requested start and returns its newest 720 candles. The ingestion detects this and records `history_truncated_at`.

## Timestamps and point-in-time rules

Every row keeps its `source` and these UTC timestamps:

| Column | Meaning |
|---|---|
| `ts` (or `date`, `period_end`) | When the event happened: bar open, trade, observation period |
| `received_at` | When the provider's response arrived |
| `ingested_at` | When the row was written to the lake |
| `available_at` | When the information became public (macro, filings, fundamentals). **Backtests must join on this** |

- **Revised macro series** (CPI, PCE, payrolls, claims, GDP, production): every vintage is stored. `available_at` is 18:00 New York on the publication date, which is conservative; `scheduled_release_times=true` on `/api/data/macro` uses the usual 08:30 or 09:15 instead.
- **Daily market series** (yields, fed funds, dollar index): not downloaded with vintages (thousands of them). Known from 18:00 New York on the next business day.
- **Company figures**: known from the filing's EDGAR acceptance time. EDGAR writes those times with a `Z` suffix although they have been observed to be Eastern clock times; they are read as Eastern, the later of the two readings. Where the acceptance time is not in the downloaded index, 18:00 New York on the filing date is used, and the row says so (`availability`).
- **Different venues are never merged.** `binance:BTC/USDT` and `kraken:BTC/USD` live in separate files. `/api/data/compare` puts them side by side on the timestamps both have; nothing is filled.
- A re-fetched row identical to the stored one is left untouched. One that differs replaces it and is counted as a revision in the ingestion log.

## Storage

    <lake>/<dataset>/<source>/.../<bucket>.parquet      zstd, one writer per series at a time, atomic replace

Datasets: `bars`, `trades`, `orderbook`, `macro`, `fundamentals`, `filings`, `crypto_market`, `crypto_venues`,
`company_profile`, `features`. Intraday bars are bucketed by month, daily bars by year, trades and books by day.
In Docker the lake is the `lake` volume, mounted at `/srv/lake` in `api`, `worker` and `data-worker`.

Why these choices:

- **Parquet + DuckDB**: both are libraries inside the existing containers. No new service, no cloud.
- **Postgres** keeps what is relational and small: the catalog (`data_series`) and the log (`data_ingestion_runs`), migration `0007`.
- **Redis** is unchanged (Celery broker). No Redis cache was added: nothing measured needed one.

Research access from Python:

```python
from app.datahub.lake import DataLake
lake = DataLake("/srv/lake")
bars = lake.bars("binance", "BTC/USDT", "1m")              # OHLCV indexed by UTC bar open
with lake.connect() as con:                                # every dataset is a SQL view
    con.sql("SELECT source, symbol, count(*) FROM bars GROUP BY ALL").show()
```

## Feature sets

Versioned (`name@vN`); changing a column's meaning requires a new version. All are causal: tests cut
the future off and check that no earlier value changes. Listed by `GET /api/data/features`.

| Set | Input | Columns |
|---|---|---|
| `bars_intraday@v1` | bars | Returns over 1/5/15 bars, session VWAP and distance to it, relative volume (same time of day, and rolling), realized volatility, range, gap flag |
| `cross_venue@v1` | bars of several venues | Price divergence in basis points and its z-score |
| `cross_asset_correlation@v1` | bars of several series | Rolling return correlation |
| `orderbook@v1` | order-book snapshots | Mid, spread, microprice, imbalance at 1/5/all levels, depth |
| `trade_flow@v1` | trades | Volume, buy/sell volume, trade count, VWAP, volume imbalance per bar |
| `macro@v1` | macro rows | Latest published value and its age, yield-curve spreads, yield changes, year-on-year inflation from the figures published at the time |

`features.fundamentals_asof(facts, asof)` returns the figures a company had reported by a given moment.

These are research inputs. They are not connected to the strategies or the signal engine.

## Using it

1. Add the keys you have to `.env` (see the data platform block in `.env.example`) and restart, or store them encrypted: `PUT /api/market/providers/{fred|fmp|coingecko|sec}/key`.
2. Check each source with one small request: `docker compose exec api python -m app.scripts.data_smoke`.
3. Ingest on demand, for example:

```
POST /api/data/ingest {"provider":"binance","dataset":"bars","keys":["BTC/USDT"],"timeframe":"1m","days":30}
POST /api/data/ingest {"provider":"fred","dataset":"macro"}
POST /api/data/ingest {"provider":"sec","dataset":"fundamentals","keys":["AAPL","MSFT"]}
```

   A bar ingestion without `start` resumes after the newest stored bar.
4. Or switch on scheduled collection: `QAT_DATA_SCHEDULE_ENABLED=true`. Bars every 5 minutes, order books and trades every `QAT_DATA_ORDERBOOK_SECONDS`, CoinGecko venues hourly, macro, filings and reference data daily at 23:30 UTC. Sources that are not ready are skipped.
5. To backtest on a lake series, copy it into the bar store: `POST /api/data/promote`. Same rules as a CSV import (validated, never merged into an existing series), and refused for an instrument that has a deployment or a live subscription.

## Monitoring

- `GET /api/data/catalog`: every stored series with rows, span, age and a `stale` flag (older than three times its expected cadence).
- `GET /api/data/runs`: per ingestion, requests, rate-limit hits, fetch and write time, rows new and revised, and the quality report (duplicates, invalid rows, off-grid, missing bars with examples, outliers).
- `GET /api/data/health`: per provider over the last hours, availability, requests, milliseconds per request, last error.
- `GET /api/data/context`: a compact structured summary (macro state, cross-venue prices, coverage, stale series, caveats) for the AI research layer.

## Measurements

`python -m app.scripts.bench_data --bars 200000` (best of 3; SQLite, this development machine; rerun on
your Postgres with `--database-url` for your own numbers):

| Operation, 200,000 one-minute bars | Before | After |
|---|---|---|
| Load a full series from the bar store (`BarStore.load`) | 2.90 s | 0.68 s (4.3x; identical output, checked) |
| Load the same series from the lake | n/a | 0.10 s |
| One-week range read | 0.039 s (bar store) | 0.043 s (lake): no gain, the bar store's index is already fast here |
| Hourly aggregation | 0.68 s (bar store + pandas) | 0.06 s (lake + DuckDB) |
| Size on disk | 25.2 MB (SQLite) | 10.5 MB (Parquet, 53 bytes per bar) |
| Write | 2.36 s (bar store) | 1.48 s (lake) |
| Fetch 40 series, 50 ms per response | 2.04 s one at a time | 0.52 s at concurrency 4 (default), 0.26 s at 8 |

The last row uses a local stand-in provider; real throughput is capped by each vendor's pacing
(with a limit of 10 requests a second, the same 40 series take 3.95 s whatever the concurrency).

## Verification status

- Automated tests: `backend/tests/datahub` (59 tests) against responses of the documented shapes.
- **No source has been exercised against the live service yet.** The development sandbox has no route to these hosts. Run `data_smoke` once after setup; the Alpaca minute-bar and FMP paths in particular are written from documentation.
- Not verified on Postgres or in a running Docker stack: the migration is tested on SQLite (upgrade, model match, downgrade), and `docker compose config` validates.

Attribution when showing data: "This product uses the FRED® API but is not endorsed or certified by the
Federal Reserve Bank of St. Louis." and "Data provided by CoinGecko."
