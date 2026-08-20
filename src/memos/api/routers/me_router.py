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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from memos.api.middleware.auth import WebPrincipal, verify_web_access_token
from memos.api.product_models import TaskSummary
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


# ---------------------------------------------------------------------------
# GET /me/memories — the caller's own memories, flattened for the console.
#
# Web sessions carry a ``wca_`` bearer that resolves to a WebPrincipal; the
# product memory handlers, by contrast, are gated behind the API-key
# ``verify_api_key`` / ``CubeAccessControl`` path.  This endpoint bridges the
# two: it resolves the caller's *own* default cube from the principal (never
# from the request body) and reads that cube's text memories directly through
# the shared ``naive_mem_cube`` component, then strips embedding vectors so
# the console never receives a 1024-dim payload it cannot use.
# ---------------------------------------------------------------------------


def _strip_embeddings(memories: list[dict]) -> list[dict]:
    """Return memory nodes with ``metadata.embedding`` removed recursively."""
    cleaned: list[dict] = []
    for node in memories:
        copy = dict(node)
        meta = copy.get("metadata")
        if isinstance(meta, dict):
            meta_copy = dict(meta)
            meta_copy.pop("embedding", None)
            copy["metadata"] = meta_copy
        cleaned.append(copy)
    return cleaned


def _infer_source_type(sources: list | None) -> str | None:
    """Return the source kind (e.g. ``chat``) from ``metadata.sources``."""
    if not sources:
        return None

    def _type_of(source) -> str | None:
        if isinstance(source, dict):
            return source.get("type")
        return getattr(source, "type", None)

    first = _type_of(sources[0])
    if first:
        return first

    counts: dict[str, int] = {}
    for source in sources:
        stype = _type_of(source)
        if stype:
            counts[stype] = counts.get(stype, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _enrich_source_metadata(node: dict) -> dict:
    """Fill normalized ``metadata.agent_id`` / ``metadata.source_type``.

    Sync writes stamp the source agent on the ``session_id`` colon prefix
    (``codex:codex:rollout-...`` → ``codex``) and the source kind on
    ``metadata.sources[].type``; lift both into the fields the console reads.
    Only fills these two fields — everything else is left untouched.
    """
    meta = node.get("metadata")
    if not isinstance(meta, dict):
        return node

    enriched = dict(meta)

    session_id = enriched.get("session_id")
    if isinstance(session_id, str) and ":" in session_id:
        prefix = session_id.split(":", 1)[0]
        if prefix:
            enriched["agent_id"] = prefix

    source_type = _infer_source_type(enriched.get("sources"))
    if source_type is not None:
        enriched["source_type"] = source_type

    if enriched == meta:
        return node

    copy = dict(node)
    copy["metadata"] = enriched
    return copy


@router.get("/memories")
def list_my_memories(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    memory_type: str | None = Query(default=None),
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")

    _ALL_MEMORY_TYPES = ["WorkingMemory", "LongTermMemory", "UserMemory", "OuterMemory"]
    text_memory_type = _ALL_MEMORY_TYPES
    if memory_type:
        if memory_type not in _ALL_MEMORY_TYPES:
            raise HTTPException(status_code=422, detail="invalid_memory_type")
        text_memory_type = [memory_type]

    cubes = svc.user_manager.get_user_cubes(user.user_id)
    if not cubes:
        return {"memories": [], "total": 0, "cube_id": None}

    cube_id = cubes[0].cube_id

    # Lazy import: server_router initialises the heavy components (LLM,
    # graph DB, embedder, ...) at module import.  server_api.py imports
    # server_router before me_router, so these globals are already live here
    # and a lazy import inside the endpoint keeps the router import-light.
    from memos.api.routers import server_router

    naive_mem_cube = server_router.naive_mem_cube
    text_memories_info = naive_mem_cube.text_mem.get_all(
        user_name=cube_id,
        user_id=user.user_id,
        memory_type=text_memory_type,
        page=page,
        page_size=page_size,
    )
    nodes = text_memories_info["nodes"] if isinstance(text_memories_info, dict) else []
    total = (
        text_memories_info.get("total_nodes", len(nodes))
        if isinstance(text_memories_info, dict)
        else len(nodes)
    )

    # Type distribution over ALL memories (not just the current page) so the
    # usage page's bar chart stays correct regardless of pagination.  Uses a
    # lightweight Neo4j GROUP BY (count per memory_type) instead of exporting
    # every node, so pagination stays fast. Failures degrade to empty stats
    # rather than blocking the list.
    type_counts: dict[str, int] = {}
    try:
        rows = naive_mem_cube.text_mem.graph_store.get_grouped_counts(
            group_fields=["memory_type"],
            where_clause=(
                "WHERE n.memory_type IN $memory_types AND n.status <> 'deleted'"
            ),
            params={"memory_types": text_memory_type},
            user_name=cube_id,
        )
        for row in rows or []:
            mtype = row.get("memory_type")
            if isinstance(mtype, str):
                type_counts[mtype] = int(row.get("count", 0))
    except Exception:  # noqa: BLE001 - stats are best-effort
        logger.warning("memory type stats unavailable: %s", exc_info=True)

    enriched_nodes = [_enrich_source_metadata(node) for node in nodes]
    return {
        "memories": _strip_embeddings(enriched_nodes),
        "total": total,
        "page": page,
        "page_size": page_size,
        "cube_id": cube_id,
        "stats": {"by_type": type_counts},
    }


# ---------------------------------------------------------------------------
# GET /me/scheduler — aggregated scheduler status for the overview console.
#
# Mirrors /product/scheduler/allstatus but is gated behind the web-session
# bearer (``wca_``) instead of an API key.  Scheduler state is global rather
# than per-user, so any authenticated console user may read it.  The heavy
# scheduler components live on server_router and are imported lazily, keeping
# this router import-light (same pattern as /me/memories).
# ---------------------------------------------------------------------------


@router.get("/scheduler")
def get_my_scheduler_status(principal: WebPrincipal = Depends(verify_web_access_token)) -> dict:
    from memos.api.handlers import scheduler_handler
    from memos.api.routers import server_router

    mem_scheduler = server_router.mem_scheduler
    status_tracker = server_router.status_tracker

    # Scheduler not enabled: report an all-zero summary instead of erroring.
    if mem_scheduler is None or status_tracker is None:
        empty = TaskSummary()
        return {"scheduler_summary": empty, "all_tasks_summary": empty}

    result = scheduler_handler.handle_scheduler_allstatus(
        mem_scheduler=mem_scheduler, status_tracker=status_tracker
    )
    return {
        "scheduler_summary": result.data.scheduler_summary,
        "all_tasks_summary": result.data.all_tasks_summary,
    }


# ---------------------------------------------------------------------------
# GET /me/memory-stats — aggregated memory usage for the console usage page.
#
# Reuses the cube resolution of /me/memories (principal's default cube; no
# cube → empty stats) but returns only lightweight Neo4j GROUP BY aggregates,
# so it never exports the full node set.  ``by_source`` is intentionally empty:
# sync writes don't record agent_id yet (see
# docs/plans/memory-usage-telemetry-input.md).  Each aggregate degrades to
# empty/zero on error rather than failing the whole response.
# ---------------------------------------------------------------------------


@router.get("/memory-stats")
def get_my_memory_stats(principal: WebPrincipal = Depends(verify_web_access_token)) -> dict:
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")

    empty = {
        "total": 0,
        "by_type": {},
        "by_status": {},
        "by_confidence": {"high": 0, "medium": 0, "low": 0},
        "trend_30d": [],
        "by_source": {},
    }

    cubes = svc.user_manager.get_user_cubes(user.user_id)
    if not cubes:
        return empty

    cube_id = cubes[0].cube_id

    # Lazy import (same pattern as /me/memories): server_router holds the heavy
    # graph DB components initialised at import time.
    from memos.api.routers import server_router

    graph_store = server_router.naive_mem_cube.text_mem.graph_store

    total = 0
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_confidence: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    trend_30d: list[dict] = []

    # Total + type distribution: one GROUP BY over memory_type covers both.
    # ``total`` sums every row so it stays exact even for unknown/null types.
    try:
        type_rows = graph_store.get_grouped_counts(
            group_fields=["memory_type"],
            where_clause="WHERE n.status <> 'deleted'",
            user_name=cube_id,
        )
        for row in type_rows or []:
            count = int(row.get("count", 0))
            total += count
            mtype = row.get("memory_type")
            if isinstance(mtype, str):
                by_type[mtype] = count
    except Exception:  # noqa: BLE001 - stats are best-effort
        logger.warning("memory type stats unavailable: %s", exc_info=True)

    # Status distribution (currently all 'activated', but cheap to keep generic).
    try:
        status_rows = graph_store.get_grouped_counts(
            group_fields=["status"],
            where_clause="WHERE n.status <> 'deleted'",
            user_name=cube_id,
        )
        for row in status_rows or []:
            status = row.get("status")
            if isinstance(status, str):
                by_status[status] = int(row.get("count", 0))
    except Exception:  # noqa: BLE001 - stats are best-effort
        logger.warning("memory status stats unavailable: %s", exc_info=True)

    # Confidence buckets.  ``toFloat`` tolerates legacy string values while the
    # current writer stores a 0~1 float.  Buckets: high >=0.8 / medium 0.5~0.8 / low <0.5.
    try:
        buckets = (
            ("high", "WHERE toFloat(n.confidence) >= 0.8 AND n.status <> 'deleted'"),
            (
                "medium",
                "WHERE toFloat(n.confidence) >= 0.5 AND toFloat(n.confidence) < 0.8"
                " AND n.status <> 'deleted'",
            ),
            ("low", "WHERE toFloat(n.confidence) < 0.5 AND n.status <> 'deleted'"),
        )
        for bucket, clause in buckets:
            rows = graph_store.get_grouped_counts(
                group_fields=["status"],
                where_clause=clause,
                user_name=cube_id,
            )
            by_confidence[bucket] = sum(int(row.get("count", 0)) for row in rows or [])
    except Exception:  # noqa: BLE001 - stats are best-effort
        logger.warning("memory confidence stats unavailable: %s", exc_info=True)

    # Daily write trend over the last 30 days, zero-filled so the frontend can
    # render a continuous axis even when some days have no writes.
    try:
        today = datetime.utcnow().date()
        days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
        day_counts: dict[str, int] = {}
        rows = graph_store.get_grouped_counts_by_day(user_name=cube_id, since_date=days[0])
        for row in rows or []:
            day = row.get("day")
            if isinstance(day, str):
                day_counts[day] = int(row.get("count", 0))
        trend_30d = [{"date": d, "count": day_counts.get(d, 0)} for d in days]
    except Exception:  # noqa: BLE001 - stats are best-effort
        logger.warning("memory trend stats unavailable: %s", exc_info=True)

    # Source (agent) distribution.  The sync pipeline stamps a "<source>:<sid>"
    # prefix on session_id, so we bucket by the part before the first ':'.
    # Legacy/default entries (session_id == "default_session") land in "other".
    try:
        source_rows = graph_store.get_grouped_counts(
            group_fields=["session_id"],
            where_clause="WHERE n.status <> 'deleted'",
            user_name=cube_id,
        )
        counts: dict[str, int] = {}
        for row in source_rows or []:
            sid = row.get("session_id")
            cnt = int(row.get("count", 0))
            src = str(sid).split(":", 1)[0] if isinstance(sid, str) and ":" in sid else "other"
            counts[src] = counts.get(src, 0) + cnt
        by_source = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    except Exception:  # noqa: BLE001 - stats are best-effort
        by_source = {}

    return {
        "total": total,
        "by_type": by_type,
        "by_status": by_status,
        "by_confidence": by_confidence,
        "trend_30d": trend_30d,
        "by_source": by_source,
    }
