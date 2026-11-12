# QUANT AI TRADER

Multi-asset algorithmic trading and research platform (crypto, stocks, ETFs, forex, futures).

> A technical research and trading tool. Backtests and paper results are simulations and are
> **not** evidence of future returns. Live trading is disabled by default.

## Status

Built phase by phase; see [docs/roadmap.md](docs/roadmap.md) for what is done and what is not.
Currently complete: **Phase 2** (repository, Docker stack, CI) and **Phase 3** (domain models,
database + migrations, authentication, roles, encrypted broker credentials, user settings).
There is no trading, strategy, backtest or AI functionality yet.

## Run with Docker Compose

```bash
cp .env.example .env      # then fill in the four empty secrets (commands are in the file)
docker compose up -d --build
```

- Dashboard: http://localhost:3000 (sign in with `QAT_ADMIN_EMAIL` / `QAT_ADMIN_PASSWORD`)
- API and interactive API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

After the first sign-in, remove `QAT_ADMIN_EMAIL` and `QAT_ADMIN_PASSWORD` from `.env`.
Then open **Settings** and set the initial paper capital; it has no default on purpose.

## Run for development

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head                       # SQLite file by default
QAT_ADMIN_EMAIL=you@example.com QAT_ADMIN_PASSWORD='a-long-password' \
  uvicorn app.main:app_factory --factory --reload
pytest                                     # in another shell

cd ../frontend && npm ci && npm run dev
```

Add users with `python -m app.scripts.create_user EMAIL admin|trader|viewer`.

## Configuration

Deployment settings are environment variables prefixed `QAT_` (see `.env.example` and
`backend/app/core/config.py`). Trading settings (base currency, paper capital, trading mode,
risk limits, emergency policy) are edited by an admin in the dashboard or via `PUT /api/settings`.

## Security model

- Passwords: Argon2id. Sessions: 60-minute signed token in an httpOnly, SameSite=Lax cookie; sign-out revokes all of a user's sessions.
- Roles: `admin` > `trader` > `viewer`, read from the database on every request.
- Broker credentials: Fernet-encrypted at rest, never returned by the API, redacted from logs. Keys declared with withdrawal or transfer permission are refused.
- Live broker accounts cannot be created unless the deployment sets both `QAT_LIVE_TRADING_ENABLED=true` and the confirmation phrase.
- Every login, settings change and credential change is written to an append-only audit table (`GET /api/audit`).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `set POSTGRES_PASSWORD in .env` | `.env` is missing or incomplete; copy `.env.example`. |
| API exits with `Missing required production secrets` | Set `QAT_JWT_SECRET` (32+ chars) and `QAT_CREDENTIAL_KEY`. |
| Cannot sign in on first run | The bootstrap admin is only created while the users table is empty, and the password must be 12+ characters. Check `docker compose logs api` for `bootstrap_admin_failed`. |
| Dashboard says "Cannot reach the API" | API not healthy, or the dashboard is opened from an origin not listed in `QAT_CORS_ORIGINS`. |
| `CERTIFICATE_VERIFY_FAILED` during `docker compose build` | A TLS-inspecting proxy; add your proxy's CA certificate to the images. |
| Port 3000 or 8000 already in use | Stop the other process or change the published ports in `docker-compose.yml`. |
