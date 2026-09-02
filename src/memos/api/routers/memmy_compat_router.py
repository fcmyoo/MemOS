"""memmy 兼容采集 API：让 Hermes 插件的 memmy-memory 直连 MemOS（替代 memmy 服务）。

协议对齐 memmy 的 /api/v1/* 端点（插件 _memmy_post/_memmy_get 调用）。
鉴权用 API key（verify_api_key，绑定用户、长期有效——比会过期的 web token
更适合常驻插件；插件 config 的 token 填 krlk_* API key）。
写入走 add_handler.handle_add_memories（fast 直存），检索走 SearchHandler。
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from memos.api.middleware.auth import AuthContext, get_current_user

router = APIRouter(prefix="/api/v1", tags=["memmy-compat"])

# layer → memory_type 语义映射（P1 层映射语义收口，four-layer-gap-assessment.md 3.4）。
# 层由内容性质决定，而非由 API 调用方指定：
#   - L1（原始 trace）：由本 API 写入，唯一对外开放的层
#   - L2（policy 归纳）/L3（world model）：由 reorganizer 等内部管线语义聚类产出
#   - Skill（可执行技能）：由 skill evidence/trial 管线产出
# 因此本 API 只接受 L1，L2/L3/Skill 一律 422（见 memory_add 校验）。
# 注意：fast 直存路径下 SingleCubeView 实际按“消息角色”而非本映射决定 memory_type
# （single_struct.py:374-389），这里的 L1→UserMemory 仅作语义留档，不参与运行时覆盖。
_LAYER_TO_TYPE = {
    "L1": "UserMemory",
}


class MemoryAddRequest(BaseModel):
    content: str = Field(..., description="记忆内容")
    title: Optional[str] = None
    tags: Optional[list[str]] = None
    layer: Optional[str] = Field(
        default="L1",
        description="仅支持 L1（原始 trace，由插件/用户写入）。"
        "L2/L3/Skill 由 MemOS 内部管线（reorganizer/world-model/skill pipeline）产出，"
        "本 API 不接受这些层的写入请求。",
    )
    source: Optional[str] = "hermes"
    sessionId: Optional[str] = None


class MemorySearchRequest(BaseModel):
    query: str
    layers: Optional[list[str]] = None


@router.post("/memory/add")
def memory_add(
    req: MemoryAddRequest,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """写入一条记忆（fast 直存，仅接受 L1）。API key 鉴权（身份=key 绑定的用户）。

    P1 层映射语义收口（four-layer-gap-assessment.md 3.4）：L2/L3/Skill 是内部管线
    （reorganizer 语义聚类 / world-model 二级聚类 / skill evidence-trial）的产物，
    不通过对外 API 写入，一律 422 拒绝。
    """
    if req.layer not in _LAYER_TO_TYPE:
        raise HTTPException(
            status_code=422,
            detail="layer_not_allowed_for_api: L2/L3/Skill are produced by internal pipelines",
        )
    # 复用 /product/add 的 add_handler 走 fast 写入（实例在 server_router 模块级）
    from memos.api.routers import server_router

    user_name = current_user.get("user_name") or "default"
    # 通过 user_name 解析真实 user_id（API key 身份是 user_name，需映射到 user_id）
    from memos.api.routers import me_router, server_router

    svc = me_router._services()
    actor = svc.user_manager.get_user_by_name(user_name)
    if actor is None or not actor.is_active:
        raise HTTPException(status_code=401, detail="user_inactive")
    memmy_id = "memmy_compat_" + uuid.uuid4().hex[:16]
    add_req = server_router.APIADDRequest(
        user_id=actor.user_id,  # 真实 user_id → resolve_actor 校验通过（与 key 绑定用户一致）
        messages=[
            {"role": "user", "content": f"『源:{req.source}:{memmy_id}』\n{req.content}"}
        ],
        custom_tags=sorted(set((req.tags or []) + [f"layer:{req.layer}", "source:memmy-compat"])),
        info={
            "memory_layer": req.layer,
            "memmy_id": memmy_id,
            "stable_key": "memmy_compat_" + uuid.uuid4().hex,
            "source": req.source or "hermes",
        },
        async_mode="sync",
        mode="fast",  # P0-3：显式设置 fast 模式（sync + fast = 原文直存不走 LLM）
    )
    result = server_router.add_handler.handle_add_memories(add_req, current_user)
    result_str = str(result)
    # 从 handler 结果提取真实节点 id（data=[{memory, memory_id, ...}]）
    import re as _re

    real_id_m = _re.search(r"'memory_id':\s*'([0-9a-f-]{36})'", result_str)
    if not real_id_m:
        real_id_m = _re.search(r'"memory_id":\s*"([0-9a-f-]{36})"', result_str)
    return {
        "id": real_id_m.group(1) if real_id_m else memmy_id,
        "memmy_id": memmy_id,
        "layer": req.layer,
        "content": req.content,
        "fast": True,
        "result": result_str[:200],
    }


@router.post("/memory/search")
def memory_search(
    req: MemorySearchRequest,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """按 query 检索记忆（复用 /me/search 的 SearchHandler 底层逻辑）。"""
    from memos.api.handlers.search_handler import SearchHandler
    from memos.api.product_models import APISearchRequest
    from memos.api.routers import me_router, server_router

    user_name = current_user.get("user_name") or "default"
    # 通过 user_name 找用户 cube（API key 身份是 user_name）
    svc = me_router._services()
    user = svc.user_manager.get_user_by_name(user_name)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="session_revoked")
    cubes = svc.user_manager.get_user_cubes(user.user_id)
    if not cubes:
        return {"items": []}
    cube_id = cubes[0].cube_id

    search_handler: SearchHandler = server_router.search_handler
    search_req = APISearchRequest(
        query=req.query,
        user_id=user.user_id,
        readable_cube_ids=[cube_id],
        mode="fast",
        top_k=10,
        memory_type=None,
        dedup="sim",
    )
    try:
        result = search_handler.handle_search_memories(search_req, current_user)
        data = result.data or {}
        results = []
        # P0-2：适配真实返回键 text_mem（single_cube.py:109-133），保留 items/memories 兼容
        for it in data.get("text_mem", data.get("items", data.get("memories", []))):
            meta = it.get("metadata") or {}
            results.append(
                {
                    "id": it.get("id", ""),
                    "content": str(it.get("memory", ""))[:500],
                    "layer": meta.get("memory_layer", "L1"),
                    "score": it.get("score", 0),
                }
            )
        return {"items": results}
    except HTTPException:
        # P0-2：HTTPException 直接向上抛（401/403 等认证/授权异常）
        raise
    except (ValueError, KeyError, AttributeError) as exc:
        # P0-2：收窄为预期检索异常（数据结构/参数/字段访问异常），记录日志
        from memos.log import get_logger

        get_logger(__name__).warning("memmy-compat search failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="search_failed") from exc


@router.get("/memory/{memory_id}")
def memory_get(
    memory_id: str,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """按 id 取单条记忆（API key 鉴权）。P0-1：真实现，owner 校验，不存在/越权统一 404。"""
    from memos.api.routers import me_router

    user_name = current_user.get("user_name") or "default"
    # 通过 user_name 找用户 cube（API key 身份是 user_name）
    svc = me_router._services()
    user = svc.user_manager.get_user_by_name(user_name)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user_inactive")
    cubes = svc.user_manager.get_user_cubes(user.user_id)
    if not cubes:
        raise HTTPException(status_code=404, detail="memory_not_found")

    # 用 server_router 的 naive_mem_cube（模块级实例，与 /me/memories 同源）。
    # 注意：Neo4j 节点的 user_name 属性存的是 user_id（非 cube_id），
    # 传 actor.user_id 精确命中（与 /me/memories 查询口径一致）。
    from memos.api.routers import server_router

    node = server_router.naive_mem_cube.text_mem.graph_store.get_node(
        memory_id, include_embedding=False, user_name=user.user_id
    )
    if node is None:
        # 未找到或越权（不区分，统一 404）
        raise HTTPException(status_code=404, detail="memory_not_found")
    meta = node.get("metadata") or {}
    return {
        "id": node.get("id", memory_id),
        "content": str(node.get("memory", "")),
        "layer": meta.get("memory_layer", "L1"),
        "key": meta.get("stable_key", ""),
        "tags": meta.get("tags", []),
        "createdAt": meta.get("created_at", ""),
    }


class SessionOpenRequest(BaseModel):
    sessionId: Optional[str] = None
    title: Optional[str] = None


@router.post("/sessions/open")
def sessions_open(
    req: SessionOpenRequest,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """会话打开（MemOS 不强制会话模型，返回 sessionId 即可）。"""
    return {"sessionId": req.sessionId or uuid.uuid4().hex}


class TurnStartRequest(BaseModel):
    sessionId: str = "default"
    query: str = ""


@router.post("/turns/start")
def turns_start(
    req: TurnStartRequest,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """轮次开始（简化——返回 turnId）。"""
    return {"turnId": uuid.uuid4().hex, "sessionId": req.sessionId}


@router.post("/turns/{turn_id}/complete")
def turns_complete(
    turn_id: str,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """轮次完成（简化——无操作）。"""
    return {}


class SkillEvidenceRequest(BaseModel):
    world_model_id: str = Field(..., description="L3 world model 节点 ID")
    note: str = Field(..., description="正向反馈说明（用户确认该规律有效/已执行成功）")


@router.post("/skill/evidence")
def skill_evidence_add(
    req: SkillEvidenceRequest,
    current_user: AuthContext = Depends(get_current_user),
) -> dict:
    """为 L3 world model 添加正向 evidence 反馈（P3 Skill 机制，four-layer-gap-assessment.md 3.3）。

    evidence 语义：用户显式确认"这条 L3 规律有效/我已按此执行成功"，作为后续
    Skill 归纳的前置证据（只有 evidence 达标的 L3 才会被提炼为可执行 Skill）。

    行为：
    1. 按 id 查 L3 节点（复用 memory_get 的 owner 校验逻辑），校验 type=world_model + memory_layer=L3
    2. skill_evidence_count += 1（int，默认 0）
    3. skill_evidence_log 追加 {at: ISO timestamp, note: str}（JSON 字符串列表）
    4. 返回 {world_model_id, skill_evidence_count}

    鉴权：API key（current_user 绑定的用户），只能为自己的 L3 节点添加 evidence。
    """
    from datetime import datetime, timezone
    import json as _json

    from memos.api.routers import me_router, server_router

    user_name = current_user.get("user_name") or "default"
    svc = me_router._services()
    user = svc.user_manager.get_user_by_name(user_name)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user_inactive")

    # 按 id 查节点（owner 校验：传 user.user_id 作为 user_name 参数）
    node = server_router.naive_mem_cube.text_mem.graph_store.get_node(
        req.world_model_id, include_embedding=False, user_name=user.user_id
    )
    if node is None:
        raise HTTPException(status_code=404, detail="world_model_not_found")

    meta = node.get("metadata") or {}
    # 校验 type=world_model + memory_layer=L3
    if meta.get("type") != "world_model" or meta.get("memory_layer") != "L3":
        raise HTTPException(
            status_code=422,
            detail="not_world_model: evidence can only be added to L3 world_model nodes",
        )

    note_entry = {"at": datetime.now(timezone.utc).isoformat(), "note": req.note}
    try:
        new_count = server_router.naive_mem_cube.text_mem.graph_store.add_skill_evidence_atomic(
            req.world_model_id, note_entry, user_name=user.user_id
        )
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="evidence_not_supported_backend")
    if new_count is None:
        raise HTTPException(status_code=404, detail="world_model_not_found")

    return {
        "world_model_id": req.world_model_id,
        "skill_evidence_count": new_count,
    }
