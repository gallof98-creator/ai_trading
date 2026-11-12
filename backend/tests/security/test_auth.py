from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import Settings
from app.main import create_app
from app.security.passwords import hash_password, verify_password
from app.security.rate_limit import RateLimiter
from tests.conftest import ADMIN, VIEWER, login


def test_password_hashing_is_salted_and_verifiable():
    h1, h2 = hash_password("a-long-password-1"), hash_password("a-long-password-1")
    assert h1 != h2 and h1.startswith("$argon2")
    assert verify_password("a-long-password-1", h1)
    assert not verify_password("wrong-password-xx", h1)
    assert not verify_password("x", "not-a-hash")
    with pytest.raises(ValueError):
        hash_password("short")


def test_login_and_me(client):
    headers = login(client)
    me = client.get("/api/auth/me", headers=headers).json()
    assert me["email"] == ADMIN[0] and me["role"] == "admin"


def test_wrong_password_and_unknown_user_give_same_error(client):
    a = client.post("/api/auth/login", json={"email": ADMIN[0], "password": "nope-nope-nope"})
    b = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_unauthenticated_requests_are_rejected(client):
    for path in ("/api/auth/me", "/api/settings", "/api/brokers", "/api/audit"):
        assert client.get(path).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_forged_and_expired_tokens_are_rejected(client):
    now = datetime.now(timezone.utc)
    forged = jwt.encode({"sub": "1", "role": "admin", "ver": 0, "exp": now + timedelta(hours=1)}, "x" * 48, algorithm="HS256")
    expired = jwt.encode({"sub": "1", "role": "admin", "ver": 0, "exp": now - timedelta(seconds=1)}, "t" * 48, algorithm="HS256")
    unsigned = jwt.encode({"sub": "1", "role": "admin", "ver": 0, "exp": now + timedelta(hours=1)}, None, algorithm="none")
    for token in (forged, expired, unsigned):
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_role_in_token_cannot_escalate(client, settings):
    """Authorization uses the role stored in the database, not the token claim."""
    viewer = client.post("/api/auth/login", json={"email": VIEWER[0], "password": VIEWER[1]}).json()
    client.cookies.clear()
    now = datetime.now(timezone.utc)
    tampered = jwt.encode({"sub": str(viewer["user"]["id"]), "role": "admin", "ver": 0, "exp": now + timedelta(hours=1)},
                          settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/audit", headers={"Authorization": f"Bearer {tampered}"}).status_code == 403


def test_rbac(client):
    viewer = login(client, VIEWER)
    assert client.get("/api/settings", headers=viewer).status_code == 200
    assert client.put("/api/settings", headers=viewer, json={}).status_code == 403
    assert client.get("/api/audit", headers=viewer).status_code == 403
    r = client.post("/api/auth/users", headers=viewer, json={"email": "x@example.com", "password": "long-enough-pass", "role": "admin"})
    assert r.status_code == 403


def test_admin_creates_user(client):
    admin = login(client)
    body = {"email": "Trader@Example.com", "password": "trader-password-1", "role": "trader"}
    assert client.post("/api/auth/users", headers=admin, json=body).status_code == 201
    assert client.post("/api/auth/users", headers=admin, json=body).status_code == 400  # duplicate
    assert login(client, ("trader@example.com", "trader-password-1"))


def test_logout_revokes_existing_tokens(client):
    headers = login(client)
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_cookie_session_requires_csrf_header_for_writes(client):
    r = client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").status_code == 200  # cookie is enough for reads
    assert client.put("/api/settings", json={}).status_code == 403
    assert client.put("/api/settings", json={}, headers={"X-Requested-With": "qat"}).status_code == 200


def test_login_rate_limit(client):
    codes = [client.post("/api/auth/login", json={"email": ADMIN[0], "password": "bad-bad-bad-bad"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


def test_rate_limiter_window_slides():
    t = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    t[0] = 61
    assert rl.allow("a")


def test_production_requires_secrets(db):
    with pytest.raises(RuntimeError, match="QAT_JWT_SECRET"):
        create_app(Settings(_env_file=None, environment="production"), db)
    with pytest.raises(RuntimeError, match="32 characters"):
        create_app(Settings(_env_file=None, environment="production", jwt_secret="short", credential_key="k"), db)
