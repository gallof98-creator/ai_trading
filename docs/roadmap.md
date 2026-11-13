# Phase status and limitations

Status words mean exactly this: **Done** = implemented and covered by automated tests;
**Done (mock-tested)** = implemented, tested only against mocks, never against the real external system;
**Partial** = works but with the stated gaps; **Not built** = absent.

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and plan | Approved |
| 2 | Repository, Docker Compose, CI definition, pinned dependencies | Done (CI workflow never executed: no remote) |
| 3 | Domain models, database, migrations, auth, roles, encrypted credentials | Done |
| 4 | Market data validation, instruments, calendars, timestamped FX, CSV import, bar store, continuous futures | Done; live feed is Partial (see below) |
| 5 | Broker adapter framework and capability detection | Done; CCXT and IBKR adapters Done (mock-tested) |
| 6 | 17 strategies, registry, signal engine | Done |
| 7 | Central risk engine | Done |
| 8 | Backtesting, metrics, walk-forward, comparison | Done; futures rollover Partial |
| 9 | Paper trading | Done |
| 10 | Portfolio engine | Done |
| 11 | Regime detection, ML framework, AI analyst | Done as a research module; Partial against the full wish-list (below) |
| 12 | Dashboard modules A–I | Done (verified by scripted browser runs; no automated frontend tests) |
| 13 | Monitoring and notifications | Done; email and Telegram channels Done (mock-tested) |
| 14 | Testing, security review, documentation, Docker verification | Done with the caveats below |

## Verification performed

- Backend test suite:  tests, all passing (strategy maths, look-ahead, sizing, every risk limit, backtest hand calculations and reproducibility, FX, adapters against mocks, idempotency, duplicate prevention, partial fills, connection loss, database outage, paper/live isolation, model validation, leakage, HTTP flows, auth and RBAC).
- Docker Compose stack (Postgres, Redis, API, worker, frontend): started healthy, migrations applied on Postgres, full browser flow passed (login, settings, paper account, data, deployment, replay, overview, portfolio, kill switch, backtest, comparison, model training, report), state identical after an API container restart, no errors or secrets in the logs.

## Not verified or not built — read before relying on anything

**Live trading**
- No adapter has ever talked to a real exchange, testnet or IBKR gateway. `IbkrAdapter` in particular is written against the `ib_async` interface from its signatures and may need event-loop handling when run inside the API process.
- On restart with a real broker, positions are reconciled against the broker (a mismatch trips a breaker), but open orders at the broker are not yet compared with the platform's records.
- Stocks and ETFs can be added from the dashboard and are tradable on paper accounts, but there is no UI for broker symbol mappings: CCXT symbols map automatically, IBKR symbols currently need a database row, which makes IBKR impractical from the dashboard today. Forex pairs, futures and crypto pairs beyond the starter set cannot be added from the dashboard yet.

**Market data**
- Stock prices can be downloaded from three vendors with an official API and the user's own key. What was verified against the real services:
  - **EODHD** (paid for real coverage; US and Europe): the price endpoint was run live with the vendor's public demo key, including continuity across a stock split. The symbol-list endpoint needs a real key and was only tested against responses of the documented shape.
  - **Alpha Vantage** (free, about 25 requests a day; US only here): the US symbol list (about 8,150 stocks and 5,900 ETFs imported) and a daily series were run live with the public demo key, followed by a backtest on the downloaded data. How much history a personal free key returns is not verified; if only 100 days are allowed the series is labelled `last100`. Prices are unadjusted.
  - **Tiingo** (free; US only): only the rejection of a wrong key was observed live. Price parsing is written from the documented format and has not seen a real response; press "Test connection" first.
- No free official API covers European stocks at scale. TradingView, Yahoo Finance and Stooq are not connected on purpose: they offer no official data API and reject or forbid automated access, so a connector would have to pose as a browser.
- Free plans are slow by design: Tiingo is paced at one request every 75 seconds (about 500 stocks take over ten hours and a month's symbol allowance), Alpha Vantage at about 25 series a day.
- EODHD prices are stored adjusted for splits and dividends (open/high/low/close scaled by adjusted_close / close); volume is as reported. A refresh replaces the whole history of an instrument.
- Listings contain only companies listed today. Delisted and bankrupt companies are missing, so results across "all stocks" are biased upwards (survivorship bias).
- A universe scan runs at most 300 instruments per request; storing 10+ years for several thousand stocks means tens of millions of rows, which Postgres handles but SQLite does not handle well.
- The bulk-file parser was tested with files generated in the two documented layouts, not with a real vendor download. London-listed shares (quoted in pence) are not supported.
- Imported stock prices are used exactly as supplied: if the file is not adjusted for splits and dividends, backtests will see artificial jumps.
- The live feed polls completed bars from a CCXT exchange or a connected broker. No websockets, ticks, order book, trade tape or per-period funding rates are ingested (funding/borrow cost is a constant annual rate in the execution model). Bid/ask is available from adapters but not shown.
- The CCXT public-data connection and the ECB rate download were not exercised against the real services from the build environment (the ECB parser is tested).
- Exchange holidays are not bundled; equity calendars know regular sessions and weekends only.
- No economic calendar module.
- Survivorship-bias-free testing needs point-in-time universe data from a vendor; the platform has no universe management.

- The global catalog and bulk price work is tracked separately in [global-markets.md](global-markets.md).

**Backtesting**
- Futures: the continuous-series builder and roll-cost charge exist and are tested, but roll schedules cannot yet be supplied through the API or dashboard.
- Bars are the smallest unit of time: when a stop and a target fall inside one bar the stop is assumed first; intrabar paths are not modelled. Cross-instrument event ordering is exact at bar boundaries only.
- Static FX assumptions are allowed and flagged; a full FX history is used when one is loaded.

**Quant AI / ML**
- Models are research artefacts. They do not filter or size orders, so signals never carry a confidence value.
- The analyst is deterministic statistics; the optional LLM narrative layer was not built.
- MLflow is not integrated (model records live in the database, artefacts on disk). Polars is not used: nothing measured needed it.
- No dedicated time-series model family (ARIMA/GARCH); the five classifier types are implemented.

**Platform**
- Research jobs (backtests, walk-forward, training, parameter search) run inside the API request, not on the Celery worker, which currently only proves the queue works. Long runs block that request; inputs are capped (30,000 bars, 60 trials, 200 grid combinations).
- The trading runtime lives in the API process and must run as a single instance; the rate limiter is in-memory.
- The dashboard polls every 5–30 seconds; there is no push channel.
- No password reset and no multi-factor authentication.
- Engine arithmetic uses floating point (database columns are NUMERIC).
- The Dockerfiles were built in the build environment only with an added proxy certificate, and the final verification layered the new code on earlier images because Docker Hub rate-limited base-image pulls. Build them once on your own network.

## Decisions taken at approval

AI analyst deterministic with optional LLM layer (layer not built) · single tenant with three roles ·
CCXT for crypto, IBKR for other markets, CSV import · timestamped FX (ECB + manual) with conversion cost ·
in-app notifications plus email and Telegram · three emergency policies, default never liquidates ·
back-adjusted continuous futures · EU assumed for compliance notes.
