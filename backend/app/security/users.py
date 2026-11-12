"""User management use-cases (kept out of API routes)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models import User
from ..monitoring import audit
from .passwords import hash_password, verify_password
from .roles import Role

# Verified against when the email is unknown, so response time does not reveal which emails exist.
_DUMMY_HASH = hash_password("timing-equalisation-only")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_user(session: Session, email: str, password: str, role: Role, actor: str = "system") -> User:
    email = normalize_email(email)
    if "@" not in email:
        raise ValueError("Invalid email address")
    if session.scalar(select(User).where(User.email == email)):
        raise ValueError("A user with this email already exists")
    user = User(email=email, password_hash=hash_password(password), role=role.value)
    session.add(user)
    session.flush()
    audit.record(session, actor, "user.created", email=email, role=role.value)
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.is_active:
        return None
    return user


def bootstrap_admin(session: Session, email: str, password: str) -> User | None:
    """Create the first admin from environment settings, only while no user exists."""
    if not email or not password or session.scalar(select(func.count()).select_from(User)):
        return None
    return create_user(session, email, password, Role.ADMIN, actor="bootstrap")
