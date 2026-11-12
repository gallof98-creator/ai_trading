"""Append-only audit trail."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..database.models import AuditEntry


def record(session: Session, actor: str, action: str, **detail) -> None:
    session.add(AuditEntry(actor=actor, action=action, detail=detail))
