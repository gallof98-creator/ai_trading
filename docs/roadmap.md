# Roadmap and phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and plan | Approved |
| 2 | Repository, Docker Compose, CI, pinned dependencies | **Done** |
| 3 | Domain models, database, migrations, auth, encrypted credentials | **Done** |
| 4 | Market data, instruments, calendars, FX | Not started (draft modules exist, untested) |
| 5 | Broker adapters and capability detection | Not started |
| 6 | Strategies, registry, signals | Not started |
| 7 | Centralised risk engine | Not started |
| 8 | Backtesting | Not started |
| 9 | Paper trading | Not started |
| 10 | Portfolio engine | Not started |
| 11 | Regime detection, ML, AI analyst | Not started |
| 12 | Dashboard modules A-I | Shell, Overview (config only) and Settings only |
| 13 | Monitoring and notifications | Audit trail only |
| 14 | Full testing, security review, legal docs | Not started |

## Decisions taken at approval

- AI analyst: deterministic analytics core; optional LLM narrative layer.
- Single tenant with admin / trader / viewer roles.
- Data: CCXT for crypto, IBKR for stocks/ETFs/forex/futures, CSV/Parquet import.
- FX: timestamped rates (ECB reference + broker) with a configurable conversion cost.
- Notifications: in-app first; email and Telegram behind one interface.
- Emergency policies: block new risk (default), cancel open orders, flatten positions (explicit admin opt-in).
- Futures: back-adjusted continuous series, roll N days before expiry.
- Compliance documentation assumes the EU (MiFID II, MiCA, GDPR).

## Known limitations after Phase 3

- Rate limiting is in-memory and covers login only; it must move to Redis before running more than one API process.
- `app/domain`, `app/instruments`, `app/market_data` contain draft code written before Phase 4; it has no tests yet and is not wired into the API.
- The health-check test simulates a database failure crudely; a real connection-loss test comes with Phase 14.
- No password reset or multi-factor authentication.
