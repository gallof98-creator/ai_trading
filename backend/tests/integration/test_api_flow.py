"""End-to-end HTTP flow through the whole platform, on simulated data and paper accounts only."""
import pytest

from app.database.settings_store import UserSettings
from tests.conftest import VIEWER, login

BTC = "CRYPTO:BTC/USDT"
FX = {"USD": 0.92}


@pytest.fixture
def api(client, tmp_path):
    client.app.state.container.settings.model_dir = str(tmp_path / "models")
    admin = login(client)
    body = UserSettings(initial_paper_capital=50_000, trading_mode="auto").model_dump()
    assert client.put("/api/settings", headers=admin, json=body).status_code == 200
    return client, admin


def test_everything_requires_authentication_and_roles(api):
    client, admin = api
    for path in ("/api/overview", "/api/orders", "/api/risk", "/api/strategies", "/api/deployments", "/api/notifications", "/api/backtests",
                 "/api/ai/models", "/api/ai/proposals", "/api/market/coverage", "/api/fx", "/api/brokers/status", "/api/approvals"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=admin).status_code == 200, path
    viewer = login(client, VIEWER)
    writes = [("/api/paper/accounts", {"id": "p1", "label": "x"}), ("/api/deployments", {"strategy": "macd", "instrument_id": BTC, "timeframe": "1h", "broker_id": "p"}),
              ("/api/risk/kill-switch", {"engage": True}), ("/api/backtests", {"deployments": [{"strategy": "macd", "instrument_id": BTC, "timeframe": "1h"}]}),
              ("/api/fx", {"currency": "USD", "rate": 0.9}), ("/api/paper/replay", {"instrument_id": BTC, "timeframe": "1h"}),
              ("/api/ai/proposals", {"strategy": "macd", "instrument_id": BTC, "timeframe": "1h"}), ("/api/market/generate-simulated", {"instrument_id": BTC, "timeframe": "1h"})]
    for path, body in writes:
        assert client.post(path, headers=viewer, json=body).status_code == 403, path


def test_paper_trading_flow_over_http(api):
    client, admin = api
    assert client.get("/api/overview", headers=admin).json()["configured"] is False
    assert client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"}).status_code == 201
    assert client.get("/api/overview", headers=admin).json()["equity"] == 50_000
    r = client.post("/api/deployments", headers=admin, json={"strategy": "ema_crossover", "params": {"fast": 99, "slow": 5}, "instrument_id": BTC,
                                                             "timeframe": "1h", "broker_id": "paper-main"})
    assert r.status_code == 400 and "fast must be smaller" in r.json()["detail"]
    dep = client.post("/api/deployments", headers=admin, json={"strategy": "ema_crossover", "params": {"fast": 5, "slow": 20}, "instrument_id": BTC,
                                                               "timeframe": "1h", "broker_id": "paper-main"}).json()["id"]
    replay = {"instrument_id": BTC, "timeframe": "1h", "bars": 400, "seed": 3, "fx_assumptions": FX}
    assert "Activate a strategy" in client.post("/api/paper/replay", headers=admin, json=replay).json()["detail"]  # inactive deployments never trade
    assert client.patch(f"/api/deployments/{dep}", headers=admin, json={"active": True}).status_code == 204
    out = client.post("/api/paper/replay", headers=admin, json=replay)
    assert out.status_code == 200 and out.json()["data_kind"] == "simulated"

    ov = client.get("/api/overview", headers=admin).json()
    assert ov["number_of_trades"] > 3 and ov["all_simulated"] and ov["reconciled"] and ov["active_strategies"] == 1
    orders = client.get("/api/orders", headers=admin).json()
    assert orders and all(o["is_simulated"] for o in orders)
    assert client.get("/api/fills", headers=admin).json()[0]["is_simulated"] is True
    assert client.get("/api/signals", headers=admin).json()[0]["confidence"] is None
    assert len(client.get("/api/trades", headers=admin).json()) == ov["number_of_trades"]
    assert len(client.get("/api/portfolio/equity", headers=admin).json()) > 50
    deps = client.get("/api/deployments", headers=admin).json()
    assert deps[0]["trades"] == ov["number_of_trades"] and deps[0]["is_active"]
    history = client.get(f"/api/deployments/{dep}/history", headers=admin).json()
    assert [h["action"] for h in history] == ["deployment.activated", "deployment.created"]
    notes = client.get("/api/notifications", headers=admin).json()
    assert notes["unread"] > 0 and {"signal", "execution"} <= {n["category"] for n in notes["items"]}
    assert client.post("/api/notifications/read-all", headers=admin).status_code == 204
    assert client.get("/api/notifications", headers=admin).json()["unread"] == 0
    chart = client.get("/api/market/analysis", headers=admin, params={"instrument_id": BTC, "timeframe": "1h", "strategy": "ema_crossover"}).json()
    assert chart["kind"] == "simulated" and len(chart["candles"]) == len(chart["regime"]) == len(chart["indicators"]["ema_20"])

    # Risk centre and kill switch
    risk = client.get("/api/risk", headers=admin).json()
    assert risk["status"]["halted"] is False and risk["utilisation"]["drawdown"]["limit"] == 0.15
    assert client.post("/api/risk/kill-switch", headers=admin, json={"engage": True, "reason": "test"}).status_code == 204
    assert client.get("/api/risk", headers=admin).json()["status"]["kill_switch"] == "test"
    n = len(client.get("/api/orders", headers=admin, params={"limit": 1000}).json())
    client.post("/api/paper/replay", headers=admin, json={**replay, "bars": 200})
    after = client.get("/api/orders", headers=admin, params={"limit": 1000}).json()
    new_entries = [o for o in after if not o["reduce_only"] and o["status"] not in ("rejected", "cancelled")]
    assert len(new_entries) <= len([o for o in after if not o["reduce_only"]]) and len(after) >= n
    assert any("kill switch" in (o.get("reason") or "") for o in after)
    assert client.post("/api/risk/kill-switch", headers=admin, json={"engage": False}).status_code == 204

    # Reset needs the typed confirmation
    assert client.post("/api/paper/accounts/paper-main/reset", headers=admin, json={"confirmation": "ok"}).status_code == 400
    assert client.post("/api/paper/accounts/paper-main/reset", headers=admin, json={"confirmation": "paper-main"}).status_code == 204
    assert client.get("/api/overview", headers=admin).json()["equity"] == 50_000


def test_research_flow_over_http(api):
    client, admin = api
    q = {"instrument_id": BTC, "timeframe": "1h"}
    assert client.get("/api/market/analysis", headers=admin, params=q).status_code == 404  # no data yet: nothing is invented
    gen = client.post("/api/market/generate-simulated", headers=admin, json={**q, "bars": 3000, "seed": 9})
    assert gen.status_code == 200 and gen.json()["kind"] == "simulated"
    assert client.post("/api/market/generate-simulated", headers=admin, json={**q, "bars": 3000, "seed": 9}).status_code == 400  # no silent overwrite
    assert client.get("/api/market/coverage", headers=admin).json()[0]["kind"] == "simulated"
    assert client.get("/api/market/quality", headers=admin, params=q).json()["ok"] is True
    assert client.get("/api/ai/regime", headers=admin, params=q).json()["current"]["primary"]
    assert isinstance(client.get("/api/ai/anomalies", headers=admin, params=q).json(), list)

    body = {"label": "EMA test", "deployments": [{"strategy": "ema_crossover", "params": {"fast": 10, "slow": 40}, **q}], "static_fx": {"USDT": 0.92}}
    run = client.post("/api/backtests", headers=admin, json=body)
    assert run.status_code == 200, run.text
    result = run.json()
    assert result["metrics"]["number_of_trades"] > 10 and any("SIMULATED" in w for w in result["warnings"]) and "do not guarantee" in result["disclaimer"]
    again = client.post("/api/backtests", headers=admin, json=body).json()
    assert again["config_hash"] == result["config_hash"] and again["metrics"] == result["metrics"]  # reproducible
    listing = client.get("/api/backtests", headers=admin).json()
    assert len(listing) == 2 and listing[0]["data_kinds"] == ["simulated"]
    assert client.get(f"/api/backtests/{result['id']}", headers=admin).json()["equity_curve"]
    bad = client.post("/api/backtests", headers=admin, json={**body, "deployments": [{"strategy": "ema_crossover", "instrument_id": "STOCK:AAPL", "timeframe": "1d"}]})
    assert bad.status_code == 404 and "No stored bars" in bad.json()["detail"]

    report = client.post("/api/ai/reports", headers=admin, json={"backtest_id": result["id"]}).json()
    assert report["payload"]["reproduced"] is True and "## Statistical weaknesses" in report["payload"]["markdown"]
    assert len(client.get("/api/ai/reports", headers=admin).json()) == 1

    wf = client.post("/api/backtests/walk-forward", headers=admin, json={"strategy": "ema_crossover", **q, "param_grid": {"fast": [5, 10], "slow": [30, 50]},
                                                                         "n_folds": 2, "static_fx": {"USDT": 0.92}})
    assert wf.status_code == 200 and len(wf.json()["folds"]) == 2
    cmp_ = client.post("/api/backtests/compare", headers=admin, json={"static_fx": {"USDT": 0.92}, "items": [
        {"strategy": "ema_crossover", **q, "label": "EMA"}, {"strategy": "donchian_breakout", **q, "label": "Donchian"}]})
    assert cmp_.status_code == 200 and {r["label"] for r in cmp_.json()["rows"]} == {"EMA", "Donchian"}

    model = client.post("/api/ai/models/train", headers=admin, json={**q, "model": "logistic_regression", "horizon": 20})
    assert model.status_code == 200 and model.json()["status"] == "rejected"  # random walk: honestly reported as no edge
    assert model.json()["payload"]["artifact"] is None
    drift = client.get(f"/api/ai/models/{model.json()['id']}/drift", headers=admin).json()
    assert drift["status"] in ("ok", "warning", "alert") and "basis" in drift
    assert client.post("/api/ai/models/train", headers=admin, json={**q, "model": "skynet"}).status_code == 422

    props = client.post("/api/ai/proposals", headers=admin, json={"strategy": "ema_crossover", **q, "n_trials": 6, "static_fx": {"USDT": 0.92}})
    assert props.status_code == 200 and props.json()
    failed = [p for p in props.json() if p["status"] == "failed_validation"]
    assert failed  # noise does not survive the out-of-sample gate
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    refused = client.post(f"/api/ai/proposals/{failed[0]['id']}/apply", headers=admin, json={"broker_id": "paper-main"})
    assert refused.status_code == 409 and "cannot be applied" in refused.json()["detail"]
    assert client.get("/api/deployments", headers=admin).json() == []  # nothing was deployed
    assert client.post(f"/api/ai/proposals/{failed[0]['id']}/dismiss", headers=admin).status_code == 204


def test_validated_proposal_is_applied_inactive_and_only_by_an_admin(api):
    from app.database.models import ResearchItem
    from app.security.roles import Role
    from app.security.users import create_user

    client, admin = api
    db = client.app.state.container.db
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    with db.session() as s:
        create_user(s, "trader@example.com", "trader-password-1", Role.TRADER)
        s.add(ResearchItem(id="good", kind="proposal", status="validated", title="t",
                           payload={"strategy": "ema_crossover", "instrument_id": BTC, "timeframe": "1h", "params": {"fast": 8, "slow": 30}}))
        s.add(ResearchItem(id="evil", kind="proposal", status="validated", title="t",
                           payload={"strategy": "ema_crossover", "instrument_id": BTC, "timeframe": "1h", "params": {"fast": 8, "slow": 30, "risk_per_trade": 0.5}}))
    trader = login(client, ("trader@example.com", "trader-password-1"))
    assert client.post("/api/ai/proposals/good/apply", headers=trader, json={"broker_id": "paper-main"}).status_code == 403
    tampered = client.post("/api/ai/proposals/evil/apply", headers=admin, json={"broker_id": "paper-main"})
    assert tampered.status_code == 409 and "non-strategy settings" in tampered.json()["detail"]
    ok = client.post("/api/ai/proposals/good/apply", headers=admin, json={"broker_id": "paper-main"})
    assert ok.status_code == 200 and ok.json()["activated"] is False
    dep = client.get("/api/deployments", headers=admin).json()[0]
    assert dep["params"]["fast"] == 8 and dep["is_active"] is False  # a human still has to switch it on
    assert client.get("/api/settings", headers=admin).json()["risk"]["risk_per_trade"] == 0.01  # risk settings untouched
    assert client.post("/api/ai/proposals/good/apply", headers=admin, json={"broker_id": "paper-main"}).status_code == 409  # already applied


def test_fx_endpoints_and_validation(api):
    client, admin = api
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    rates = {r["currency"]: r for r in client.get("/api/fx", headers=admin).json()}
    assert rates["USD"]["available"] is False
    assert client.post("/api/fx", headers=admin, json={"currency": "USD", "rate": 0.92}).status_code == 204
    rates = {r["currency"]: r for r in client.get("/api/fx", headers=admin).json()}
    assert rates["USD"]["rate"] == pytest.approx(0.92) and rates["USDT"]["source"] == "manual"  # USDT valued through its configured USD peg
    for bad in ({"currency": "usd", "rate": 1}, {"currency": "USD", "rate": -1}, {"currency": "USD", "rate": 0}):
        assert client.post("/api/fx", headers=admin, json=bad).status_code == 422
    blocked = client.put("/api/settings", headers=admin, json={**client.get("/api/settings", headers=admin).json(), "base_currency": "USD"})
    assert blocked.status_code == 409
