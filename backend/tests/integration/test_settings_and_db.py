import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.database.base import Base, Database
from app.database.models import AuditEntry, OrderRow, User
from app.database.settings_store import UserSettings
from tests.conftest import login


def test_settings_defaults_require_capital_to_be_configured(client):
    s = client.get("/api/settings", headers=login(client)).json()
    assert s["base_currency"] == "EUR" and s["initial_paper_capital"] is None
    assert s["trading_mode"] == "manual" and s["risk"]["emergency_policy"] == "block_new_risk"


def test_settings_update_is_validated_persisted_and_audited(client):
    admin = login(client)
    body = UserSettings(initial_paper_capital=25_000, trading_mode="semi_auto").model_dump()
    body["risk"]["risk_per_trade"] = 0.005
    assert client.put("/api/settings", headers=admin, json=body).status_code == 200
    saved = client.get("/api/settings", headers=admin).json()
    assert saved["initial_paper_capital"] == 25_000 and saved["risk"]["risk_per_trade"] == 0.005
    assert client.get("/api/audit", headers=admin).json()[0]["action"] == "settings.updated"

    for bad in ({"initial_paper_capital": -5}, {"trading_mode": "yolo"}, {"risk": {"risk_per_trade": 0.9}},
                {"risk": {"emergency_policy": "sell_everything"}}, {"base_currency": "euro"}):
        assert client.put("/api/settings", headers=admin, json=bad).status_code == 422, bad
    assert client.get("/api/settings", headers=admin).json() == saved


def test_transaction_rolls_back_on_failure(db):
    with pytest.raises(RuntimeError):
        with db.session() as s:
            s.add(AuditEntry(actor="t", action="will.rollback"))
            raise RuntimeError("boom")
    with db.session() as s:
        assert s.scalar(select(AuditEntry).where(AuditEntry.action == "will.rollback")) is None


def test_order_idempotency_key_is_unique_at_database_level(db):
    def order(i):
        return OrderRow(id=f"o{i}", idempotency_key="same-key", broker_account_id="paper", is_simulated=True, instrument_id="X",
                        side="buy", qty=1, status="new", payload={})
    with db.session() as s:
        s.add(order(1))
    with pytest.raises(IntegrityError):
        with db.session() as s:
            s.add(order(2))


def test_health_reports_database_failure(client, db):
    assert client.get("/health").json()["status"] == "ok"
    db.engine.dispose()
    Base.metadata.drop_all(db.engine)
    db.session = None  # simulate an unusable database
    assert client.get("/health").json() == {**client.get("/health").json(), "status": "degraded", "database": False}


def test_migrations_upgrade_matches_models_and_downgrade_is_clean(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/m.db")
    cfg = Config("alembic.ini")
    cfg.attributes["engine"] = database.engine
    command.upgrade(cfg, "head")
    tables = set(inspect(database.engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    with database.session() as s:
        s.add(User(email="a@b.c", password_hash="x", role="viewer"))
    command.downgrade(cfg, "base")
    assert set(inspect(database.engine).get_table_names()) == {"alembic_version"}
