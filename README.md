# QUANT AI TRADER

Multi-asset algorithmic trading and research platform: crypto, stocks, ETFs, forex and futures
through one engine, with backtesting, paper trading, a central risk engine and a Quant AI research module.

> **This is a technical research and trading tool, not a source of guaranteed returns.**
> Backtests and paper results are simulations. Live trading is disabled by default and has never been
> exercised against a real broker; read [docs/legal-and-compliance.md](docs/legal-and-compliance.md) first.

## What is in the box

| Area | Summary |
|---|---|
| Strategies | 17 plug-in strategies (trend, momentum, mean reversion, volatility, breakout, multi-timeframe), versioned registry, validated parameters |
| Engine | One pipeline for backtest, paper and live: signal → sizing → risk engine → (approval) → broker adapter → portfolio |
| Risk | Per-trade risk sizing, exposure limits (asset, strategy, broker, leverage, margin, correlation), daily-loss and drawdown breakers, kill switch, configurable emergency policy |
| Backtesting | Event-driven, next-bar fills, spread, slippage, commissions, financing, partial fills, sessions, walk-forward, comparison, reproducible config hash |
| Paper trading | Simulated accounts in the base currency that survive restarts; reset with typed confirmation |
| Brokers | Adapter framework with capability detection; CCXT (Binance, Bybit, …) and Interactive Brokers adapters; simulated venue |
| Quant AI | Market-regime detection, 5 model types with purged out-of-sample validation and calibration, drift checks, analyst reports, gated parameter proposals |
| Dashboard | Overview, Markets, Strategies, Quant AI, Backtesting, Portfolio, Execution, Risk Center, Settings |
| Security | Argon2 passwords, httpOnly sessions, roles, encrypted broker credentials, audit trail, rate limiting |

Honest status of every feature, including what is **not** verified: [docs/roadmap.md](docs/roadmap.md).

## Quick start (Docker Compose)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.
Open a terminal (Windows: PowerShell; macOS: Terminal) and run these four commands one at a time:

```bash
git clone https://github.com/gallof98-creator/ai_trading
```

```bash
cd ai_trading
```

```bash
docker run --rm -v "${PWD}:/w" -w /w python:3.12-slim python infrastructure/scripts/make_env.py
```

That creates the `.env` configuration file with fresh secrets and prints your sign-in email and
password (write the password down; pass your own email as a last argument if you like). Then:

```bash
docker compose up -d --build
```

The first build takes several minutes. To stop everything later: `docker compose down`.
(To write `.env` by hand instead, copy `.env.example` and follow the comments in it.)

- Dashboard: http://localhost:3000 — sign in with `QAT_ADMIN_EMAIL` / `QAT_ADMIN_PASSWORD`
- API and interactive API reference: http://localhost:8000/docs
- Health: http://localhost:8000/health

First steps in the dashboard:

1. **Settings** → set the initial paper capital (there is no default) and review the risk limits.
2. **Execution** → create a paper account.
3. **Markets** → import a CSV of real bars, or generate clearly labelled simulated data to try things out.
4. **Strategies** → pick a strategy, create a deployment, then activate it.
5. **Execution** → with no market-data connection, use *Simulated market replay* to watch the pipeline run.
6. **Backtesting**, **Quant AI**, **Portfolio**, **Risk Center** work on the same data.

After the first sign-in remove `QAT_ADMIN_EMAIL` and `QAT_ADMIN_PASSWORD` from `.env`.

## Development setup

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -r requirements-ml.txt
alembic upgrade head
QAT_ADMIN_EMAIL=you@example.com QAT_ADMIN_PASSWORD='a-long-password' uvicorn app.main:app_factory --factory --reload
```

```bash
cd backend && pytest          # 204 tests, 4-5 minutes; never contacts a real broker
```

```bash
cd frontend && npm ci && npm run dev
```

`requirements-ml.txt` (PyTorch) is only needed for the neural-network model; without it that one
model type reports that PyTorch is missing and everything else works.

## Configuration

| Where | What |
|---|---|
| `.env` (variables prefixed `QAT_`) | Secrets, database, CORS origins, market-data exchange, FX pegs, notification channels, live-trading gate. Full list: `.env.example` and `backend/app/core/config.py`. |
| Dashboard → Settings (stored in the database, audited) | Base currency, initial paper capital, trading mode (manual / semi-automatic / automatic), every risk limit, emergency policy, notification preferences, FX rates, broker accounts and credentials, users. |

More detail: [docs/architecture.md](docs/architecture.md) · [docs/api.md](docs/api.md) ·
[docs/legal-and-compliance.md](docs/legal-and-compliance.md)

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `set POSTGRES_PASSWORD in .env` | `.env` is missing or incomplete; copy `.env.example`. |
| API exits with `Missing required production secrets` | Set `QAT_JWT_SECRET` (32+ characters) and `QAT_CREDENTIAL_KEY`. |
| Cannot sign in on first run | The bootstrap admin is created only while no user exists, and its password needs 12+ characters. Look for `bootstrap_admin_failed` in `docker compose logs api`. |
| Health shows `"status": "degraded"` | Database unreachable or migrations missing (`runtime_started: false`). Run `alembic upgrade head` / check Postgres. |
| Dashboard: "Cannot reach the API" | API not healthy, or the dashboard is served from an origin missing in `QAT_CORS_ORIGINS`. |
| "Portfolio cannot be valued" / "No usable FX rate" | Add a rate under Settings → Exchange rates, or load the ECB reference rates. |
| "No stored bars for …" | Import a CSV or generate simulated data under Markets. |
| "simulated and market data are never mixed" | An instrument holds one kind of data; reset the paper account / replace the stored series deliberately. |
| Orders stay "pending approval" | Trading mode is semi-automatic, or the account is a real-money one (approval is on by default). Approve under Execution. |
| All new orders rejected | Kill switch or a circuit breaker is active: see Risk Center. |
| `429 Too Many Requests` from the API | Per-client rate limit (`QAT_API_RATE_LIMIT_PER_MINUTE`, default 600). |
| `CERTIFICATE_VERIFY_FAILED` during `docker compose build` | A TLS-inspecting proxy; add its CA certificate to the images. |
| Port 3000 or 8000 already in use | Stop the other process or change the published ports in `docker-compose.yml`. |
