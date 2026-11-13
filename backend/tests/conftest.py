import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.database.base import Database
from app.main import create_app
from app.security.roles import Role
from app.security.users import create_user
from app.security.vault import CredentialVault

ADMIN = ("admin@example.com", "correct-horse-battery")
VIEWER = ("viewer@example.com", "viewer-password-123")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite://", jwt_secret="t" * 48, credential_key=CredentialVault.generate_key(),
                    login_rate_limit_per_minute=5)


@pytest.fixture
def db(tmp_path) -> Database:
    # A file database gives every session its own connection, as Postgres does. An in-memory SQLite database
    # has a single shared connection, on which background jobs and HTTP requests would disturb each other.
    database = Database(f"sqlite:///{tmp_path}/test.db")
    database.create_all()
    with database.session() as s:
        create_user(s, *ADMIN, Role.ADMIN)
        create_user(s, *VIEWER, Role.VIEWER)
    return database


@pytest.fixture
def client(settings, db) -> TestClient:
    return TestClient(create_app(settings, db))


def login(client: TestClient, creds=ADMIN) -> dict:
    r = client.post("/api/auth/login", json={"email": creds[0], "password": creds[1]})
    assert r.status_code == 200, r.text
    client.cookies.clear()  # tests use the bearer token explicitly unless they test cookies
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
