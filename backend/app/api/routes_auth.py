from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database.models import User
from ..monitoring import audit
from ..security import users
from ..security.roles import Role
from .deps import SESSION_COOKIE, Container, current_user, get_container, get_session, require_role

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class UserOut(BaseModel):
    id: int
    email: str
    role: Role


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class CreateUserRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=12, max_length=1024)
    role: Role


def _out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=Role(user.role))


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response, container: Container = Depends(get_container),
          session: Session = Depends(get_session)) -> LoginResponse:
    client = request.client.host if request.client else "unknown"
    if not container.login_limiter.allow(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts; try again later")
    user = users.authenticate(session, body.email, body.password)
    if user is None:
        audit.record(session, users.normalize_email(body.email)[:320], "auth.login_failed", ip=client)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token = container.tokens.issue(user.id, user.role, user.token_version)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=container.settings.cookie_secure,
                        max_age=container.settings.jwt_ttl_minutes * 60, path="/")
    audit.record(session, user.email, "auth.login", ip=client)
    return LoginResponse(access_token=token, user=_out(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, user: User = Depends(current_user), session: Session = Depends(get_session)) -> None:
    """Revokes every session of this user (token version bump) and clears the cookie."""
    session.get(User, user.id).token_version += 1
    audit.record(session, user.email, "auth.logout")
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return _out(user)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserRequest, admin: User = Depends(require_role(Role.ADMIN)),
                session: Session = Depends(get_session)) -> UserOut:
    try:
        return _out(users.create_user(session, body.email, body.password, body.role, actor=admin.email))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
