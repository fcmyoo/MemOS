"""User self-service routes: /me/profile and /me/keys.

Every endpoint resolves the caller through ``verify_web_access_token`` (a
``wca_`` session bearer) and operates strictly on the caller's own resources:
the owner is always taken from the authenticated principal, never from the
request body, and cross-user operations collapse to a uniform 403 so key IDs
cannot be enumerated.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from memos.api.middleware.auth import WebPrincipal, verify_web_access_token
from memos.api.utils.api_keys import (
    create_api_key_in_db,
    list_api_keys,
    revoke_api_key_for_user,
)
from memos.log import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


def _get_db_connection():
    """Get the API-keys database connection (same backend as /admin)."""
    import psycopg2

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "memos"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        dbname=os.getenv("POSTGRES_DB", "memos"),
    )


def _services():
    from memos.api.routers.auth_router import get_services

    try:
        return get_services()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="web_auth_not_configured") from None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)


class CreateMyKeyRequest(BaseModel):
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    description: str | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


# ---------------------------------------------------------------------------
# GET /me/profile
# ---------------------------------------------------------------------------


@router.get("/profile")
def get_my_profile(principal: WebPrincipal = Depends(verify_web_access_token)) -> dict:
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")

    cubes = svc.user_manager.get_user_cubes(user.user_id)
    default_cube = cubes[0] if cubes else None

    conn = None
    key_count = 0
    try:
        conn = _get_db_connection()
        keys = list_api_keys(conn, user_name=user.user_name)
        key_count = len(keys)
    except Exception as exc:  # noqa: BLE001 - key store may be down; profile still usable
        logger.warning("key count unavailable: %s", exc)
    finally:
        if conn is not None:
            conn.close()

    return {
        "user": {
            "user_id": user.user_id,
            "user_name": user.user_name,
            "role": user.role.value if hasattr(user.role, "value") else str(user.role),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "default_cube": (
            {"cube_id": default_cube.cube_id, "cube_name": default_cube.cube_name}
            if default_cube
            else None
        ),
        "key_count": key_count,
    }


# ---------------------------------------------------------------------------
# PATCH /me/profile — change password only
# ---------------------------------------------------------------------------


@router.patch("/profile", status_code=204)
def change_my_password(
    body: ChangePasswordRequest,
    principal: WebPrincipal = Depends(verify_web_access_token),
):
    from fastapi.responses import Response

    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")
    if user.password_hash is None or not svc.passwords.verify_password(
        user.password_hash, body.current_password
    ):
        raise HTTPException(status_code=401, detail="invalid_credentials")

    if not svc.user_manager.set_user_password(user.user_id, svc.passwords.hash_password(body.new_password)):
        raise HTTPException(status_code=500, detail="password_update_failed")
    # Changing the password revokes every session of this user.
    svc.session_service.revoke_user_sessions(user.user_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# GET /me/keys — metadata only, never the raw key
# ---------------------------------------------------------------------------


@router.get("/keys")
def list_my_keys(principal: WebPrincipal = Depends(verify_web_access_token)) -> list[dict]:
    conn = _get_db_connection()
    try:
        keys = list_api_keys(conn, user_name=principal.user_name)
    finally:
        conn.close()
    return [
        {
            "id": k.get("id"),
            "key_prefix": k.get("key_prefix"),
            "scopes": k.get("scopes"),
            "description": k.get("description"),
            "expires_at": k.get("expires_at"),
            "is_active": k.get("is_active"),
            "created_at": k.get("created_at"),
        }
        for k in keys
    ]


# ---------------------------------------------------------------------------
# POST /me/keys — owner is forced server-side
# ---------------------------------------------------------------------------


@router.post("/keys", status_code=201)
def create_my_key(
    body: CreateMyKeyRequest,
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    conn = _get_db_connection()
    try:
        api_key = create_api_key_in_db(
            conn,
            user_name=principal.user_name,  # owner always from the session
            scopes=body.scopes,
            description=body.description,
            expires_in_days=body.expires_in_days,
            created_by=principal.user_name,
        )
    finally:
        conn.close()
    # The full krlk_* value is returned exactly once, at creation.
    return {
        "id": api_key.id or api_key.key_prefix,
        "key": api_key.key,
        "key_prefix": api_key.key_prefix,
        "scopes": body.scopes,
        "description": body.description,
        "expires_at": (
            (datetime.utcnow() + timedelta(days=body.expires_in_days)).isoformat()
            if body.expires_in_days
            else None
        ),
    }


# ---------------------------------------------------------------------------
# DELETE /me/keys/{key_id} — owner-conditioned atomic revoke, uniform 403
# ---------------------------------------------------------------------------


@router.delete("/keys/{key_id}", status_code=204)
def revoke_my_key(
    key_id: str,
    principal: WebPrincipal = Depends(verify_web_access_token),
):
    from fastapi.responses import Response

    conn = _get_db_connection()
    try:
        ok = revoke_api_key_for_user(conn, key_id, principal.user_name)
    finally:
        conn.close()
    if not ok:
        # Not found / not yours / already revoked all look the same.
        raise HTTPException(status_code=403, detail="key_not_available")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
