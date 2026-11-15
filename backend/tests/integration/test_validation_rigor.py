"""Promotion of strategies: parallel validation, multiple-testing control, older verdicts, a universe that grows in steps."""
import uuid
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.database.models import ResearchItem
from app.database.settings_store import AutopilotSettings, initial_settings, save_settings
from app.execution.runtime import TradingRuntime
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset
from app.quant_ai import opportunities as opp
from app.quant_ai.parallel import ValidationPool, worker_count
from tests.integration.test_autopilot import configure
from tests.integration.test_autopilot import rt as autopilot_rt  # noqa: F401  (fixture)
from tests.integration.test_opportunities import CYC, cyclical


def random_walk(seed, n=1300):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))  # no drift, no pattern: nothing to find
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC", name="ts")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.003, "low": np.minimum(o, c) * 0.997, "close": c, "volume": 1e6}, index=idx)


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    now = [pd.Timestamp("2025-08-01", tz="UTC").to_pydatetime()]
    runtime = TradingRuntime(db, Settings(_env_file=None), clock=lambda: now[0])
    runtime.start()
    runtime.create_paper_account("paper-main", "Main", "admin")
    runtime.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", now[0] - timedelta(hours=2))
    names = [f"N{i:02d}/USD" for i in range(12)]
    runtime.add_instruments([*names, "CYC/USD"], "", "crypto", "admin")
    with db.session() as s:
        for i, name in enumerate(names):
            runtime.store.save(s, f"CRYPTO:{name}", "1d", Dataset(random_walk(100 + i), DataKind.HISTORICAL, "test"))
        runtime.store.save(s, CYC, "1d", Dataset(cyclical(), DataKind.HISTORICAL, "test"))
    runtime.now, runtime.noise = now, [f"CRYPTO:{n}" for n in names]
    return runtime


def specs_for(rt, ids, strategy="ema_crossover"):
    with rt.db.session() as s:
        return [opp.prepare(s, rt, strategy, i, "1d", allow_short=True) for i in ids]


def test_strategies_on_pure_noise_are_not_promoted_while_a_real_pattern_is(rt):
    """Twelve driftless random walks and one series with a real, repeating pattern, all tested with the same rule."""
    now = rt.clock()
    ids = [*rt.noise, CYC]
    results = ValidationPool().run(specs_for(rt, ids), workers=1)
    old_rules = 0
    with rt.db.session() as s:
        for spec, result in zip(specs_for(rt, ids), results):
            tier, payload = opp.judge(spec, result, "test", now)
            opp.record(s, tier, payload, now)
            old_rules += all(c["passed"] for c in payload["checks"][:7]) and spec.instrument_id != CYC  # the checks that existed before
    with rt.db.session() as s:
        verdicts = {v["instrument_id"]: v for v in opp.validations(s, now)}
        family = opp.multiple_testing(s, now)
    assert verdicts[CYC]["status"] == "validated" and verdicts[CYC]["statistics"]["p_value"] < 0.01
    assert all(c["passed"] for c in verdicts[CYC]["checks"]) and len(verdicts[CYC]["checks"]) == 11
    noise = [verdicts[i] for i in rt.noise]
    assert sum(v["status"] == "validated" for v in noise) == 0  # nothing found where there is nothing to find
    assert sum(v["status"] == "validated" for v in noise) <= old_rules  # never more permissive than the previous criteria
    assert family["setups_tested"] == 13 and family["validated"] == 1 and family["best_annual_sharpe_expected_by_chance"] > 0
    names = [c["name"] for c in verdicts[CYC]["checks"]]
    assert any("statistically significant" in n for n in names) and any("trading costs multiplied by 2" in n for n in names)
    assert any("parameter settings" in n for n in names) and any("without its best trade" in n for n in names)


def stored_verdict(session, key, status, p, now, version=opp.CRITERIA["version"], days=0):
    strategy, iid, tf = key
    payload = {"strategy": strategy, "instrument_id": iid, "timeframe": tf, "statistics": {"p_value": p, "sharpe_per_period": 0.05},
               "valid_until": (now + timedelta(days=30 - days)).isoformat(), "caveat": "original"}
    if version is not None:
        payload["criteria_version"] = version
    session.add(ResearchItem(id=uuid.uuid4().hex, kind="validation", status=status, title=opp._key(strategy, iid, tf), created_at=now - timedelta(days=days), payload=payload))


def test_a_result_must_survive_the_number_of_setups_tested(rt):
    now = rt.clock()
    rng = np.random.default_rng(5)
    with rt.db.session() as s:
        for i, p in enumerate(rng.uniform(0.06, 1, 60)):   # sixty setups that failed on their own
            stored_verdict(s, ("ema_crossover", f"STOCK:R{i}", "1d"), "rejected", float(p), now)
        stored_verdict(s, ("ema_crossover", "STOCK:WEAK", "1d"), "validated", 0.04, now)     # passed alone, at 4%
        stored_verdict(s, ("ema_crossover", "STOCK:STRONG", "1d"), "validated", 0.0004, now)  # far beyond chance
        stored_verdict(s, ("ema_crossover", "STOCK:OLD", "1d"), "validated", None, now, version=None)  # judged by the previous criteria
        stored_verdict(s, ("ema_crossover", "STOCK:OLDNO", "1d"), "rejected", None, now, version=None)
    with rt.db.session() as s:
        v = {x["instrument_id"]: x for x in opp.validations(s, now)}
    assert v["STOCK:STRONG"]["status"] == "validated" and v["STOCK:STRONG"]["caveat"] == "original"
    # 0.04 is not enough among 62 tests (the bar there is 0.10 x rank / 62): reported as a candidate, with the reason.
    assert v["STOCK:WEAK"]["status"] == "candidate" and "62 setups tested" in v["STOCK:WEAK"]["caveat"]
    # A verdict from before these criteria is not trusted: it is due for a new test and not tradable meanwhile.
    assert v["STOCK:OLD"]["status"] == "expired" and v["STOCK:OLD"]["due_for_retest"] is True
    assert v["STOCK:OLDNO"]["status"] == "rejected" and v["STOCK:OLDNO"]["due_for_retest"] is False  # stricter rules cannot rescue a rejection
    with rt.db.session() as s:
        alone = uuid.uuid4().hex
        for row in s.query(ResearchItem).filter(ResearchItem.status == "rejected"):
            s.delete(row)
    with rt.db.session() as s:
        assert {x["instrument_id"]: x for x in opp.validations(s, now)}["STOCK:WEAK"]["status"] == "validated"  # the same 4% among two tests is fine
    assert alone


def test_parallel_validation_gives_exactly_the_sequential_results(rt):
    ids = [*rt.noise[:3], CYC]
    specs = specs_for(rt, ids)
    pool = ValidationPool()
    try:
        parallel = pool.run(specs, workers=2)
        assert pool.last["mode"] == "parallel" and pool.last["workers"] == 2 and pool.last["finished"] == 4
        sequential = pool.run(specs, workers=1)
        assert pool.last["mode"] == "sequential"
        assert parallel == sequential  # same folds, same numbers, whichever process computed them
        # One validation that cannot run does not stop the others, in either mode.
        short = specs_for(rt, [CYC])[0]
        short.data = Dataset(short.data.bars.iloc[:50], short.data.kind, short.data.source)
        mixed = pool.run([specs[0], short, specs[3]], workers=2)
        assert isinstance(mixed[1], ValueError) and mixed[0] == sequential[0] and mixed[2] == sequential[3]
    finally:
        pool.close()
    assert pool.run(specs, workers=1, timeout=0) == [None] * 4  # out of time: left for the next cycle, not half-done
    assert worker_count(3) == 3 and 1 <= worker_count(0) <= 8


def test_a_broken_process_pool_falls_back_to_one_process(rt, monkeypatch):
    specs = specs_for(rt, [rt.noise[0], CYC])
    pool = ValidationPool()

    def no_processes(workers):
        raise OSError("cannot start a process")

    monkeypatch.setattr(pool, "_pool", no_processes)
    out = pool.run(specs, workers=4)
    assert pool.last["mode"] == "sequential (process pool unavailable)" and all(isinstance(r, dict) for r in out)


def test_the_autopilot_validates_in_parallel_with_the_same_outcome(autopilot_rt):  # noqa: F811
    rt = autopilot_rt
    configure(rt, validation_workers=2)
    try:
        out = rt.autopilot.run_cycle()
    finally:
        rt.autopilot.pool.close()
    counts = {k: out["validation"][k] for k in ("run", "passed", "candidates", "could_not_run", "remaining")}
    assert counts == {"run": 4, "passed": 2, "candidates": 0, "could_not_run": 0, "remaining": 0}  # as in the one-process test
    assert out["validation"]["speed"]["mode"] == "parallel" and out["validation"]["speed"]["workers"] == 2
    assert rt.autopilot.status()["validation_workers"] == 2


def test_the_universe_grows_in_steps_and_only_while_validation_keeps_up(autopilot_rt):  # noqa: F811
    rt = autopilot_rt
    listed = [f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(150)]   # what the screener lists today
    small = rt.frames["BBB"].iloc[-300:]
    rt.data.frames = {s: small for s in listed}
    asked = []
    rt.autopilot.most_active = lambda key, secret, top: asked.append(top) or listed
    cfg = AutopilotSettings(enabled=True, strategies=["ema_crossover"], max_instruments=200, universe_step=40, etfs=[], crypto_pairs=[], min_traded_value=1e6)
    with rt.db.session() as s:
        save_settings(s, initial_settings().model_copy(update={"autopilot": cfg}))
        rt.reload_settings(s)
    stocks = lambda: len([i for i in rt.registry.all() if i.asset_class.value == "stock"])  # noqa: E731
    before = stocks()
    sizes = []
    for day in range(4):
        rt.autopilot.state["last_result"] = {"validation": {"remaining": 0}}
        out = rt.autopilot._seed({})
        sizes.append(stocks() - before)
        assert out["growth"]["limit"] == 200
        rt.now[0] += timedelta(hours=25)
    assert sizes == [30, 70, 110, 150]  # thirty to start, then forty more a day
    # Validation did not finish last cycle: the universe waits instead of piling up more work.
    rt.autopilot.state["last_result"] = {"validation": {"remaining": 25}}
    paused = rt.autopilot._seed({})
    assert paused["growth"]["paused"] is True and stocks() - before == 150
    assert AutopilotSettings().max_instruments == 200 and AutopilotSettings().universe_step == 40
