"""Broker credential storage use-cases. Secrets go in encrypted and never come back out via the API."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import BrokerAccount, EncryptedCredential
from ..monitoring import audit
from .vault import CredentialVault

FORBIDDEN_PERMISSIONS = {"withdraw", "withdrawal", "transfer"}


def store_credentials(session: Session, vault: CredentialVault, broker_account_id: str, secrets: dict[str, str],
                      declared_permissions: list[str], actor: str) -> EncryptedCredential:
    if session.get(BrokerAccount, broker_account_id) is None:
        raise LookupError(f"Unknown broker account {broker_account_id}")
    forbidden = FORBIDDEN_PERMISSIONS & {p.lower() for p in declared_permissions}
    if forbidden:
        raise PermissionError("API keys with withdrawal/transfer permissions are not accepted; create a trade-only key")
    if not secrets.get("api_key"):
        raise ValueError("api_key is required")
    ciphertext = vault.encrypt(secrets)
    row = session.scalar(select(EncryptedCredential).where(EncryptedCredential.broker_account_id == broker_account_id))
    hint = secrets["api_key"][-4:]
    if row:
        row.ciphertext, row.key_hint = ciphertext, hint
    else:
        row = EncryptedCredential(broker_account_id=broker_account_id, ciphertext=ciphertext, key_hint=hint)
        session.add(row)
    audit.record(session, actor, "credential.stored", broker_account_id=broker_account_id, key_hint=hint)
    return row


def load_credentials(session: Session, vault: CredentialVault, broker_account_id: str) -> dict[str, str]:
    """Internal use by broker adapters only."""
    row = session.scalar(select(EncryptedCredential).where(EncryptedCredential.broker_account_id == broker_account_id))
    if row is None:
        raise LookupError(f"No credentials stored for {broker_account_id}")
    return vault.decrypt(row.ciphertext)
