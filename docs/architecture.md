# Architecture

```
Next.js dashboard ── REST (cookie session) ── FastAPI: auth, roles, rate limit, validation
                                                  │
                          ┌──────────── TradingRuntime (one process, one lock) ────────────┐
 market data ─► validation ─► strategies/signals ─► sizing ─► RISK ENGINE ─► approval? ─► BrokerManager
 (CCXT, IBKR,                                                     │                          ├─ SimulatedBroker (paper, backtest)
  CSV, replay)                                           breakers / kill switch              ├─ CcxtAdapter  (testnet, or live with authorization)
                                                                                             └─ IbkrAdapter  (paper gateway, or live with authorization)
            fills ─► PortfolioEngine (slices per broker/instrument/strategy, reconciliation, FX)
            journal ─► Postgres (orders, fills, signals, risk events, audit) ─► Notifier (in-app, email, Telegram)
 Research (same engines, isolated instances): Backtester · Walk-forward · ML training · Analyst
```

## Packages (`backend/app`)

| Package | Responsibility |
|---|---|
| `domain` | Instrument, Order, Fill, Signal, Position, commission models |
| `instruments` | Registry of normalized instruments and broker symbol mapping |
| `market_data` | Timeframes, causal resampling and alignment, calendars, validation/quality reports, timestamped FX, CSV/synthetic sources, bar store, continuous futures |
| `strategies`, `signals` | Indicator library, strategy plug-ins and registry, signal engine |
| `risk` | Position sizing, pre-trade limits, circuit breakers, kill switch, emergency policy |
| `portfolio` | Per-strategy position slices, closed trades, valuation in base currency, exposure breakdowns, correlation, reconciliation |
| `execution` | `TradingEngine` (the shared pipeline) and `TradingRuntime` (persistence, feeds, operator actions) |
| `brokers` | Adapter contract, capabilities, error taxonomy, CCXT and IBKR adapters |
| `paper_trading` | `SimulatedBroker`: the execution venue for paper trading and backtests |
| `live_trading` | Authorization gate for real-money connections |
| `backtesting` | Event-driven backtester, metrics, walk-forward, comparison, services |
| `machine_learning` | Features, triple-barrier labels, purged validation, model zoo, training/calibration/drift |
| `quant_ai` | Regime detection, analyst (explanations, weaknesses, anomalies, proposals), services |
| `notifications`, `monitoring` | Notifier and channels; audit trail, broker and strategy monitors |
| `security`, `database`, `api`, `core` | Auth, vault, rate limits; schema and settings; routes; configuration and logging |

## Design rules that the tests enforce

1. **No look-ahead.** A strategy value at bar *t* uses bars ≤ *t*; orders from it fill at the next bar's open. Every strategy, the features and the regime detector are tested by deleting or altering future bars and checking nothing in the past changes.
2. **One pipeline.** Backtest, paper and live differ only in the registered broker adapter.
3. **Risk is central and independent.** Every order, including protective stops, passes `RiskEngine.evaluate`. The risk package imports nothing from strategies, ML or AI.
4. **Idempotent orders.** Client order ids are derived from deployment and bar; duplicates are refused in memory, by a unique database key and by the broker. After a timeout the engine asks the broker whether the order arrived before retrying.
5. **Provenance.** Data is labelled realtime / delayed / historical / simulated; simulated and market data are never mixed for an instrument; simulated orders are flagged.
6. **AI proposes, humans dispose.** The analyst can only return strategy parameters. They need schema validation, an out-of-sample gate and an administrator; applying one never activates a strategy or touches risk settings.
7. **Live trading is a deployment decision.** A real-money adapter needs an authorization object that only deployment environment settings can produce.

## Accounting conventions

- Money in the engines is IEEE double; database columns are NUMERIC. Quantities are rounded down to the instrument lot.
- Accounts are margin-style: equity = cash (capital + realised P&L − costs) + unrealised P&L. Initial margin is checked at fill.
- P&L in a foreign quote currency is converted to the account currency at the rate in force at the fill, with the configured conversion cost.
- The simulated venue keeps separate books per strategy on the same instrument and reports the net position, as a netting broker would.
