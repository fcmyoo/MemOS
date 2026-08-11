"""
Admin Router for API Key Management and Web console user administration.

Key endpoints keep the legacy contract: master key or admin scope. The
``/users*`` and Cube endpoints use a hybrid admin principal per
docs/plans/web-console-revamp.md §4.3: ROOT/ADMIN Web sessions (``wca_``)
or legacy credentials with historical admin reach (master key, admin-scope
``krlk_`` keys mapped to a ROOT/ADMIN user, internal services, the
AUTH_ENABLED=false bypass).
"""

import os

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import memos.log

from memos.api.middleware.auth import (
    WebPrincipal,
    extract_bearer_token,
    require_scope,
    verify_api_key,
    verify_web_access_token,
)
from memos.api.routers.auth_router import (
    _validate_password,
    _validate_user_name,
    get_services as _get_web_services,
)
from memos.api.utils.api_keys import (
    create_api_key_in_db,
    generate_master_key,
    list_api_keys,
    revoke_api_key,
)
from memos.api.web_auth import ACCESS_TOKEN_PREFIX
from memos.mem_user.user_manager import UserRole


logger = memos.log.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

#: Roles allowed to enter the console admin surface.
_ADMIN_ROLES = (UserRole.ROOT, UserRole.ADMIN)


# Request/Response models
class CreateKeyRequest(BaseModel):
    user_name: str = Field(..., min_length=1, max_length=255)
    scopes: list[str] = Field(default=["read"])
    description: str | None = Field(default=None, max_length=500)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class CreateKeyResponse(BaseModel):
    message: str
    key: str  # Only returned once!
    key_prefix: str
    user_name: str
    scopes: list[str]


class KeyListResponse(BaseModel):
    message: str
    keys: list[dict[str, Any]]


class RevokeKeyRequest(BaseModel):
    key_id: str


class SimpleResponse(BaseModel):
    message: str
    success: bool = True


# --- /admin/users & Cube models (web-console-revamp.md §4.3) ----------------


class AdminUserSummary(BaseModel):
    """Contract row: never carries password material."""

    user_id: str
    user_name: str
    role: str
    is_active: bool
    created_at: str | None = None
    default_cube_id: str | None = None


class AdminUserListResponse(BaseModel):
    users: list[AdminUserSummary]
    total: int
    page: int
    limit: int


class CubeSummary(BaseModel):
    cube_id: str
    cube_name: str
    owner_id: str


class AdminUserDetailResponse(BaseModel):
    user: AdminUserSummary
    cubes: list[CubeSummary]


class AdminCubeSummary(BaseModel):
    cube_id: str
    cube_name: str
    owner_id: str
    created_at: str | None = None


class AdminCubeListResponse(BaseModel):
    cubes: list[AdminCubeSummary]
    total: int


class AdminCreateUserRequest(BaseModel):
    user_name: str = Field(..., min_length=1, max_length=255)
    password: str | None = None
    role: str | None = "USER"
    cube_ids: list[str] | None = None


class SetUserCubesRequest(BaseModel):
    cube_ids: list[str]


class SetUserCubesResponse(BaseModel):
    user_id: str
    cubes: list[CubeSummary]


def _get_db_connection():
    """Get database connection for admin operations."""
    import psycopg2

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "memos"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        dbname=os.getenv("POSTGRES_DB", "memos"),
    )


# ---------------------------------------------------------------------------
# Hybrid admin principal (session admins + legacy admin-scope keys)
# ---------------------------------------------------------------------------


def _services():
    """Web auth services shared with the auth router; 503 when unconfigured."""
    try:
        return _get_web_services()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="web_auth_not_configured") from None


def _principal_from_key_auth(auth: dict) -> WebPrincipal:
    """Map a legacy verify_api_key() context onto a console principal.

    Master keys, internal services and the AUTH_ENABLED=false bypass keep
    their historical admin reach. Regular keys additionally need the
    ``admin``/``all`` scope AND an active ROOT/ADMIN user behind their
    ``user_name`` — a plain USER holding an admin-scope key gets the same
    uniform 403 as any non-admin.
    """
    if auth.get("auth_bypassed"):
        return WebPrincipal(
            user_id=None,
            user_name=auth.get("user_name", "default"),
            role=UserRole.ROOT,
            source="auth_bypassed",
        )
    if auth.get("is_internal"):
        return WebPrincipal(
            user_id=None, user_name="internal", role=UserRole.ROOT, source="internal"
        )
    if auth.get("is_master_key"):
        return WebPrincipal(
            user_id=None,
            user_name=auth.get("user_name", "admin"),
            role=UserRole.ROOT,
            is_master_key=True,
            source="master_key",
        )

    scopes = auth.get("scopes") or []
    if "admin" not in scopes and "all" not in scopes:
        raise HTTPException(status_code=403, detail="admin_scope_required")

    user = _services().user_manager.get_user_by_name(auth.get("user_name", ""))
    if user is None or not user.is_active:
        raise HTTPException(status_code=403, detail="admin_role_required")
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="admin_role_required")
    return WebPrincipal(
        user_id=user.user_id, user_name=user.user_name, role=role, source="api_key"
    )


async def require_admin_principal(request: Request) -> WebPrincipal:
    """Hybrid dependency guarding every ``/admin/users*`` and Cube route.

    ``wca_`` session bearers resolve through ``verify_web_access_token``;
    everything else falls through to the untouched legacy ``verify_api_key``
    path. Non-admin identities receive a uniform 403 so no route reveals
    whether a user ID exists.
    """
    token = extract_bearer_token(request)
    if token is not None and token.startswith(ACCESS_TOKEN_PREFIX):
        principal = await verify_web_access_token(request)
        if principal.role not in _ADMIN_ROLES:
            raise HTTPException(status_code=403, detail="admin_role_required")
        request.state.web_principal = principal
        return principal

    auth = await verify_api_key(request, api_key=request.headers.get("Authorization"))
    principal = _principal_from_key_auth(auth)
    request.state.web_principal = principal
    return principal


def _check_target_hierarchy(principal: WebPrincipal, target) -> None:
    """ROOT manages every role; ADMIN may only touch USER/GUEST targets."""
    if principal.role == UserRole.ROOT:
        return
    target_role = target.role if isinstance(target.role, UserRole) else UserRole(target.role)
    if target_role in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="forbidden_role_hierarchy")


def _parse_role(value: str) -> UserRole:
    try:
        return UserRole(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="invalid_role") from None


def _user_summary(um, user) -> AdminUserSummary:
    return AdminUserSummary(
        user_id=user.user_id,
        user_name=user.user_name,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        is_active=bool(user.is_active),
        created_at=user.created_at.isoformat() if user.created_at else None,
        default_cube_id=um.get_default_cube_id(user.user_id),
    )


def _cube_summaries(um, user_id: str) -> list[CubeSummary]:
    return [
        CubeSummary(cube_id=cube.cube_id, cube_name=cube.cube_name, owner_id=cube.owner_id)
        for cube in um.get_user_cubes(user_id)
    ]


def _detail_response(um, user) -> AdminUserDetailResponse:
    return AdminUserDetailResponse(
        user=_user_summary(um, user), cubes=_cube_summaries(um, user.user_id)
    )


# ---------------------------------------------------------------------------
# /admin/users
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=AdminUserListResponse,
    summary="List users (admin)",
)
async def admin_list_users(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    role: str | None = None,
    is_active: bool | None = None,
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """
    Paginated user listing with optional ``role`` / ``is_active`` filters.

    Rows carry only ``user_id, user_name, role, is_active, created_at,
    default_cube_id`` — never password material.
    """
    role_filter = _parse_role(role) if role is not None else None

    um = _services().user_manager
    total, rows = um.search_users(
        role=role_filter,
        is_active=is_active,
        offset=(page - 1) * limit,
        limit=limit,
    )
    logger.info(
        "Admin user list by %s (page=%s limit=%s role=%s is_active=%s)",
        principal.user_name,
        page,
        limit,
        role,
        is_active,
    )
    return AdminUserListResponse(
        users=[AdminUserSummary(**row) for row in rows], total=total, page=page, limit=limit
    )


@router.post(
    "/users",
    response_model=AdminUserDetailResponse,
    status_code=201,
    summary="Create a user on behalf of an admin",
)
async def admin_create_user(
    request_body: AdminCreateUserRequest,
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """
    Admin-proxied user creation.

    Role defaults to USER; an initial password and ``cube_ids`` are optional.
    Password material never appears in the response.
    """
    services = _services()
    um = services.user_manager

    if _validate_user_name(request_body.user_name) is not None:
        raise HTTPException(status_code=422, detail="invalid_user_name")
    if request_body.password is not None and (
        _validate_password(request_body.password) is not None
    ):
        raise HTTPException(status_code=422, detail="invalid_password")

    role = _parse_role(request_body.role or "USER")
    if principal.role != UserRole.ROOT and role in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="forbidden_role_hierarchy")

    # Validate cube assignments before creating anything (no partial state).
    cube_ids = list(dict.fromkeys(request_body.cube_ids or []))
    for cube_id in cube_ids:
        cube = um.get_cube(cube_id)
        if cube is None or not cube.is_active:
            raise HTTPException(status_code=422, detail="cube_invalid")

    if um.get_user_by_name(request_body.user_name) is not None:
        raise HTTPException(status_code=409, detail="user_name_taken")

    user_id = um.create_user(user_name=request_body.user_name, role=role)
    if request_body.password is not None:
        um.set_user_password(user_id, services.passwords.hash_password(request_body.password))
    for cube_id in cube_ids:
        um.add_user_to_cube(user_id, cube_id)

    logger.info(
        "Admin user %s created by %s (role=%s cubes=%s)",
        user_id,
        principal.user_name,
        role.value,
        len(cube_ids),
    )
    return _detail_response(um, um.get_user(user_id))


@router.get(
    "/users/{user_id}",
    response_model=AdminUserDetailResponse,
    summary="Get a user (admin)",
)
async def admin_get_user(
    user_id: str,
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """Return one user with their accessible cubes; 404 when unknown."""
    um = _services().user_manager
    user = um.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return _detail_response(um, user)


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserSummary,
    summary="Update a user (admin)",
)
async def admin_update_user(
    user_id: str,
    body: dict[str, Any],
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """
    Partial update: ``is_active``, ``role`` (hierarchy-bound) and password
    reset. ``user_name`` is immutable. Password reset / deactivation revokes
    every Web session of the target. ROOT is never deletable and the last
    active ROOT can neither be deactivated nor demoted.
    """
    if "user_name" in body:
        raise HTTPException(status_code=422, detail="user_name_immutable")

    services = _services()
    um = services.user_manager
    target = um.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    _check_target_hierarchy(principal, target)

    new_role: UserRole | None = None
    if "role" in body:
        new_role = _parse_role(body["role"])
        if principal.role != UserRole.ROOT and new_role in _ADMIN_ROLES:
            raise HTTPException(status_code=403, detail="forbidden_role_hierarchy")

    new_active: bool | None = None
    if "is_active" in body:
        if not isinstance(body["is_active"], bool):
            raise HTTPException(status_code=422, detail="invalid_is_active")
        new_active = body["is_active"]

    new_password: str | None = None
    if "password" in body:
        if not isinstance(body["password"], str) or _validate_password(body["password"]) is not None:
            raise HTTPException(status_code=422, detail="invalid_password")
        new_password = body["password"]

    target_role = target.role if isinstance(target.role, UserRole) else UserRole(target.role)
    if target_role == UserRole.ROOT:
        leaving_root = new_active is False or (
            new_role is not None and new_role != UserRole.ROOT
        )
        if leaving_root and um.count_users(role=UserRole.ROOT, is_active=True) <= 1:
            raise HTTPException(status_code=409, detail="last_root_protected")

    if not um.update_user(user_id, role=new_role, is_active=new_active):
        raise HTTPException(status_code=404, detail="user_not_found")

    revoke_sessions = False
    if new_password is not None:
        um.set_user_password(user_id, services.passwords.hash_password(new_password))
        revoke_sessions = True
    if new_active is False:
        revoke_sessions = True
    if revoke_sessions:
        services.session_service.revoke_user_sessions(user_id)

    logger.info("Admin user %s updated by %s", user_id, principal.user_name)
    return _user_summary(um, um.get_user(user_id))


@router.delete(
    "/users/{user_id}",
    status_code=204,
    summary="Delete (soft) a user (admin)",
)
async def admin_delete_user(
    user_id: str,
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """
    Soft delete: deactivates the user and revokes every Web session.
    ROOT accounts are never deletable; ADMIN cannot delete ROOT/ADMIN.
    """
    services = _services()
    um = services.user_manager
    target = um.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user_not_found")

    target_role = target.role if isinstance(target.role, UserRole) else UserRole(target.role)
    if target_role == UserRole.ROOT:
        raise HTTPException(status_code=403, detail="root_undeletable")
    _check_target_hierarchy(principal, target)
    if not target.is_active:
        raise HTTPException(status_code=409, detail="user_already_inactive")

    if not um.delete_user(user_id):
        raise HTTPException(status_code=500, detail="delete_failed")
    services.session_service.revoke_user_sessions(user_id)

    logger.info("Admin user %s deleted by %s", user_id, principal.user_name)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Cubes
# ---------------------------------------------------------------------------


@router.get(
    "/cubes",
    response_model=AdminCubeListResponse,
    summary="List active cubes (admin)",
)
async def admin_list_cubes(
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """Active cubes for the assignment dialog."""
    cubes = _services().user_manager.list_active_cubes()
    return AdminCubeListResponse(
        cubes=[AdminCubeSummary(**cube) for cube in cubes], total=len(cubes)
    )


@router.put(
    "/users/{user_id}/cubes",
    response_model=SetUserCubesResponse,
    summary="Replace the cube set of a user (admin)",
)
async def admin_set_user_cubes(
    user_id: str,
    request_body: SetUserCubesRequest,
    principal: WebPrincipal = Depends(require_admin_principal),  # noqa: B008
):
    """
    Atomically replace the user's accessible cube set.

    Owner cubes can never be removed; unknown or inactive cube IDs are
    rejected with 422 before any membership changes.
    """
    um = _services().user_manager
    target = um.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    _check_target_hierarchy(principal, target)

    cube_ids = list(dict.fromkeys(request_body.cube_ids))
    for cube_id in cube_ids:
        cube = um.get_cube(cube_id)
        if cube is None or not cube.is_active:
            raise HTTPException(status_code=422, detail="cube_invalid")

    if not set(um.get_owned_cube_ids(user_id)).issubset(set(cube_ids)):
        raise HTTPException(status_code=422, detail="owner_cube_not_removable")

    um.set_user_cubes(user_id, cube_ids)
    logger.info(
        "Admin replaced cube set of user %s by %s (%s cubes)",
        user_id,
        principal.user_name,
        len(cube_ids),
    )
    return SetUserCubesResponse(user_id=user_id, cubes=_cube_summaries(um, user_id))


# ---------------------------------------------------------------------------
# API keys (legacy contract: master key or admin scope)
# ---------------------------------------------------------------------------


@router.post(
    "/keys",
    response_model=CreateKeyResponse,
    summary="Create a new API key",
    dependencies=[Depends(require_scope("admin"))],
)
def create_key(
    request: CreateKeyRequest,
    auth: dict = Depends(verify_api_key),  # noqa: B008
):
    """
    Create a new API key for a user.

    Requires admin scope or master key.

    **WARNING**: The API key is only returned once. Store it securely!
    """
    try:
        conn = _get_db_connection()
        try:
            api_key = create_api_key_in_db(
                conn=conn,
                user_name=request.user_name,
                scopes=request.scopes,
                description=request.description,
                expires_in_days=request.expires_in_days,
                created_by=auth.get("user_name", "unknown"),
            )

            logger.info(
                "API key created for user %s by %s",
                request.user_name,
                auth.get("user_name"),
            )

            return CreateKeyResponse(
                message="API key created successfully. Store this key securely - it won't be shown again!",
                key=api_key.key,
                key_prefix=api_key.key_prefix,
                user_name=request.user_name,
                scopes=request.scopes,
            )
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to create API key: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create API key") from e


@router.get(
    "/keys",
    response_model=KeyListResponse,
    summary="List API keys",
    dependencies=[Depends(require_scope("admin"))],
)
def list_keys(
    user_name: str | None = None,
    auth: dict = Depends(verify_api_key),  # noqa: B008
):
    """
    List all API keys (admin) or keys for a specific user.

    Note: Actual key values are never returned, only prefixes.
    """
    try:
        conn = _get_db_connection()
        try:
            keys = list_api_keys(conn, user_name=user_name)
            return KeyListResponse(
                message=f"Found {len(keys)} key(s)",
                keys=keys,
            )
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to list API keys: %s", e)
        raise HTTPException(status_code=500, detail="Failed to list API keys") from e


@router.delete(
    "/keys/{key_id}",
    response_model=SimpleResponse,
    summary="Revoke an API key",
    dependencies=[Depends(require_scope("admin"))],
)
def revoke_key(
    key_id: str,
    auth: dict = Depends(verify_api_key),  # noqa: B008
):
    """
    Revoke an API key by ID.

    The key will be deactivated but not deleted (for audit purposes).
    """
    try:
        conn = _get_db_connection()
        try:
            success = revoke_api_key(conn, key_id)
            if success:
                logger.info("API key %s revoked by %s", key_id, auth.get("user_name"))
                return SimpleResponse(message="API key revoked successfully")
            else:
                raise HTTPException(status_code=404, detail="API key not found or already revoked")
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to revoke API key: %s", e)
        raise HTTPException(status_code=500, detail="Failed to revoke API key") from e


@router.post(
    "/generate-master-key",
    response_model=dict,
    summary="Generate a new master key",
    dependencies=[Depends(require_scope("admin"))],
)
def generate_new_master_key(
    auth: dict = Depends(verify_api_key),  # noqa: B008
):
    """
    Generate a new master key.

    **WARNING**: Store the key securely! Add MASTER_KEY_HASH to your .env file.
    """
    if not auth.get("is_master_key"):
        raise HTTPException(
            status_code=403,
            detail="Only master key can generate new master keys",
        )

    key, key_hash = generate_master_key()

    logger.warning("New master key generated - update MASTER_KEY_HASH in .env")

    return {
        "message": "Master key generated. Add MASTER_KEY_HASH to your .env file!",
        "key": key,
        "key_hash": key_hash,
        "env_line": f"MASTER_KEY_HASH={key_hash}",
    }


@router.get(
    "/health",
    summary="Admin health check",
)
def admin_health():
    """Health check for admin endpoints."""
    auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"
    master_key_configured = bool(os.getenv("MASTER_KEY_HASH"))

    return {
        "status": "ok",
        "auth_enabled": auth_enabled,
        "master_key_configured": master_key_configured,
    }
