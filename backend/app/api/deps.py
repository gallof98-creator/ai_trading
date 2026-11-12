"""Dependency injection: one container per app, resolved from request state."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..database.base import Database
from ..database.models import User
from ..security.rate_limit import RateLimiter
from ..security.roles import Role
from ..security.tokens import TokenError, TokenService
from ..security.vault import CredentialVault

SESSION_COOKIE = "qat_session"
CSRF_HEADER = "x-requested-with"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Container:
    settings: Settings
    db: Database
    tokens: TokenService
    vault: CredentialVault
    login_limiter: RateLimiter


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_session(container: Container = Depends(get_container)) -> Iterator[Session]:
    with container.db.session() as session:
        yield session


def current_user(request: Request, container: Container = Depends(get_container), session: Session = Depends(get_session)) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:]
    else:
        token = request.cookies.get(SESSION_COOKIE, "")
        # Cookie sessions: state-changing requests must carry a custom header, which browsers
        # only allow from the CORS-approved origins (CSRF protection).
        if token and request.method not in _SAFE_METHODS and CSRF_HEADER not in request.headers:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Requested-With header")
    if not token:
        raise unauthorized
    try:
        claims = container.tokens.decode(token)
    except TokenError:
        raise unauthorized from None
    user = session.get(User, int(claims["sub"]))
    if user is None or not user.is_active or user.token_version != claims["ver"]:
        raise unauthorized
    return user


def require_role(minimum: Role) -> Callable[..., User]:
    def dependency(user: User = Depends(current_user)) -> User:
        if Role(user.role).rank < minimum.rank:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role {minimum.value}")
        return user

    return dependency
