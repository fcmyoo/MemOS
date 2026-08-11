"""
API Key Authentication Middleware for MemOS.

Validates API keys and extracts user context for downstream handlers.
Keys are validated against SHA-256 hashes stored in PostgreSQL.
"""

import hashlib
import hmac
import os

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

import memos.log

from memos.context.context import set_current_user_name
from memos.mem_user.user_manager import UserRole


logger = memos.log.get_logger(__name__)


class AuthContext(TypedDict, total=False):
    """Authenticated request identity published by verify_api_key()."""

    user_name: str
    scopes: list[str]
    is_master_key: bool
    auth_bypassed: bool
    is_internal: bool
    api_key_id: str


def _publish_authenticated_user(request: Request, auth: AuthContext) -> AuthContext:
    request.state.auth = auth
    request.state.user = auth["user_name"]
    set_current_user_name(auth["user_name"])
    return auth

# API key header configuration
API_KEY_HEADER = APIKeyHeader(name="Authorization", auto_error=False)

# Load .env before the environment-derived constants below: the server entry
# point imports verify_api_key at module import time, so relying on a later
# load_dotenv() would leave AUTH_ENABLED / MASTER_KEY_HASH /
# INTERNAL_SERVICE_SECRET stale. Process environment variables still take
# precedence over .env values.
load_dotenv()

# Environment configuration
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
MASTER_KEY_HASH = os.getenv("MASTER_KEY_HASH")  # SHA-256 hash of master key
INTERNAL_SERVICE_SECRET = os.getenv("INTERNAL_SERVICE_SECRET")
INTERNAL_SERVICE_IPS = {"127.0.0.1", "::1", "memos-mcp", "moltbot", "clawdbot"}

# Connection pool for auth queries (lazy init)
_auth_pool = None


def _get_auth_pool():
    """Get or create auth database connection pool."""
    global _auth_pool
    if _auth_pool is not None:
        return _auth_pool

    try:
        import psycopg2.pool

        _auth_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "memos"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            dbname=os.getenv("POSTGRES_DB", "memos"),
            connect_timeout=10,
        )
        logger.info("Auth database pool initialized")
        return _auth_pool
    except Exception as e:
        logger.error("Failed to initialize auth pool: %s", e)
        return None


def hash_api_key(key: str) -> str:
    """Hash an API key using SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def validate_key_format(key: str) -> bool:
    """Validate API key format: krlk_<64-hex>."""
    if not key or not key.startswith("krlk_"):
        return False
    hex_part = key[5:]  # Remove 'krlk_' prefix
    if len(hex_part) != 64:
        return False
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False


def get_key_prefix(key: str) -> str:
    """Extract prefix for key identification (first 12 chars)."""
    return key[:12] if len(key) >= 12 else key


async def lookup_api_key(key_hash: str) -> dict[str, Any] | None:
    """
    Look up API key in database.

    Returns dict with user_name, scopes, etc. or None if not found.
    """
    pool = _get_auth_pool()
    if not pool:
        logger.warning("Auth pool not available, cannot validate key")
        return None

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_name, scopes, expires_at, is_active
                FROM api_keys
                WHERE key_hash = %s
                """,
                (key_hash,),
            )
            row = cur.fetchone()

            if not row:
                return None

            key_id, user_name, scopes, expires_at, is_active = row

            # Check if key is active
            if not is_active:
                logger.warning("Inactive API key used: %s...", key_hash[:16])
                return None

            # Check expiration (PostgreSQL TIMESTAMPTZ yields aware datetimes)
            if expires_at and expires_at <= datetime.now(UTC):
                logger.warning("Expired API key used: %s...", key_hash[:16])
                return None

            # Update last_used_at
            cur.execute(
                "UPDATE api_keys SET last_used_at = NOW() WHERE id = %s",
                (key_id,),
            )
            conn.commit()

            return {
                "id": str(key_id),
                "user_name": user_name,
                "scopes": scopes or ["read"],
            }
    except Exception as e:
        logger.error("Database error during key lookup: %s", e)
        return None
    finally:
        if conn and pool:
            pool.putconn(conn)


def is_internal_request(request: Request) -> bool:
    """Check if request is from internal service."""
    client_host = request.client.host if request.client else None

    # Check internal IPs
    if client_host in INTERNAL_SERVICE_IPS:
        return True

    # Check internal header (for container-to-container). Both the configured
    # secret and the presented header must be non-empty, so an unset secret
    # can never match a missing header; compare in constant time.
    internal_header = request.headers.get("X-Internal-Service")
    return bool(INTERNAL_SERVICE_SECRET and internal_header) and hmac.compare_digest(
        internal_header,
        INTERNAL_SERVICE_SECRET,
    )


async def verify_api_key(
    request: Request,
    api_key: str | None = Security(API_KEY_HEADER),
) -> AuthContext:
    """
    Verify API key and return user context.

    This is the main dependency for protected endpoints.

    Returns:
        dict with user_name, scopes, and is_master_key flag

    Raises:
        HTTPException 401 if authentication fails
    """
    # Skip auth if disabled
    if not AUTH_ENABLED:
        return _publish_authenticated_user(
            request,
            {
                "user_name": request.headers.get("X-User-Name", "default"),
                "scopes": ["all"],
                "is_master_key": False,
                "auth_bypassed": True,
            },
        )

    # Allow internal services
    if is_internal_request(request):
        logger.debug(
            "Internal request from %s",
            request.client.host if request.client else "unknown",
        )
        return _publish_authenticated_user(
            request,
            {
                "user_name": "internal",
                "scopes": ["all"],
                "is_master_key": False,
                "is_internal": True,
            },
        )

    # Require API key
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Handle "Bearer" or "Token" prefix
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:]
    elif api_key.lower().startswith("token "):
        api_key = api_key[6:]

    # Check against master key first (has different format: mk_*)
    key_hash = hash_api_key(api_key)
    if MASTER_KEY_HASH and key_hash == MASTER_KEY_HASH:
        logger.info("Master key authentication")
        return _publish_authenticated_user(
            request,
            {
                "user_name": "admin",
                "scopes": ["all"],
                "is_master_key": True,
            },
        )

    # Validate format for regular API keys (krlk_*)
    if not validate_key_format(api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key format",
        )

    # Look up in database
    key_data = await lookup_api_key(key_hash)
    if not key_data:
        logger.warning("Invalid API key attempt: %s...", get_key_prefix(api_key))
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key",
        )

    logger.debug("Authenticated user: %s", key_data["user_name"])
    return _publish_authenticated_user(
        request,
        {
            "user_name": key_data["user_name"],
            "scopes": key_data["scopes"],
            "is_master_key": False,
            "api_key_id": key_data["id"],
        },
    )


async def get_current_user(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),  # noqa: B008
) -> AuthContext:
    """Dependency exposing the authenticated identity to endpoints.

    Re-publishes so app.dependency_overrides[verify_api_key] has identical
    side effects (request.state + request context) as the real dependency.
    """
    return _publish_authenticated_user(request, auth)


def require_scope(required_scope: str):
    """
    Dependency factory to require a specific scope.

    Usage:
        @router.post("/admin/keys", dependencies=[Depends(require_scope("admin"))])
    """

    async def scope_checker(
        auth: dict[str, Any] = Depends(verify_api_key),  # noqa: B008
    ) -> dict[str, Any]:
        scopes = auth.get("scopes", [])

        # "all" scope grants everything
        if "all" in scopes or required_scope in scopes:
            return auth

        raise HTTPException(
            status_code=403,
            detail=f"Insufficient permissions. Required scope: {required_scope}",
        )

    return scope_checker


# Convenience dependencies
require_read = require_scope("read")
require_write = require_scope("write")
require_admin = require_scope("admin")


# ---------------------------------------------------------------------------
# Web console principal (design: docs/plans/web-console-session-design.md)
#
# Independent of verify_api_key(): a Web session token (``wca_``) never
# touches the API-key path, never populates AuthContext, and never becomes
# ``is_master_key``. The resolved identity is written to
# ``request.state.web_principal`` for console routers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebPrincipal:
    """Authenticated console identity (session bearer or legacy admin key).

    ``source`` records how the identity was established: ``session`` for
    ``wca_`` Web sessions, ``api_key`` for admin-scope ``krlk_`` keys mapped
    to a ROOT/ADMIN user, ``master_key``, ``internal`` or ``auth_bypassed``
    for the legacy non-session paths that keep their historical reach.
    """

    user_id: str | None
    user_name: str
    role: UserRole
    is_master_key: bool = False
    source: str = "session"


def extract_bearer_token(request: Request) -> str | None:
    """Return the raw ``Authorization: Bearer`` payload, or None."""
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        token = header[len("Bearer ") :].strip()
        return token or None
    return None


async def verify_web_access_token(request: Request) -> WebPrincipal:
    """Resolve a ``wca_`` session bearer token into a :class:`WebPrincipal`.

    Raises:
        HTTPException 401 for missing/malformed/expired/revoked tokens or a
        disabled account, 503 when the auth services are not configured.
    """
    from memos.api.web_auth import ACCESS_TOKEN_PREFIX, WebAuthError

    token = extract_bearer_token(request)
    if not token or not token.startswith(ACCESS_TOKEN_PREFIX):
        raise HTTPException(
            status_code=401,
            detail="access_token_invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        # Lazy import: auth_router does not import this module, but keeping
        # the router out of middleware import time avoids any future cycle.
        from memos.api.routers.auth_router import get_services

        services = get_services()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="web_auth_not_configured") from None

    try:
        record = services.session_service.validate_access(token)
    except WebAuthError as exc:
        raise HTTPException(
            status_code=401,
            detail=exc.code,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = services.user_manager.get_user(record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="session_revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    principal = WebPrincipal(
        user_id=user.user_id,
        user_name=user.user_name,
        role=role,
        is_master_key=False,
        source="session",
    )
    request.state.web_principal = principal
    request.state.user = user.user_name
    return principal
