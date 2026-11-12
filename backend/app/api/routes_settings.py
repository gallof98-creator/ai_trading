from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import AuditEntry, BrokerAccount, EncryptedCredential, User
from ..database.settings_store import UserSettings, load_settings, save_settings
from ..monitoring import audit
from ..security import credentials
from ..security.roles import Role
from ..security.vault import VaultError
from .deps import Container, get_container, get_session, require_role

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", response_model=UserSettings)
def get_settings(_: User = Depends(require_role(Role.VIEWER)), session: Session = Depends(get_session)) -> UserSettings:
    return load_settings(session)


@router.put("/settings", response_model=UserSettings)
def put_settings(body: UserSettings, admin: User = Depends(require_role(Role.ADMIN)),
                 session: Session = Depends(get_session)) -> UserSettings:
    before = load_settings(session)
    saved = save_settings(session, body)
    audit.record(session, admin.email, "settings.updated", before=before.model_dump(), after=saved.model_dump())
    return saved


class BrokerAccountIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    adapter: str = Field(pattern=r"^(paper|ccxt|ibkr)$")
    label: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern=r"^[A-Z]{3,5}$")
    is_live: bool = False
    config: dict = Field(default_factory=dict)


class BrokerAccountOut(BrokerAccountIn):
    has_credentials: bool = False
    key_hint: str | None = None


class CredentialIn(BaseModel):
    api_key: str = Field(min_length=4, max_length=512)
    api_secret: str = Field("", max_length=512)
    passphrase: str = Field("", max_length=512)
    # Permissions the key was created with at the broker. Withdrawal-capable keys are refused.
    permissions: list[str] = Field(default_factory=lambda: ["read", "trade"])


@router.get("/brokers", response_model=list[BrokerAccountOut])
def list_brokers(_: User = Depends(require_role(Role.VIEWER)), session: Session = Depends(get_session)) -> list[BrokerAccountOut]:
    hints = {c.broker_account_id: c.key_hint for c in session.scalars(select(EncryptedCredential))}
    return [
        BrokerAccountOut(id=a.id, adapter=a.adapter, label=a.label, currency=a.currency, is_live=a.is_live, config=a.config,
                         has_credentials=a.id in hints, key_hint=hints.get(a.id))
        for a in session.scalars(select(BrokerAccount).order_by(BrokerAccount.id))
    ]


@router.post("/brokers", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
def create_broker(body: BrokerAccountIn, admin: User = Depends(require_role(Role.ADMIN)), session: Session = Depends(get_session),
                  container: Container = Depends(get_container)) -> BrokerAccountOut:
    if session.get(BrokerAccount, body.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Broker account id already exists")
    if body.adapter == "paper" and body.is_live:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A paper account cannot be live")
    if body.is_live and not container.settings.live_trading_authorized:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Live trading is not authorized in this deployment")
    if any(s in k.lower() for k in body.config for s in ("secret", "key", "password", "token")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Secrets must be stored via the credentials endpoint, not in config")
    session.add(BrokerAccount(**body.model_dump()))
    audit.record(session, admin.email, "broker.created", broker_account_id=body.id, adapter=body.adapter, is_live=body.is_live)
    return BrokerAccountOut(**body.model_dump())


@router.put("/brokers/{broker_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
def put_credentials(broker_id: str, body: CredentialIn, admin: User = Depends(require_role(Role.ADMIN)),
                    session: Session = Depends(get_session), container: Container = Depends(get_container)) -> None:
    secrets = {k: v for k, v in body.model_dump(exclude={"permissions"}).items() if v}
    try:
        credentials.store_credentials(session, container.vault, broker_id, secrets, body.permissions, actor=admin.email)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except VaultError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


class AuditOut(BaseModel):
    id: int
    ts: str
    actor: str
    action: str
    detail: dict


@router.get("/audit", response_model=list[AuditOut])
def list_audit(limit: int = 100, _: User = Depends(require_role(Role.ADMIN)), session: Session = Depends(get_session)) -> list[AuditOut]:
    rows = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(max(1, min(limit, 1000))))
    return [AuditOut(id=r.id, ts=r.ts.isoformat(), actor=r.actor, action=r.action, detail=r.detail) for r in rows]
