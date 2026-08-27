"""User self-service routes: /me/profile and /me/keys.

Every endpoint resolves the caller through ``verify_web_access_token`` (a
``wca_`` session bearer) and operates strictly on the caller's own resources:
the owner is always taken from the authenticated principal, never from the
request body, and cross-user operations collapse to a uniform 403 so key IDs
cannot be enumerated.
"""

from __future__ import annotations

import json
import os

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from memos.api.middleware.auth import WebPrincipal, verify_web_access_token
from memos.api.product_models import APISearchRequest, SearchResponse, TaskSummary
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


class FeedbackTaskActionRequest(BaseModel):
    """Body for approve/ignore/delete on a feedback task; reason is mandatory for audit."""

    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped


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
    memory_layer: str | None = Query(default=None, description="Filter by memory layer"),
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")

    _ALL_MEMORY_TYPES = [
        "WorkingMemory",
        "LongTermMemory",
        "UserMemory",
        "OuterMemory",
        "SkillMemory",
    ]
    text_memory_type = _ALL_MEMORY_TYPES
    if memory_type:
        if memory_type not in _ALL_MEMORY_TYPES:
            raise HTTPException(status_code=422, detail="invalid_memory_type")
        text_memory_type = [memory_type]

    _ALL_MEMORY_LAYERS = ["L1", "L2", "L3", "Skill"]
    if memory_layer is not None and memory_layer not in [*_ALL_MEMORY_LAYERS, "unclassified"]:
        raise HTTPException(status_code=422, detail="invalid_memory_layer")

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
    graph_store = naive_mem_cube.text_mem.graph_store

    if memory_layer is None:
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
    elif memory_layer in _ALL_MEMORY_LAYERS:
        # memory_layer is stored as a flat Neo4j node property (see
        # Neo4jGraphDB._build_filter_conditions_cypher), so an equality
        # filter can be pushed down into the export_graph Cypher query and
        # combined with native SKIP/LIMIT pagination. This avoids exporting
        # the full node set (and its embeddings) just to filter in Python.
        # Bypasses the TreeTextMemory.get_all wrapper (which doesn't expose
        # `status`) to call export_graph directly: `status=["activated"]`
        # keeps this total aligned with the per-layer "activated" bucket
        # /me/sync-status reports, instead of also counting
        # archived/resolving nodes (see docs/plans/.../memory-layer-p1-final-codex.md P1-1).
        text_memories_info = graph_store.export_graph(
            user_name=cube_id,
            page=page,
            page_size=page_size,
            memory_type=text_memory_type,
            status=["activated"],
            filter={"and": [{"memory_layer": memory_layer}]},
        )
        nodes = text_memories_info["nodes"] if isinstance(text_memories_info, dict) else []
        total = (
            text_memories_info.get("total_nodes", len(nodes))
            if isinstance(text_memories_info, dict)
            else len(nodes)
        )
    else:
        # memory_layer == "unclassified": "missing or not one of
        # {L1,L2,L3,Skill}" is pushed down via the filter DSL's `not_in`
        # operator (Neo4jGraphDB._build_filter_conditions_cypher), which
        # treats a NULL memory_layer as satisfying "not in the list" too.
        # This lets export_graph do the filtering AND native SKIP/LIMIT
        # pagination in one Cypher query, instead of exporting every node
        # (and its embedding) to filter in Python. `status=["activated"]`
        # matches the L1/L2/L3/Skill branch above, so this total stays
        # aligned with the "activated" bucket /me/sync-status reports.
        text_memories_info = graph_store.export_graph(
            user_name=cube_id,
            page=page,
            page_size=page_size,
            memory_type=text_memory_type,
            status=["activated"],
            filter={"and": [{"memory_layer": {"not_in": _ALL_MEMORY_LAYERS}}]},
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
    stats_available = True
    stats_error: str | None = None
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
    except Exception as exc:  # noqa: BLE001 - stats are best-effort
        logger.warning("memory type stats unavailable: %s", exc_info=True)
        # Degrade to an explicit "stats unavailable" signal instead of a
        # silent 200 + all-zero counts: the frontend can then show "—" /
        # "统计不可用" rather than a misleading zero.
        stats_available = False
        stats_error = str(exc)

    enriched_nodes = [_enrich_source_metadata(node) for node in nodes]
    return {
        "memories": _strip_embeddings(enriched_nodes),
        "total": total,
        "page": page,
        "page_size": page_size,
        "cube_id": cube_id,
        "stats": {"by_type": type_counts},
        "stats_available": stats_available,
        "stats_error": stats_error,
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
        "limit": _get_memory_limit(),
        "usage": total,
        "usage_percent": round((total / _get_memory_limit()) * 100, 1) if _get_memory_limit() > 0 else 0.0,
    }


def _get_memory_limit() -> int:
    """Get memory quota limit from environment variables.

    Uses MOS_LONGTERM_MEMORY or MOS_USER_MEMORY if set, otherwise defaults to 5000.

    Environment variables:
        MOS_LONGTERM_MEMORY: Long-term memory limit (falls back to this if set)
        MOS_USER_MEMORY: User memory limit (checked first if set)

    Returns:
        Memory limit in number of memories (default: 5000)
    """
    return int(
        os.getenv("MOS_USER_MEMORY", os.getenv("MOS_LONGTERM_MEMORY", "5000"))
    )


# ---------------------------------------------------------------------------
# GET /me/sync-status — per-layer memory counts for the sync status panel.
#
# The one-way sync pipeline's own cursor/backlog/dead-letter state lives in
# ``sync_state_layers.json`` on the *host* running the sync service, not
# inside this container, so it cannot be read from here. This endpoint only
# reports what MemOS itself can see: memory counts grouped by the
# ``memory_layer`` property (currently unset on existing data, so everything
# lands in the ``unclassified`` bucket — that's expected, not an error).
# ---------------------------------------------------------------------------

_SYNC_STATUS_LAYERS = ["L1", "L2", "L3", "Skill"]


def _query_layer_last_updated(graph_store, cube_id: str | None) -> dict[str | None, str | None]:
    """Return the max ``updated_at`` per ``memory_layer``.

    ``get_grouped_counts`` only supports ``COUNT``, so this mirrors its
    user_name/multi_db handling directly for a ``MAX`` aggregate instead.
    """
    where_parts = ["n.status <> 'deleted'"]
    params: dict[str, Any] = {}
    if not graph_store.config.use_multi_db and (graph_store.config.user_name or cube_id):
        where_parts.append("n.user_name = $user_name")
        params["user_name"] = cube_id if cube_id else graph_store.config.user_name

    query = f"""
    MATCH (n:Memory)
    WHERE {" AND ".join(where_parts)}
    RETURN n.memory_layer AS memory_layer, MAX(n.updated_at) AS last_updated
    """
    with graph_store.driver.session(database=graph_store.db_name) as session:
        result = session.run(query, params)
        rows: dict[str | None, str | None] = {}
        for record in result:
            last_updated = record["last_updated"]
            rows[record["memory_layer"]] = (
                last_updated.isoformat() if hasattr(last_updated, "isoformat") else last_updated
            )
        return rows


@router.get("/sync-status")
def get_my_sync_status(principal: WebPrincipal = Depends(verify_web_access_token)) -> dict:
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")

    cubes = svc.user_manager.get_user_cubes(user.user_id)
    if not cubes:
        return {"layers": [], "total": 0}

    cube_id = cubes[0].cube_id

    from memos.api.routers import server_router

    graph_store = server_router.naive_mem_cube.text_mem.graph_store

    buckets: dict[str, dict[str, Any]] = {
        layer: {"count": 0, "activated": 0, "archived": 0, "last_updated": None}
        for layer in [*_SYNC_STATUS_LAYERS, "unclassified"]
    }

    # If the grouped-counts query fails, the buckets above stay all-zero.
    # Returning that as a plain 200 would make the frontend show "0 memories"
    # instead of "stats unavailable" -- so surface the failure explicitly via
    # `layers_available` rather than swallowing it.
    layers_available = True
    layers_error: str | None = None
    try:
        rows = graph_store.get_grouped_counts(
            group_fields=["memory_layer", "status"],
            where_clause="WHERE n.status <> 'deleted'",
            user_name=cube_id,
        )
        for row in rows or []:
            raw_layer = row.get("memory_layer")
            layer = raw_layer if raw_layer in _SYNC_STATUS_LAYERS else "unclassified"
            count = int(row.get("count", 0))
            bucket = buckets[layer]
            bucket["count"] += count
            if row.get("status") == "activated":
                bucket["activated"] += count
            elif row.get("status") == "archived":
                bucket["archived"] += count
    except Exception as exc:  # noqa: BLE001 - stats are best-effort
        logger.warning("sync-status layer counts unavailable", exc_info=True)
        layers_available = False
        layers_error = str(exc)

    try:
        for raw_layer, last_updated in _query_layer_last_updated(graph_store, cube_id).items():
            if not last_updated:
                continue
            layer = raw_layer if raw_layer in _SYNC_STATUS_LAYERS else "unclassified"
            bucket = buckets[layer]
            if bucket["last_updated"] is None or last_updated > bucket["last_updated"]:
                bucket["last_updated"] = last_updated
    except Exception:  # noqa: BLE001 - stats are best-effort
        logger.warning("sync-status last-updated unavailable", exc_info=True)

    layers = [{"layer": layer, **buckets[layer]} for layer in [*_SYNC_STATUS_LAYERS, "unclassified"]]
    return {
        "layers": layers,
        "total": sum(b["count"] for b in buckets.values()),
        "layers_available": layers_available,
        "layers_error": layers_error,
    }


# ---------------------------------------------------------------------------
# GET /me/search — semantic search with web session auth.
#
# Reuses SearchHandler from server_router (via lazy import) to perform
# semantic search with the same backend as /product/search, but gated
# behind verify_web_access_token for console session-based access.
# ---------------------------------------------------------------------------
@router.get("/search")
def search_my_memories(
    query: str = Query(..., description="Search query"),
    mode: str = Query(default="fast", description="Search mode: fast, fine, or mixture"),
    top_k: int = Query(default=10, ge=1, le=50, description="Number of results to return"),
    memory_type: str | None = Query(default=None, description="Filter by memory type"),
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    """Semantic search over user's memories with session-based authentication.

    This endpoint provides the same semantic search capabilities as /product/search
    but uses web session authentication (wca_ bearer token) instead of API keys,
    making it suitable for use by the web console.

    Args:
        query: Search query text (required)
        mode: Search mode - fast (embedding-based), fine (LLM-enhanced), or mixture
        top_k: Number of results to return (1-50)
        memory_type: Optional filter for specific memory type
        principal: Authenticated user from session token

    Returns:
        Dict with total count and items list containing id, memory, and metadata
    """
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")

    cubes = svc.user_manager.get_user_cubes(user.user_id)
    if not cubes:
        return {"total": 0, "items": []}

    cube_id = cubes[0].cube_id

    # Lazy import to avoid heavy components at module load time
    from memos.api.handlers.search_handler import SearchHandler
    from memos.api.routers import server_router

    # Reuse the existing SearchHandler instance
    search_handler: SearchHandler = server_router.search_handler

    # Build search request using same model as /product/search
    search_req = APISearchRequest(
        query=query,
        user_id=user.user_id,
        readable_cube_ids=[cube_id],
        mode=mode,
        top_k=top_k,
        memory_type=memory_type,
        # Default dedup for console search; can be configured if needed
        dedup="sim",
    )

    try:
        # Call the handler directly (same backend as /product/search)
        result = search_handler.handle_search_memories(search_req)
        data = result.data or {}

        # Extract text_mem results and format to match /me/memories response.
        # text_mem is a list of per-cube buckets: [{cube_id, memories: [...], total_nodes}]
        text_memories = data.get("text_mem", [])
        if not isinstance(text_memories, list):
            text_memories = []

        # Transform items: keep only id, memory, metadata (strip embeddings)
        items = []
        for bucket in text_memories:
            if not isinstance(bucket, dict):
                continue
            for mem in bucket.get("memories", []):
                if not isinstance(mem, dict):
                    continue
                metadata = mem.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}

                # Enrich with agent_id/source_type like /me/memories does
                enriched_meta = dict(metadata)

                # Fill agent_id from session_id if present
                session_id = enriched_meta.get("session_id")
                if isinstance(session_id, str) and ":" in session_id:
                    prefix = session_id.split(":", 1)[0]
                    if prefix:
                        enriched_meta["agent_id"] = prefix

                # Fill source_type from sources if present
                sources = enriched_meta.get("sources")
                if isinstance(sources, list) and len(sources) > 0:
                    first_source = sources[0]
                    if isinstance(first_source, dict):
                        source_type = first_source.get("type")
                        if source_type:
                            enriched_meta["source_type"] = source_type

                # Remove embedding from metadata
                enriched_meta.pop("embedding", None)

                items.append({
                    "id": mem.get("id", ""),
                    "memory": mem.get("memory", ""),
                    "metadata": enriched_meta,
                })

        # Extract total from buckets' total_nodes or count items
        total = sum(
            int(bucket.get("total_nodes", 0))
            for bucket in text_memories
            if isinstance(bucket, dict)
        )
        if total <= 0:
            total = len(items)

        return {
            "total": total,
            "items": items,
        }
    except Exception as e:
        logger.warning("Search failed: %s", exc_info=True)
        raise HTTPException(status_code=500, detail="search_failed") from e


# ---------------------------------------------------------------------------
# /me/feedback/tasks — approval queue for sync-service feedback nodes.
#
# The sync service marks feedback nodes with a top-level ``memmy_sync``
# property. ``_flatten_info_fields`` (graph_dbs/neo4j.py) only flattens one
# level of ``metadata.info``, so a nested envelope such as
# ``info.sync = {"kind": "feedback", ...}`` lands as a *single* top-level
# property whose value is JSON-encoded by ``_sanitize_neo4j_value`` — there is
# no dotted ``info.sync.kind`` property in Neo4j. We read/write that one
# top-level string property (``memmy_sync``) and (de)serialize it as JSON
# here, matching the envelope shape referenced in the sync design doc:
# ``{"kind": "feedback", "feedback_id": ..., "stable_key": ..., "target_layer":
# ..., "requires_approval": ...}``. ``task_status`` (pending/approved/ignored/
# deleted) and ``audit_log`` (JSON-encoded list of {op, reason, by, at}) are
# separate top-level properties this router owns, since the sync envelope
# itself carries no approval-state field.
# ---------------------------------------------------------------------------


def _get_feedback_graph_store():
    from memos.api.routers import server_router

    return server_router.naive_mem_cube.text_mem.graph_store


def _parse_memmy_sync(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _parse_audit_log(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _feedback_task_summary(node: dict[str, Any]) -> dict[str, Any] | None:
    """Build the list-view shape for a feedback node, or None if not a feedback task."""
    metadata = node.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    envelope = _parse_memmy_sync(metadata.get("memmy_sync"))
    if envelope is None or envelope.get("kind") != "feedback":
        return None
    return {
        "feedback_id": envelope.get("feedback_id", node.get("id")),
        "stable_key": envelope.get("stable_key"),
        "target_layer": envelope.get("target_layer"),
        "reason": metadata.get("feedback_reason") or envelope.get("reason"),
        "received_at": metadata.get("created_at"),
        "status": metadata.get("task_status", "pending"),
    }


def _list_feedback_node_ids(graph_store, cube_id: str | None) -> list[str]:
    """Return IDs of nodes carrying a feedback ``memmy_sync`` envelope.

    ``memmy_sync`` is an opaque JSON string property (see module note above),
    so an exact-match Cypher filter can't target ``kind``/``feedback_id``
    directly; narrow with a substring ``CONTAINS`` on the serialized JSON,
    then verify the parsed envelope in Python.

    性能防护（重要）：先用 ``memmy_sync IS NOT NULL`` 的索引计数快速短路。
    当前库通常没有任何 feedback 节点，避免全表 CONTAINS 扫描 + 逐节点
    拉向量拖垮引擎（曾导致容器 OOM 崩溃）。
    """
    try:
        counts = graph_store.get_grouped_counts(
            group_fields=["status"],
            where_clause="n.memmy_sync IS NOT NULL",
            user_name=cube_id,
        )
        total = sum(int(row.get("count", 0)) for row in counts or [])
    except Exception:  # noqa: BLE001 - 计数失败不阻塞，退化为原路径
        total = 0
    if total == 0:
        return []
    return graph_store.get_by_metadata(
        filters=[
            {"field": "memmy_sync", "op": "starts_with", "value": '{"kind"'},
            {"field": "status", "op": "=", "value": "activated"},
        ],
        user_name=cube_id,
    )


def _find_feedback_node(graph_store, cube_id: str | None, feedback_id: str) -> dict[str, Any] | None:
    """Locate the feedback node whose envelope carries the given feedback_id."""
    try:
        ids = _list_feedback_node_ids(graph_store, cube_id)
    except Exception:  # noqa: BLE001
        logger.warning("feedback node lookup unavailable", exc_info=True)
        return None
    if not ids:
        return None
    for node in graph_store.get_nodes(ids, user_name=cube_id):
        metadata = node.get("metadata", {})
        envelope = _parse_memmy_sync(metadata.get("memmy_sync") if isinstance(metadata, dict) else None)
        if envelope and envelope.get("kind") == "feedback" and envelope.get("feedback_id") == feedback_id:
            return node
    return None


def _require_feedback_node(graph_store, cube_id: str | None, feedback_id: str) -> dict[str, Any]:
    node = _find_feedback_node(graph_store, cube_id, feedback_id)
    if node is None:
        raise HTTPException(status_code=404, detail="feedback_task_not_found")
    return node


def _append_feedback_audit(
    graph_store,
    cube_id: str | None,
    node: dict[str, Any],
    *,
    op: str,
    reason: str,
    by: str,
    new_status: str,
) -> None:
    metadata = node.get("metadata", {}) if isinstance(node.get("metadata"), dict) else {}
    audit_log = _parse_audit_log(metadata.get("audit_log"))
    audit_log.append(
        {"op": op, "reason": reason, "by": by, "at": datetime.utcnow().isoformat()}
    )
    graph_store.update_node(
        node["id"],
        {"task_status": new_status, "audit_log": json.dumps(audit_log, ensure_ascii=False)},
        user_name=cube_id,
    )


def _resolve_feedback_cube(principal: WebPrincipal):
    svc = _services()
    user = svc.user_manager.get_user(principal.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")
    cubes = svc.user_manager.get_user_cubes(user.user_id)
    cube_id = cubes[0].cube_id if cubes else None
    return user, cube_id


@router.get("/feedback/tasks")
def list_my_feedback_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    _user, cube_id = _resolve_feedback_cube(principal)
    if cube_id is None:
        return {"items": [], "total": 0}

    graph_store = _get_feedback_graph_store()
    try:
        ids = _list_feedback_node_ids(graph_store, cube_id)
        nodes = graph_store.get_nodes(ids, user_name=cube_id) if ids else []
    except Exception:  # noqa: BLE001
        logger.warning("feedback task listing unavailable", exc_info=True)
        return {"items": [], "total": 0}

    tasks = [t for t in (_feedback_task_summary(n) for n in nodes) if t is not None]
    pending = [t for t in tasks if t["status"] == "pending"]
    pending.sort(key=lambda t: t.get("received_at") or "", reverse=True)

    start = (page - 1) * page_size
    page_items = pending[start : start + page_size]
    return {"items": page_items, "total": len(pending)}


@router.get("/feedback/tasks/{feedback_id}")
def get_my_feedback_task(
    feedback_id: str,
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    _user, cube_id = _resolve_feedback_cube(principal)
    if cube_id is None:
        raise HTTPException(status_code=404, detail="feedback_task_not_found")

    graph_store = _get_feedback_graph_store()
    node = _require_feedback_node(graph_store, cube_id, feedback_id)
    metadata = node.get("metadata", {}) if isinstance(node.get("metadata"), dict) else {}
    envelope = _parse_memmy_sync(metadata.get("memmy_sync")) or {}

    return {
        "feedback_id": envelope.get("feedback_id", node.get("id")),
        "stable_key": envelope.get("stable_key"),
        "target_layer": envelope.get("target_layer"),
        "requires_approval": envelope.get("requires_approval"),
        "reason": metadata.get("feedback_reason") or envelope.get("reason"),
        "received_at": metadata.get("created_at"),
        "status": metadata.get("task_status", "pending"),
        "payload_summary": {
            "memory": (node.get("memory") or "")[:500],
            "memory_type": metadata.get("memory_type"),
            "confidence": metadata.get("confidence"),
        },
        "audit_log": _parse_audit_log(metadata.get("audit_log")),
    }


def _run_feedback_task_action(
    feedback_id: str,
    body: FeedbackTaskActionRequest,
    principal: WebPrincipal,
    *,
    op: str,
    new_status: str,
) -> dict:
    _user, cube_id = _resolve_feedback_cube(principal)
    if cube_id is None:
        raise HTTPException(status_code=404, detail="feedback_task_not_found")

    graph_store = _get_feedback_graph_store()
    node = _require_feedback_node(graph_store, cube_id, feedback_id)
    _append_feedback_audit(
        graph_store,
        cube_id,
        node,
        op=op,
        reason=body.reason,
        by=principal.user_name,
        new_status=new_status,
    )
    logger.info(
        "feedback task %s %s by %s: %s", feedback_id, op, principal.user_name, body.reason
    )
    return {"feedback_id": feedback_id, "status": new_status}


@router.post("/feedback/tasks/{feedback_id}/approve")
def approve_my_feedback_task(
    feedback_id: str,
    body: FeedbackTaskActionRequest,
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    return _run_feedback_task_action(
        feedback_id, body, principal, op="approve", new_status="approved"
    )


@router.post("/feedback/tasks/{feedback_id}/ignore")
def ignore_my_feedback_task(
    feedback_id: str,
    body: FeedbackTaskActionRequest,
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    return _run_feedback_task_action(
        feedback_id, body, principal, op="ignore", new_status="ignored"
    )


@router.post("/feedback/tasks/{feedback_id}/delete")
def delete_my_feedback_task(
    feedback_id: str,
    body: FeedbackTaskActionRequest,
    principal: WebPrincipal = Depends(verify_web_access_token),
) -> dict:
    return _run_feedback_task_action(
        feedback_id, body, principal, op="delete", new_status="deleted"
    )
