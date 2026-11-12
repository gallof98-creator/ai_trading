import io
import json

import pytest
import structlog
from sqlalchemy import select

from app.core.logging import redact
from app.database.models import EncryptedCredential
from app.security.credentials import load_credentials
from app.security.vault import CredentialVault, VaultError
from tests.conftest import login

BROKER = {"id": "binance-test", "adapter": "ccxt", "label": "Binance testnet", "currency": "USDT"}
SECRET = {"api_key": "AKIA-PUBLIC-1234", "api_secret": "super-secret-value"}


def test_vault_roundtrip_and_wrong_key():
    vault = CredentialVault(CredentialVault.generate_key())
    token = vault.encrypt(SECRET)
    assert "super-secret-value" not in token and vault.decrypt(token) == SECRET
    with pytest.raises(VaultError):
        CredentialVault(CredentialVault.generate_key()).decrypt(token)
    with pytest.raises(VaultError):
        CredentialVault("").encrypt(SECRET)


def test_credentials_are_encrypted_at_rest_and_never_returned(client, db, settings):
    admin = login(client)
    assert client.post("/api/brokers", headers=admin, json=BROKER).status_code == 201
    assert client.put("/api/brokers/binance-test/credentials", headers=admin, json=SECRET).status_code == 204

    listing = client.get("/api/brokers", headers=admin)
    assert listing.json()[0]["has_credentials"] and listing.json()[0]["key_hint"] == "1234"
    audit = client.get("/api/audit", headers=admin)
    for text in (listing.text, audit.text):
        assert "super-secret-value" not in text and "AKIA-PUBLIC-1234" not in text

    with db.session() as s:
        row = s.scalar(select(EncryptedCredential))
        assert "super-secret-value" not in row.ciphertext
        assert load_credentials(s, CredentialVault(settings.credential_key), "binance-test") == SECRET


def test_withdrawal_capable_keys_are_refused(client):
    admin = login(client)
    client.post("/api/brokers", headers=admin, json=BROKER)
    r = client.put("/api/brokers/binance-test/credentials", headers=admin, json={**SECRET, "permissions": ["read", "trade", "Withdraw"]})
    assert r.status_code == 400 and "withdrawal" in r.json()["detail"]


def test_secrets_in_broker_config_are_refused(client):
    r = client.post("/api/brokers", headers=login(client), json={**BROKER, "config": {"apiSecret": "x"}})
    assert r.status_code == 400


def test_live_broker_account_requires_deployment_authorization(client):
    admin = login(client)
    assert client.post("/api/brokers", headers=admin, json={**BROKER, "is_live": True}).status_code == 403
    assert client.post("/api/brokers", headers=admin, json={**BROKER, "id": "p1", "adapter": "paper", "is_live": True}).status_code == 400
    assert client.get("/health").json()["live_trading_authorized"] is False


def test_unknown_broker_and_input_validation(client):
    admin = login(client)
    assert client.put("/api/brokers/nope/credentials", headers=admin, json=SECRET).status_code == 404
    assert client.post("/api/brokers", headers=admin, json={**BROKER, "id": "Bad Id!"}).status_code == 422
    assert client.post("/api/brokers", headers=admin, json={**BROKER, "adapter": "evil"}).status_code == 422


def test_log_redaction():
    out = io.StringIO()
    logger = structlog.wrap_logger(structlog.PrintLogger(out), processors=[redact, structlog.processors.JSONRenderer()])
    logger.info("connect", api_key="AKIA", nested={"apiSecret": "s3cret", "symbol": "BTC"}, items=[{"password": "p"}])
    logged = json.loads(out.getvalue())
    assert logged["api_key"] == "***" and logged["nested"] == {"apiSecret": "***", "symbol": "BTC"} and logged["items"] == [{"password": "***"}]
