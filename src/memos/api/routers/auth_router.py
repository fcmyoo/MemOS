"""Web console auth routes: register / login / logout / refresh / me.

Implements the HTTP surface of docs/plans/web-console-session-design.md on
top of :mod:`memos.api.web_auth` primitives. Coexists with the existing
API-key middleware (``memos.api.middleware.auth``): these routes accept only
``wca_``/``wcr_`` web tokens, never ``mk_``/``krlk_`` keys.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response

from memos.log import get_logger
from memos.mem_user.user_manager import UserManager, UserRole

from ..web_auth import (
    ACCESS_TOKEN_PREFIX,
    REFRESH_TOKEN_PREFIX,
    ClockService,
    RotateStatus,
    SessionService,
    WebAuthError,
    WebPasswordService,
    WebRateLimiter,
    WebSessionStore,
    WebTokenService,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Services container (injectable for tests)
# ---------------------------------------------------------------------------


@dataclass
class AuthServices:
    user_manager: UserManager
    session_service: SessionService
    passwords: WebPasswordService
    rate_limiter: WebRateLimiter
    clock: ClockService = field(default_factory=ClockService)


_current_services: AuthServices | None = None


def set_auth_services(services: AuthServices | None) -> None:
    """Install (or clear) the services used by the auth routes."""
    global _current_services
    _current_services = services


def get_services() -> AuthServices:
    if _current_services is None:
        raise RuntimeError("auth services not configured")
    return _current_services


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WebAuthHTTPError(Exception):
    """Raised inside handlers; rendered by the registered exception handler."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def web_auth_error_handler(request: Request, exc: WebAuthHTTPError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=exc.status_code, content={"message": exc.message}, headers=headers)


def _services() -> AuthServices:
    return get_services()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_USER_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")

#: Common passwords that are rejected outright (sample list).
_COMMON_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "password1234",
    "12345678",
    "123456789",
    "qwerty123",
    "letmein1",
    "admin123",
    "welcome1",
    "abc12345",
    "iloveyou1",
}


def _validate_user_name(user_name: str) -> str | None:
    if not isinstance(user_name, str) or not _USER_NAME_RE.match(user_name):
        return "invalid_user_name"
    return None


def _validate_password(password: str) -> str | None:
    if not isinstance(password, str) or not (12 <= len(password) <= 128):
        return "invalid_password_length"
    if password.lower() in _COMMON_PASSWORDS:
        return "weak_password"
    return None


def _registration_mode() -> str:
    return os.getenv("REGISTRATION_MODE", "public").strip().lower() or "public"


def _invite_code() -> str:
    return os.getenv("WEB_CONSOLE_INVITE_CODE", "")


# ---------------------------------------------------------------------------
# Token pair serialization
# ---------------------------------------------------------------------------


def _pair_body(pair, user) -> dict[str, Any]:
    return {
        "user": {
            "user_id": user.user_id,
            "user_name": user.user_name,
            "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        },
        "token_type": "Bearer",
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "access_expires_at": pair.access_expires_at.isoformat(),
        "refresh_expires_at": pair.refresh_expires_at.isoformat(),
        "rotation": pair.rotation,
    }


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------


@router.post("/register", status_code=201)
def register(request: Request, body: dict[str, Any]) -> JSONResponse:
    svc = _services()
    mode = _registration_mode()

    if mode == "admin_only":
        return JSONResponse(status_code=404, content={"message": "registration_disabled"})
    if mode == "invite":
        if body.get("invite_code") != _invite_code():
            return JSONResponse(status_code=403, content={"message": "invalid_invite_code"})

    user_name = body.get("user_name", "")
    password = body.get("password", "")

    name_err = _validate_user_name(user_name)
    if name_err is not None:
        return JSONResponse(status_code=422, content={"message": name_err})
    pass_err = _validate_password(password)
    if pass_err is not None:
        return JSONResponse(status_code=422, content={"message": pass_err})

    # Rate limit: 3 registrations per hour per IP.
    if not svc.rate_limiter.allow(f"register:ip:{_client_ip(request)}", 3, timedelta(hours=1)):
        return JSONResponse(status_code=429, content={"message": "rate_limited"})

    um = svc.user_manager
    if um.get_user_by_name(user_name) is not None:
        return JSONResponse(status_code=409, content={"message": "user_name_taken"})

    try:
        user_id = um.create_user(user_name=user_name, role=UserRole.USER)
        user = um.get_user(user_id)
        if user is None:
            raise WebAuthHTTPError(500, "user_creation_failed")
        # Persist the Argon2id password hash (dedicated commit-bound method).
        if not um.set_user_password(user_id, svc.passwords.hash_password(password)):
            raise WebAuthHTTPError(500, "user_creation_failed")
        user = um.get_user(user_id)
        # Create the default private cube owned by this user.
        cube_id = um.create_cube(cube_name=user_name, owner_id=user_id)
        um.add_user_to_cube(user_id, cube_id)
    except WebAuthHTTPError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("register failed")
        raise WebAuthHTTPError(500, "registration_failed") from exc

    pair = svc.session_service.issue_pair(user_id)
    cube = um.get_cube(cube_id)
    body = _pair_body(pair, user)
    body["default_cube"] = {
        "cube_id": cube.cube_id,
        "cube_name": cube.cube_name,
        "owner_id": cube.owner_id,
    } if cube else None
    return JSONResponse(
        status_code=201,
        content=body,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@router.post("/login")
def login(request: Request, body: dict[str, Any]) -> JSONResponse:
    svc = _services()
    user_name = body.get("user_name", "")
    password = body.get("password", "")

    # 10 attempts per minute per IP.
    if not svc.rate_limiter.allow(f"login:ip:{_client_ip(request)}", 10, timedelta(minutes=1)):
        return JSONResponse(status_code=429, content={"message": "rate_limited"})

    um = svc.user_manager
    user = um.get_user_by_name(user_name) if isinstance(user_name, str) else None
    valid = (
        user is not None
        and user.is_active
        and user.password_hash is not None
        and svc.passwords.verify_password(user.password_hash, password)
    )
    if not valid:
        # Unified 401: never reveal whether the user exists.
        return JSONResponse(status_code=401, content={"message": "invalid_credentials"})

    pair = svc.session_service.issue_pair(user.user_id)
    return JSONResponse(status_code=200, content=_pair_body(pair, user), headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


@router.post("/refresh")
def refresh(request: Request, body: dict[str, Any]) -> JSONResponse:
    svc = _services()
    refresh_token = body.get("refresh_token", "")

    if not svc.rate_limiter.allow(f"refresh:ip:{_client_ip(request)}", 30, timedelta(minutes=1)):
        return JSONResponse(status_code=429, content={"message": "rate_limited"})

    if not isinstance(refresh_token, str) or not refresh_token.startswith(REFRESH_TOKEN_PREFIX):
        raise WebAuthHTTPError(401, "refresh_token_invalid")

    try:
        pair = svc.session_service.rotate_refresh(refresh_token)
    except WebAuthError as exc:
        raise WebAuthHTTPError(401, exc.code) from exc

    user = svc.user_manager.get_user(pair.session_family_id and svc.session_service.store.get_session(pair.session_family_id).user_id)
    if user is None:
        raise WebAuthHTTPError(401, "session_revoked")
    return JSONResponse(status_code=200, content=_pair_body(pair, user), headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post("/logout")
def logout(request: Request, authorization: str | None = Header(default=None), body: dict[str, Any] | None = None) -> Response:
    svc = _services()
    body = body or {}
    access_token: str | None = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization[len("Bearer "):].strip()

    refresh_token = body.get("refresh_token")
    if not access_token and not refresh_token:
        raise WebAuthHTTPError(401, "access_token_invalid")

    try:
        svc.session_service.logout(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        )
    except WebAuthError as exc:
        raise WebAuthHTTPError(401, exc.code) from exc
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


def _require_access(authorization: str | None) -> tuple[Any, Any]:
    """Return ``(user, session_record)`` for a valid access token or raise 401."""
    svc = _services()
    if not authorization or not authorization.startswith("Bearer "):
        raise WebAuthHTTPError(401, "access_token_invalid")
    access_token = authorization[len("Bearer "):].strip()
    try:
        record = svc.session_service.validate_access(access_token)
    except WebAuthError as exc:
        raise WebAuthHTTPError(401, exc.code) from exc
    user = svc.user_manager.get_user(record.user_id)
    if user is None:
        raise WebAuthHTTPError(401, "session_revoked")
    return user, record


@router.get("/me")
def me(authorization: str | None = Header(default=None)) -> JSONResponse:
    user, _ = _require_access(authorization)
    svc = _services()
    cubes = svc.user_manager.get_user_cubes(user.user_id)
    return JSONResponse(
        status_code=200,
        content={
            "user": {
                "user_id": user.user_id,
                "user_name": user.user_name,
                "role": user.role.value if hasattr(user.role, "value") else str(user.role),
            },
            "cubes": [
                {"cube_id": c.cube_id, "cube_name": c.cube_name, "owner_id": c.owner_id}
                for c in cubes
            ],
        },
        headers={"Cache-Control": "no-store"},
    )
