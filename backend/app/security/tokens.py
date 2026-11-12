"""Short-lived signed session tokens (JWT, HS256)."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import jwt

ALGORITHM = "HS256"


class TokenError(Exception):
    pass


class TokenService:
    def __init__(self, secret: str, ttl_minutes: int) -> None:
        # Without a configured secret (development only) use a random per-process key:
        # sessions simply end on restart instead of being signed with a guessable default.
        self._secret = secret or secrets.token_urlsafe(48)
        self._ttl = timedelta(minutes=ttl_minutes)

    def issue(self, user_id: int, role: str, token_version: int) -> str:
        now = datetime.now(timezone.utc)
        claims = {"sub": str(user_id), "role": role, "ver": token_version, "iat": now, "exp": now + self._ttl}
        return jwt.encode(claims, self._secret, algorithm=ALGORITHM)

    def decode(self, token: str) -> dict:
        try:
            return jwt.decode(token, self._secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub", "ver"]})
        except jwt.PyJWTError as exc:
            raise TokenError(str(exc)) from None
