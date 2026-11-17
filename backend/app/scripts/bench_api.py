"""API benchmark on a realistically filled database: latency (mean, p50, p95, p99), database queries per request and
response size for every page endpoint, plus the time of one opportunity scan.

    cd backend && python -m app.scripts.bench_api [--repeat 30] [--json out.json]

Nothing leaves the machine: SQLite in a temporary folder, a paper account, no broker, no network.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
import uuid
from datetime import timedelta

ENDPOINTS = ["/api/overview", "/api/quant/dashboard", "/api/quant/autopilot", "/api/quant/validations", "/api/quant/modules", "/api/settings",
             "/api/brokers/status", "/api/strategies", "/api/deployments", "/api/risk", "/api/markets", "/api/instruments", "/api/instruments/stats",
             "/api/notifications?limit=1", "/api/portfolio/equity", "/api/fx", "/api/trades", "/api/orders", "/api/signals", "/api/backtests",
             "/api/proposals", "/api/scanner/watchlists", "/api/global/coverage", "/api/ai/reports", "/api/brokers", "/health"]


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return values[k]


def build(tmp: str, n_instruments: int, strategies: list[str], history: int = 1):
    os.environ.setdefault("QAT_ENABLED_PROVIDERS", "all")
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.database.base import Database
    from app.database.models import ResearchItem
    from app.main import create_app
    from app.market_data.quality import DataKind
    from app.market_data.sources import Dataset, synthetic_bars
    from app.quant_ai import opportunities as opp
    from app.security.roles import Role
    from app.security.users import create_user
    from app.security.vault import CredentialVault

    db = Database(f"sqlite:///{tmp}/bench.db")
    db.create_all()
    with db.session() as s:
        create_user(s, "admin@example.com", "correct-horse-battery", Role.ADMIN)
    settings = Settings(_env_file=None, database_url="sqlite://", jwt_secret="t" * 48, credential_key=CredentialVault.generate_key(),
                        data_lake_dir=f"{tmp}/lake", minute_refresh_offset_seconds=-1, autopilot_tick_seconds=0, api_rate_limit_per_minute=100_000)
    client = TestClient(create_app(settings, db))
    client.__enter__()
    rt = client.app.state.container.runtime
    rt.create_paper_account("paper-main", "Main", "admin")
    rt.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", rt.clock() - timedelta(hours=1))
    syms = [f"T{i:03d}" for i in range(n_instruments)]
    rt.add_instruments(syms, "US", "stock", "admin")
    ids = [i.id for i in rt.registry.all() if i.symbol in syms]
    bars = synthetic_bars("1d", 1500, seed=3, start=(rt.clock() - timedelta(days=1501)).strftime("%Y-%m-%d")).bars
    with db.session() as s:
        for iid in ids:
            rt.store.save(s, iid, "1d", Dataset(bars, DataKind.HISTORICAL, "bench"))
    with db.session() as s:
        v = opp.validate(s, rt, "ema_crossover", ids[0], "1d", "admin")
    payload = {k: v[k] for k in v if k not in ("id", "status", "tested_at")}
    with db.session() as s:
        for k in range(history - 1, 0, -1):  # older tests of the same setups: the table keeps every retest
            for iid in ids:
                for st in strategies:
                    s.add(ResearchItem(id=uuid.uuid4().hex, kind="validation", status="rejected", title=opp._key(st, iid, "1d"),
                                       created_at=rt.clock() - timedelta(days=30 * k), payload={**payload, "strategy": st, "instrument_id": iid}))
    with db.session() as s:
        for iid in ids:
            for st in strategies:
                s.add(ResearchItem(id=uuid.uuid4().hex, kind="validation", status="candidate" if hash((iid, st)) % 7 == 0 else "rejected",
                                   title=opp._key(st, iid, "1d"), created_at=rt.clock(), payload={**payload, "strategy": st, "instrument_id": iid}))
    r = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "correct-horse-battery"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}", "Accept-Encoding": "gzip"}
    return client, rt, db, headers, ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=30)
    ap.add_argument("--instruments", type=int, default=200)
    ap.add_argument("--history", type=int, default=1, help="validations kept per setup (each retest adds one)")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    from sqlalchemy import event

    strategies = ["ema_crossover", "macd", "donchian_breakout", "momentum_breakout", "rsi_reversion", "volatility_expansion"]
    with tempfile.TemporaryDirectory() as tmp:
        t0 = time.perf_counter()
        client, rt, db, headers, ids = build(tmp, args.instruments, strategies, args.history)
        setup = time.perf_counter() - t0
        queries = [0]
        event.listen(db.engine, "before_cursor_execute", lambda *a, **k: queries.__setitem__(0, queries[0] + 1))
        rows, errors = [], 0
        for ep in ENDPOINTS:
            times, size, q, status = [], 0, 0, 0
            for k in range(args.repeat):
                queries[0] = 0
                t = time.perf_counter()
                r = client.get(ep, headers=headers)
                times.append((time.perf_counter() - t) * 1000)
                status, q = r.status_code, queries[0]
                size = r.num_bytes_downloaded
                errors += r.status_code >= 500
            rows.append({"endpoint": ep, "status": status, "mean_ms": round(statistics.mean(times), 1), "p50_ms": round(pct(times, .5), 1),
                         "p95_ms": round(pct(times, .95), 1), "p99_ms": round(pct(times, .99), 1), "first_ms": round(times[0], 1),
                         "db_queries": q, "bytes_on_wire": size})
        from app.quant_ai import opportunities as opp

        cold = []
        for _ in range(5):  # the validation list read from the database (as after every new validation), not from its cache
            opp._CACHE.clear()
            t = time.perf_counter()
            with db.session() as s:
                opp.validations(s, rt.clock())
            cold.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()

        with db.session() as s:
            scan = opp.scan(s, rt, "paper-main", "bench", include_candidates=True)
        scan_ms = (time.perf_counter() - t) * 1000
        client.__exit__(None, None, None)
    all_ms = [r["mean_ms"] for r in rows]
    out = {"instruments": args.instruments, "validations": args.instruments * len(strategies), "repeat": args.repeat, "setup_seconds": round(setup, 1),
           "endpoints": rows, "scan_ms": round(scan_ms, 1), "scan_setups": scan["funnel"].get("setups"), "server_errors": errors,
           "mean_of_means_ms": round(statistics.mean(all_ms), 1), "validations_uncached_ms": round(statistics.median(cold), 1), "history": args.history}
    print(f"{'endpoint':32} {'st':>3} {'mean':>7} {'p50':>7} {'p95':>7} {'p99':>7} {'first':>7} {'q':>4} {'bytes':>9}")
    for r in rows:
        print(f"{r['endpoint']:32} {r['status']:>3} {r['mean_ms']:>7} {r['p50_ms']:>7} {r['p95_ms']:>7} {r['p99_ms']:>7} {r['first_ms']:>7} {r['db_queries']:>4} {r['bytes_on_wire']:>9}")
    print(f"scan of {out['scan_setups']} setups: {out['scan_ms']} ms; validation list from the database: {out['validations_uncached_ms']} ms; server errors: {errors}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
