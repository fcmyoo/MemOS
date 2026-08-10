# P0 Cube 访问控制修复实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 修复 API key 认证身份无法到达 handler（P0-1）以及 18 个 HTTP 端点未校验用户/Cube 归属（P0-2），保证认证身份不可由请求头或请求体伪造，未授权访问统一返回 403。

**架构：** 采用 A+B+C 组合方案。`verify_api_key` 保持现有 key 校验逻辑不变，只在每个成功分支发布认证结果到 `request.state` 和 `RequestContext`；`get_current_user` 复用 FastAPI 依赖缓存，将同一认证结果显式注入路由并传给 handler。新增无状态的 `CubeAccessControl`，以 PostgreSQL `api_keys.user_name` 解析 SQLite `users.user_name -> users.user_id`，再调用 `UserManager.validate_user_cube_access()`；业务 handler 必须在任何存储、LLM、scheduler 或 SSE 操作前完成校验。

**技术栈：** Python 3、FastAPI dependency injection、Starlette `request.state`、`ContextVar`、SQLAlchemy/SQLite `UserManager`、PostgreSQL API key 表、pytest/TestClient。

---

## 1. 范围、约束与源码漂移

### 1.1 本次会修改/新增的文件

| 文件 | 责任 |
|---|---|
| `src/memos/api/middleware/auth.py` | 定义认证结果类型；成功认证后发布 `request.state.auth/user`；提供 `get_current_user` |
| `src/memos/context/context.py` | 提供只更新当前上下文 `user_name` 的原子 helper，避免重建 trace/env/source |
| `src/memos/api/access_control.py`（新增） | 认证身份到 SQLite user 的映射、用户作用域校验、Cube 批量校验、统一 403 |
| `src/memos/api/handlers/base_handler.py` | 将同一个 `CubeAccessControl` 注入所有 class-based handler |
| `src/memos/api/routers/server_router.py` | 保留现有 25 个路由及 router 级鉴权挂载；18 个端点增加 `Depends(get_current_user)` 并向 handler 传认证结果 |
| `src/memos/api/handlers/search_handler.py` | search 的 actor 解析、fallback 和 readable Cube 校验 |
| `src/memos/api/handlers/add_handler.py` | add 的 actor 解析、fallback 和 writable Cube 校验 |
| `src/memos/api/handlers/cube_handler.py` | create/register 的 owner/user/Cube 校验；复用共享 `UserManager` |
| `src/memos/api/handlers/memory_handler.py` | get/get_all/get_by_ids/delete 的 Cube 校验；ID 模式从 memory metadata 反查 Cube |
| `src/memos/api/handlers/feedback_handler.py` | feedback 的 writable Cube 校验 |
| `src/memos/api/handlers/suggestion_handler.py` | suggestions 的 user/Cube 校验 |
| `src/memos/api/handlers/scheduler_handler.py` | 4 个 user-scoped scheduler handler 的身份一致性校验，SSE 在建流前拒绝 |
| `src/memos/api/handlers/chat_handler.py` | complete/stream 的 readable+writable Cube 并集校验，stream 在建流前拒绝 |
| `tests/api/test_auth.py` | P0-1 request.state、ContextVar、header 优先级单测 |
| `tests/api/test_cube_access_control.py`（新增） | 双库映射语义、owner/shared/deny、固定 403、AUTH disabled 单测 |
| `tests/api/test_server_cube_access.py`（新增） | 18 个 HTTP 端点的参数化越权/授权回归 |
| 现有 `tests/api/test_server_router.py`、`test_cube_endpoints.py`、`test_memory_handler_delete.py`、`test_suggestion_handler.py` | 更新 handler 新参数断言；保持 AUTH disabled 旧行为 |
| `docs/openapi.json` | API 目录被修改后按项目规则重新生成；预期 paths、method、request/response schema 无变化 |

不修改 `api_keys` 表结构、key 格式/哈希/过期/active/scopes 校验，不新增依赖，不修改 `pyproject.toml`，不改变 25 个路由的 path、HTTP method、请求/响应模型，也不移除 `server_api.py` 和 `server_api_ext.py` 上的 router 级 `Depends(verify_api_key)`。

### 1.2 审计表与当前源码的差异

实施以当前源码为准，但仍完整覆盖审计指定的 18 个逻辑端点：

- 审计写 `POST /product/delete_memories`，当前注册为 `POST /product/delete_memory`。
- 审计写 `POST /product/scheduler/task_queue_status`，当前注册为 `GET`。
- `POST /product/get_memory_by_ids` 当前请求体是裸 `list[str]`，没有 `user_id/mem_cube_id`；必须先取回 memory metadata，校验其中每个 `metadata.user_name` 对应的 Cube，再构造响应。
- add/feedback 当前分别由 `AddHandler`、`FeedbackHandler` 处理，不在 `memory_handler.py`。
- delete/recover by record id 当前直接实现在 `server_router.py` 路由函数中；本次在这两个 route handler 调用 `CubeAccessControl`，不为一次校验额外制造业务抽象。
- scheduler wait/wait stream 参数名是 `user_name`，其实际值被作为 scheduler 的 user id 使用；本次将它按 SQLite `users.user_id` 校验。

## 2. 身份与双库约定

### 2.1 唯一关联键

认证库和授权库是两个独立数据库，不做跨库 join：

```text
PostgreSQL api_keys.user_name
             │ 必须精确相等（区分大小写，不做 trim/模糊匹配）
             ▼
SQLite users.user_name ──解析──> users.user_id
                                  │
                                  ├── cubes.owner_id
                                  └── user_cube_association.user_id
```

- API key 身份来源：PostgreSQL `api_keys.user_name`，由现有 `lookup_api_key()` 返回。
- 用户/Cube 授权来源：`MEMOS_DIR/memos_users.db` 中的 `users`、`cubes`、`user_cube_association`。
- `api_keys.user_name` **必须等于** SQLite `users.user_name`，但通常不等于 `users.user_id`。代码不可把认证 `user_name` 直接传给 `validate_user_cube_access(user_id, cube_id)`。
- 普通 key 找不到同名且 active 的 SQLite 用户时，统一按未授权处理：`403 {"detail": "Insufficient cube access"}`。不得返回 404、400 或“用户不存在”，以免暴露用户/Cube 存在性。
- `is_master_key=True` 和 `is_internal=True` 视为已有全局授权：跳过 SQLite Cube 归属校验，但仍记录认证身份。它们使用请求中的目标 user/cube；这是对现有 master/internal “all” 语义的保留。普通 API key 没有该绕过。

### 2.2 请求身份优先级

| 模式 | 有效身份 | 请求体/header 处理 |
|---|---|---|
| `AUTH_ENABLED=true`，普通 key | `api_keys.user_name -> users.user_id` | `X-User-Name` 只可作为鉴权前的临时上下文，鉴权后必被覆盖；请求体/query 的 `user_id/user_name/owner_id` 若与 actor 不同，固定 403 |
| `AUTH_ENABLED=true`，master/internal | `admin/internal`，具有显式 privileged 标记 | 可指定目标 user/cube，保留管理/内部调用能力 |
| `AUTH_ENABLED=false` | 请求体/query user 优先，其次 `X-User-Name`，最后现有 `default` | `auth_bypassed=True`，不启用新 Cube ACL，保持本地开发和现有测试行为 |

`AUTH_ENABLED=false` 下跳过 ACL 是兼容边界，不是认证模式下的授权例外。handler 仍收到 `current_user`，但 `CubeAccessControl.resolve_actor()` 返回请求体/query 的 user；Cube fallback 继续使用该 user，不强制要求本地 SQLite fixture 已预建。

## 3. TDD：先写的失败测试清单

实现代码前先提交以下失败测试；每组测试先运行并确认因缺少身份注入/访问校验而失败，而不是 fixture、422 或 handler mock 错误。

### 3.1 P0-1 身份注入测试（`tests/api/test_auth.py`）

- [ ] `test_verify_api_key_injects_auth_into_request_state`：普通 key 返回后，`request.state.auth` 等于认证 dict，`request.state.user == api_keys.user_name`。
- [ ] `test_authenticated_user_overrides_spoofed_header_in_context`：请求带 `X-User-Name: victim`，lookup 返回 `alice`；鉴权后 `request.state.user` 和 `get_current_user_name()` 都是 `alice`。
- [ ] `test_get_current_user_publishes_dependency_override_result`：FastAPI 测试 app override `verify_api_key` 后，`get_current_user` 仍把 override 结果发布到 state，防止现有 router tests 因 override 绕过副作用。
- [ ] `test_auth_disabled_keeps_header_identity`：`AUTH_ENABLED=false` 时仍返回 `auth_bypassed=True`，state/context 使用 header（无 header 时为 `default`）。
- [ ] `test_master_and_internal_auth_are_published`：两个成功短路分支也写 state/context，避免仅普通 key 正常。

先运行：

```powershell
poetry run pytest tests/api/test_auth.py -q
```

预期：新增测试 FAIL，表现为 `request.state` 无 `auth/user`、ContextVar 仍为伪造 header 或 `get_current_user` 不存在。

### 3.2 授权核心测试（新建 `tests/api/test_cube_access_control.py`）

使用临时 SQLite `tmp_path / "memos_users.db"` 初始化 `UserManager`；测试不连接 PostgreSQL，认证 dict 直接构造。

- [ ] owner：`alice` 是 Cube owner，`require_cube_access()` 通过。
- [ ] association：`bob` 经 `add_user_to_cube()` 授权，访问同一 Cube 通过。
- [ ] other user：active 用户未关联 Cube，固定 403 和固定 detail。
- [ ] missing/inactive Cube：与未授权完全相同的 403/detail，证明防枚举。
- [ ] missing/inactive SQLite user mapping：固定 403/detail，不暴露“用户不存在”。
- [ ] forged claim：认证 `alice` 携带 `user_id=bob_id`，固定 403。
- [ ] multi-Cube all-or-nothing：列表中任意一个 Cube 未授权，整次请求在业务调用前 403。
- [ ] duplicate Cube：去重后每个 Cube 最多调用一次 `validate_user_cube_access`。
- [ ] `auth_bypassed=True`：请求体 user 优先于 header/default，跳过 SQLite 查询和 ACL。
- [ ] master/internal：显式 privileged bypass；普通 `scopes=["all"]` 不能绕过，防止把 scope 错当 master。

先运行：

```powershell
poetry run pytest tests/api/test_cube_access_control.py -q
```

预期：FAIL，原因是 `memos.api.access_control` 尚不存在。

### 3.3 18 个端点失败测试（新建 `tests/api/test_server_cube_access.py`）

构建与 `test_auth_router_mounts.py` 相同的轻量 app fixture；override 必须使用入口模块绑定的 `verify_api_key` 对象。fixture 建立 `alice` owner、`bob` shared、`mallory` outsider 和对应 Cube。参数化请求逐项断言：

- [ ] outsider 对每个 Cube 端点得到 HTTP 403，固定 detail，底层 search/add/graph/LLM/scheduler handler 未被调用。
- [ ] `alice` 对 owner Cube 通过到达业务 mock。
- [ ] `bob` 对 association Cube 通过到达业务 mock。
- [ ] `X-User-Name: alice` + mallory API key 仍按 mallory 拒绝。
- [ ] 请求体/query 自报 `alice_id` + mallory API key 固定 403。
- [ ] 不存在 Cube 与存在但未授权 Cube 的响应 status/body 完全相同。
- [ ] search/add/feedback/chat 的多 Cube 列表中混入一个未授权 Cube，整次请求 403。
- [ ] chat stream、scheduler wait stream 在返回 `StreamingResponse` 前拒绝，必须是 HTTP 403，不是 `200` 后发送 error SSE。
- [ ] `get_memory_by_ids` 返回的任一 memory metadata 指向未授权 Cube 时 403；授权 metadata 通过。
- [ ] delete-by-memory-ids 预读 metadata 后拒绝，且 `delete_by_memory_ids` 从未被调用。
- [ ] `AUTH_ENABLED=false` 参数化回归：相同旧请求仍到达原 handler，body/query user 和缺省 Cube fallback 不变。

建议参数表使用当前真实路由（不是审计中的旧 method/path），并为不同 payload 建 builder，避免 18 份重复 setup：

```python
UNAUTHORIZED_CASES = [
    ("post", "/product/search", {"query": "q", "user_id": "mallory", "readable_cube_ids": ["alice-cube"]}, None),
    ("post", "/product/add", {"user_id": "mallory", "writable_cube_ids": ["alice-cube"], "messages": [{"role": "user", "content": "x"}]}, None),
    ("post", "/product/create_cube", {"cube_name": "forged", "owner_id": "alice-id"}, None),
    ("post", "/product/register_cube", {"mem_cube_name_or_path": "alice-cube", "mem_cube_id": "alice-cube", "user_id": "mallory"}, None),
    ("post", "/product/get_all", {"user_id": "mallory", "mem_cube_ids": ["alice-cube"], "memory_type": "text_mem"}, None),
    ("post", "/product/get_memory", {"user_id": "mallory", "mem_cube_id": "alice-cube"}, None),
    ("post", "/product/get_memory_by_ids", ["alice-memory-id"], None),
    ("post", "/product/delete_memory", {"writable_cube_ids": ["alice-cube"], "memory_ids": ["alice-memory-id"]}, None),
    ("post", "/product/delete_memory_by_record_id", {"mem_cube_id": "alice-cube", "record_id": "record-1"}, None),
    ("post", "/product/recover_memory_by_record_id", {"mem_cube_id": "alice-cube", "delete_record_id": "record-1"}, None),
    ("post", "/product/feedback", {"user_id": "mallory", "writable_cube_ids": ["alice-cube"], "history": [], "feedback_content": "wrong"}, None),
    ("post", "/product/suggestions", {"user_id": "mallory", "mem_cube_id": "alice-cube", "language": "en"}, None),
    ("get", "/product/scheduler/status", None, {"user_id": "alice-id"}),
    ("post", "/product/scheduler/wait", None, {"user_name": "alice-id", "timeout_seconds": 0}),
    ("get", "/product/scheduler/wait/stream", None, {"user_name": "alice-id", "timeout_seconds": 0}),
    ("get", "/product/scheduler/task_queue_status", None, {"user_id": "alice-id"}),
    ("post", "/product/chat/complete", {"user_id": "mallory", "query": "q", "readable_cube_ids": ["alice-cube"]}, None),
    ("post", "/product/chat/stream", {"user_id": "mallory", "query": "q", "readable_cube_ids": ["alice-cube"]}, None),
]


@pytest.mark.parametrize(("method", "path", "json_body", "query"), UNAUTHORIZED_CASES)
def test_unauthorized_product_endpoint_returns_uniform_403(
    denied_client: TestClient,
    method: str,
    path: str,
    json_body: object | None,
    query: dict[str, object] | None,
) -> None:
    response = denied_client.request(method, path, json=json_body, params=query)
    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient cube access"}
```

先运行：

```powershell
poetry run pytest tests/api/test_server_cube_access.py -q
```

预期：FAIL；当前端点会到达业务 mock/返回 200、404 或 500，而不是统一 403。

## 4. P0-1 实施：A+B+C 组合与精确 diff

### 4.1 `context.py` 增加定向更新 helper

在 `set_request_context()` 后增加，不改变 `RequestContextMiddleware` 创建 trace/env/source 的顺序：

```diff
diff --git a/src/memos/context/context.py b/src/memos/context/context.py
@@
 def set_request_context(context: RequestContext | None) -> None:
     if context:
         _request_context.set(context.to_dict())
     else:
         _request_context.set(None)

+def set_current_user_name(user_name: str | None) -> None:
+    """Replace the authenticated user without dropping other request context fields."""
+    context = _request_context.get()
+    if context is None:
+        return
+    _request_context.set({**context, "user_name": user_name})
```

这样 `request_context.py` 从 header 建立的 `user_name` 只是鉴权前初始值；认证成功后用新 helper 覆盖。`AUTH_ENABLED=false` 时 `verify_api_key` 发布的仍是 header/default，因此旧行为不变。

### 4.2 `auth.py` 发布认证结果并提供依赖

类型和 helper：

```diff
diff --git a/src/memos/api/middleware/auth.py b/src/memos/api/middleware/auth.py
@@
-from typing import Any
+from typing import Any, TypedDict
@@
 from fastapi.security import APIKeyHeader
+from memos.context.context import set_current_user_name
+
+class AuthContext(TypedDict, total=False):
+    user_name: str
+    scopes: list[str]
+    is_master_key: bool
+    auth_bypassed: bool
+    is_internal: bool
+    api_key_id: str
+
+def _publish_authenticated_user(request: Request, auth: AuthContext) -> AuthContext:
+    request.state.auth = auth
+    request.state.user = auth["user_name"]
+    set_current_user_name(auth["user_name"])
+    return auth
```

`verify_api_key()` 不改变任何校验条件，只把四类成功返回（disabled、internal、master、regular）包进 publisher：

```diff
@@ async def verify_api_key(
-) -> dict[str, Any]:
+) -> AuthContext:
@@ if not AUTH_ENABLED:
-        return {
+        return _publish_authenticated_user(request, {
             "user_name": request.headers.get("X-User-Name", "default"),
             "scopes": ["all"],
             "is_master_key": False,
             "auth_bypassed": True,
-        }
+        })
@@ if is_internal_request(request):
-        return {
+        return _publish_authenticated_user(request, {
             "user_name": "internal",
             "scopes": ["all"],
             "is_master_key": False,
             "is_internal": True,
-        }
+        })
@@ if MASTER_KEY_HASH and key_hash == MASTER_KEY_HASH:
-        return {
+        return _publish_authenticated_user(request, {
             "user_name": "admin",
             "scopes": ["all"],
             "is_master_key": True,
-        }
+        })
@@ regular key success
-    return {
+    return _publish_authenticated_user(request, {
         "user_name": key_data["user_name"],
         "scopes": key_data["scopes"],
         "is_master_key": False,
         "api_key_id": key_data["id"],
-    }
+    })
+
+async def get_current_user(
+    request: Request,
+    auth: AuthContext = Depends(verify_api_key),  # noqa: B008
+) -> AuthContext:
+    # Re-publish so app.dependency_overrides[verify_api_key] has identical side effects.
+    return _publish_authenticated_user(request, auth)
```

`get_current_user` 再次声明 `Depends(verify_api_key)`，同时保留入口 app 的 router 级依赖。FastAPI 对同一请求、同一 dependency callable 默认缓存，因此生产请求只验证一次 key；测试 override 也只执行一次。显式重新发布解决现有测试通过 dependency override 绕开 `verify_api_key` 函数体的问题。

`src/memos/api/middleware/__init__.py` 同步导出 `AuthContext` 和 `get_current_user`，但所有业务代码优先从 `memos.api.middleware.auth` 直接导入，避免含糊依赖。

### 4.3 路由注入形态

保持 decorator 和 router mount 不变，仅增加参数并传入 handler：

```diff
diff --git a/src/memos/api/routers/server_router.py b/src/memos/api/routers/server_router.py
@@
-from fastapi import APIRouter, HTTPException, Query
+from fastapi import APIRouter, Depends, HTTPException, Query
+from memos.api.middleware.auth import AuthContext, get_current_user
@@
-def search_memories(search_req: APISearchRequest):
-    return search_handler.handle_search_memories(search_req)
+def search_memories(
+    search_req: APISearchRequest,
+    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
+):
+    return search_handler.handle_search_memories(search_req, current_user=current_user)
```

其余 17 个端点采用相同参数。handler 不读取 `X-User-Name`，也不自行调用 `verify_api_key`；认证身份只能由 `current_user` 进入。

## 5. P0-2 实施：统一访问控制

### 5.1 新增 `CubeAccessControl`

核心接口固定如下，实施时不再分散拼接异常信息：

```python
FORBIDDEN_DETAIL = "Insufficient cube access"


class CubeAccessControl:
    def __init__(self, user_manager: UserManager) -> None:
        self.user_manager = user_manager

    @staticmethod
    def _deny() -> NoReturn:
        raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)

    @staticmethod
    def is_bypassed(auth: AuthContext) -> bool:
        return bool(auth.get("auth_bypassed"))

    @staticmethod
    def is_privileged(auth: AuthContext) -> bool:
        return bool(auth.get("is_master_key") or auth.get("is_internal"))

    def resolve_actor(
        self,
        auth: AuthContext,
        claimed_user_id: str | None = None,
    ) -> str:
        if self.is_bypassed(auth):
            return claimed_user_id or auth.get("user_name", "default")
        if self.is_privileged(auth):
            return claimed_user_id or auth["user_name"]

        user = self.user_manager.get_user_by_name(auth["user_name"])
        if user is None or not user.is_active:
            self._deny()
        if claimed_user_id is not None and claimed_user_id != user.user_id:
            self._deny()
        return user.user_id

    def require_cube_access(
        self,
        auth: AuthContext,
        actor_user_id: str,
        cube_ids: Iterable[str],
    ) -> None:
        if self.is_bypassed(auth) or self.is_privileged(auth):
            return
        for cube_id in dict.fromkeys(cube_ids):
            if not cube_id or not self.user_manager.validate_user_cube_access(
                actor_user_id, cube_id
            ):
                self._deny()
```

实现文件需导入 `Iterable`、`NoReturn`、`HTTPException`、`AuthContext` 和 `UserManager` 并补齐类型。禁止先 `get_cube()` 再分别返回“not found/no permission”；`validate_user_cube_access()` 的所有 `False` 都落到相同 `_deny()`。

在 `server_router.py` 初始化一个共享实例：

```python
user_manager = UserManager()
access_control = CubeAccessControl(user_manager)
dependencies = HandlerDependencies.from_init_server(
    {**components, "access_control": access_control}
)
```

`BaseHandler.access_control` 返回 `self.deps.access_control`；`CubeHandler` 删除自己的 `UserManager()`，改用 `self.access_control.user_manager`。这样一个进程只使用一个授权对象，测试可替换临时 SQLite manager，避免不同 handler 连接不同测试数据库。

### 5.2 身份、fallback 和批量校验顺序

所有 handler 遵守同一顺序：

1. `actor_user_id = access_control.resolve_actor(current_user, claimed_user_id)`。
2. 解析显式 Cube 列表并去重；没有 Cube 时 fallback 为 `[actor_user_id]`，不得 fallback 到未经认证的请求值。
3. 在访问 graph/vector DB、scheduler、LLM、创建 background task 或返回 `StreamingResponse` **之前**调用 `require_cube_access()`。
4. 后续业务使用 `actor_user_id` 作为有效 user。普通认证模式下不得继续使用请求自报 user；disabled/privileged 模式由 `resolve_actor()` 保留兼容语义。
5. readable/writable 当前共用 `validate_user_cube_access()`，因为 SQLite association 没有读写级别。本次不改 schema；细分 ACL 属后续设计。

### 5.3 18 个端点逐项插入点

| # | 当前真实端点 | 精确插入点 | 校验对象与动作 |
|---:|---|---|---|
| 1 | `POST /product/search` | `SearchHandler.handle_search_memories()` 开头，在任何 hook/search 前；`_resolve_cube_ids(search_req, actor_user_id)` | 校验 `search_req.user_id` 等于 actor；校验全部 `readable_cube_ids`；空列表 fallback 到 actor 自身默认 Cube |
| 2 | `POST /product/add` | `AddHandler.handle_add_memories()` 开头，在日志输出完整请求、`_build_cube_view()` 和 scheduler 入队前；`_resolve_cube_ids(add_req, actor_user_id)` | 校验 `user_id`；校验全部 `writable_cube_ids`（deprecated `mem_cube_id` 已由 model validator 转换）；fallback 到 actor Cube |
| 3 | `POST /product/create_cube` | `CubeHandler.create_cube()` 的 `try` 前/第一行，先于 `validate_user()` 和 `create_cube()` | `resolve_actor(auth, request.owner_id)`；普通 key 只能给自己建 Cube。不存在/冒用 owner 固定 403；privileged 仍走原 owner existence 400 语义 |
| 4 | `POST /product/register_cube` | `CubeHandler.register_cube()` 第一行，先于当前 `validate_user` 和成功日志 | `final_cube_id = mem_cube_id or mem_cube_name_or_path`；校验可选 `request.user_id` 与 actor，再校验 final Cube。认证模式下 unknown 和 unauthorized 都为 403；disabled 保持 placeholder 的现有 200/400 行为 |
| 5 | `POST /product/get_all` | 路由仍分支调用；在 `memory_handler.handle_get_subgraph()` 和 `handle_get_all_memories()` 各自开头 | 传入 `current_user/access_control`；校验 `memory_req.user_id`；实际 Cube 为 `mem_cube_ids[0]` 或 actor（不再用伪造 user fallback），然后再读 graph |
| 6 | `POST /product/get_memory` | `memory_handler.handle_get_memories()` 第一行，先于首次 `text_mem.get_all()` | 校验可选 `user_id` 和 `mem_cube_id`；把有效 actor 用于后续 `user_id` filter |
| 7 | `POST /product/get_memory_by_ids` | `memory_handler.handle_get_memory_by_ids()`：调用 `get_by_ids()` 后、构造 response 前 | 从每个 `TextualMemoryItem.metadata.user_name`（或 dict `metadata["user_name"]`）收集 Cube 并批量校验；任何返回项缺 Cube metadata 固定 403；未通过前不得序列化/返回 memories |
| 8 | `POST /product/delete_memory`（审计名 `/delete_memories`） | `memory_handler.handle_delete_memories()` 在任何 delete 前；memory_ids 模式先只读 metadata | 有 `writable_cube_ids` 校验全部；memory_ids 模式从预读结果反查全部 Cube，任一 ID 缺失/缺 metadata 也 403；file/filter/quick-delete 无显式 Cube 时使用 actor 默认 Cube。AUTH disabled 保留原 `writable_cube_ids=None` 调用形态以通过旧单测 |
| 9 | `POST /product/delete_memory_by_record_id` | `server_router.delete_memory_by_record_id()` 中，`graph_db.delete_node_by_mem_cube_id()` 前 | 校验 `memory_req.mem_cube_id`；拒绝时 graph delete mock 必须未调用 |
| 10 | `POST /product/recover_memory_by_record_id` | `server_router.recover_memory_by_record_id()` 中，`graph_db.recover_memory_by_mem_cube_id()` 前 | 校验 `memory_req.mem_cube_id`；拒绝时 graph recover mock 必须未调用 |
| 11 | `POST /product/feedback` | `FeedbackHandler.handle_feedback_memories()` 开头，先于 `_handle_feedback()`/add/scheduler；`_resolve_cube_ids(feedback_req, actor)` | 校验 `feedback_req.user_id` 与全部 `writable_cube_ids`；deprecated `mem_cube_id` 若实际参与解析也必须纳入；fallback actor Cube |
| 12 | `POST /product/suggestions` | `suggestion_handler.handle_get_suggestion_queries()` 开头，先于 `if message` 和 `text_mem.search()` | 修改 handler 参数为同时接收 `cube_id=suggestion_req.mem_cube_id`、claimed `user_id`、`current_user/access_control`；校验 user 和 Cube。修正当前 router 把 `mem_cube_id` 错传为 `user_id` 的混淆，但保持请求/响应 schema |
| 13 | `GET /product/scheduler/status` | `scheduler_handler.handle_scheduler_status()` 第一行，先于 tracker 查询 | `resolve_actor(auth, user_id)`；普通 key 只能查自己。无 Cube 字段时执行 user-scope 校验，不调用 `validate_user_cube_access` |
| 14 | `POST /product/scheduler/wait` | `handle_scheduler_wait()` 第一行，先于 polling loop | 将 `user_name` 当作 claimed SQLite user id 校验；后续轮询只用有效 actor id |
| 15 | `GET /product/scheduler/wait/stream` | `handle_scheduler_wait_stream()` 最外层第一行，先于创建 `event_generator()`/`StreamingResponse` | 先做 user-scope 校验，越权直接 HTTP 403，不能在生成器内吞成 SSE error |
| 16 | `GET /product/scheduler/task_queue_status`（审计写 POST） | `handle_task_queue_status()` 第一行，先于获取 stream keys/Redis | 校验 claimed `user_id`；所有 Redis filter 使用有效 actor id，防止读取他人 queue key |
| 17 | `POST /product/chat/complete` | `ChatHandler.handle_chat_complete()` 开头，先于 search、LLM 和 async add | 解析 actor；read 为 `readable_cube_ids or [actor]`，write 为 `writable_cube_ids or [actor]`（仅当 `add_message_on_answer` 时需要 write）；对并集一次性校验后再工作 |
| 18 | `POST /product/chat/stream` | `ChatHandler.handle_chat_stream()` 外层开头，先于定义/返回生成器 | read fallback 顺序保留显式 `readable_cube_ids -> mem_cube_id -> actor`；write 同理；在建流前校验并集。生成器及 scheduler/background add 只使用已校验列表和 actor |

### 5.4 ID 列表读删的特殊安全规则

`get_memory_by_ids` 和 delete `memory_ids` 没有可信 Cube 入参，不能因为“没有 cube_id”跳过校验：

```python
def _cube_ids_from_memories(memories: list[Any], requested_ids: list[str]) -> list[str]:
    if len(memories) != len(set(requested_ids)):
        raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)
    cube_ids: list[str] = []
    for memory in memories:
        metadata = memory.metadata if hasattr(memory, "metadata") else memory.get("metadata", {})
        cube_id = (
            metadata.user_name if hasattr(metadata, "user_name") else metadata.get("user_name")
        )
        if not cube_id:
            raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)
        cube_ids.append(cube_id)
    return list(dict.fromkeys(cube_ids))
```

规则是 all-or-nothing：缺失 ID、缺 metadata、混入未授权 Cube 都返回同一 403；读接口不返回部分结果，删接口不执行部分删除。该预读只用于授权判定，日志不得输出 memory/vector 内容。

### 5.5 异常传播

现有多个 handler 使用宽泛 `except Exception` 并包装成 500。所有新增访问校验必须放在 `try` 外，或在 `except HTTPException: raise` 分支中原样抛出，确保固定 403 不会变成 400/404/500。尤其：

- `CubeHandler.create_cube/register_cube` 不得让授权 `HTTPException` 进入通用 `except Exception`。
- `ChatHandler.handle_chat_complete/stream` 不得将授权异常包装为 404/500。
- `scheduler wait stream` 不得在 event generator 内捕获授权异常。
- memory delete 不得把授权失败转换成业务 `DeleteMemoryResponse(code=200, status=failure)`。

## 6. 分任务实施顺序

### Task 1：P0-1 失败测试与身份发布

**文件：** `tests/api/test_auth.py`、`src/memos/context/context.py`、`src/memos/api/middleware/auth.py`、`src/memos/api/middleware/__init__.py`

- [ ] 写 3.1 的五个失败测试。
- [ ] 运行 `poetry run pytest tests/api/test_auth.py -q`，确认新增测试因 state/context 缺失失败。
- [ ] 按 4.1/4.2 精确 diff 实现 `AuthContext`、publisher、context setter、`get_current_user`。
- [ ] 再运行同一命令，预期全部 PASS。
- [ ] 提交：`git commit -m "fix(api): propagate authenticated request identity"`。

### Task 2：授权核心失败测试与实现

**文件：** `tests/api/test_cube_access_control.py`、`src/memos/api/access_control.py`、`src/memos/api/handlers/base_handler.py`、`src/memos/api/routers/server_router.py`

- [ ] 写 3.2 的十组失败测试并确认 import/behavior FAIL。
- [ ] 实现 5.1 的 `CubeAccessControl`，固定唯一 403 detail。
- [ ] 在 router 创建共享 `UserManager/CubeAccessControl`，通过 `HandlerDependencies` 注入。
- [ ] 运行 `poetry run pytest tests/api/test_cube_access_control.py -q`，预期全部 PASS。
- [ ] 提交：`git commit -m "fix(api): add centralized cube access control"`。

### Task 3：search/add/cube/feedback/suggestions

**文件：** 对应五个 handler、`server_router.py`、`tests/api/test_server_cube_access.py` 及相关现有测试。

- [ ] 先为表 5.3 的 #1-4、#11-12 写 outsider/owner/shared/header spoof/disabled 测试并确认 FAIL。
- [ ] 在六个 handler 的最外层按 actor -> fallback -> Cube 校验顺序实现。
- [ ] 更新 router 为这些端点注入/传递 `current_user`。
- [ ] 运行：

```powershell
poetry run pytest tests/api/test_server_cube_access.py tests/api/test_cube_endpoints.py tests/api/test_suggestion_handler.py tests/api/test_server_router.py -q
```

- [ ] 预期全部 PASS；提交：`git commit -m "fix(api): enforce cube access on memory entrypoints"`。

### Task 4：memory 读删与 record 操作

**文件：** `memory_handler.py`、`server_router.py`、`tests/api/test_server_cube_access.py`、`test_memory_handler_delete.py`

- [ ] 先为表 5.3 的 #5-10 写失败测试，包含 ID metadata 混合 Cube 和 delete side-effect 未发生。
- [ ] 实现 `_cube_ids_from_memories`、get/get_all/get_by_ids/delete 校验和两个 record route 校验。
- [ ] 确保 disabled 分支不改变现有 delete mock 的调用参数。
- [ ] 运行：

```powershell
poetry run pytest tests/api/test_server_cube_access.py tests/api/test_memory_handler_delete.py tests/api/test_server_router.py -q
```

- [ ] 预期全部 PASS；提交：`git commit -m "fix(api): guard memory reads and deletes by cube"`。

### Task 5：scheduler 与 chat/SSE

**文件：** `scheduler_handler.py`、`chat_handler.py`、`server_router.py`、`tests/api/test_server_cube_access.py`

- [ ] 先为表 5.3 的 #13-18 写失败测试，特别断言两个 stream 越权是初始 HTTP 403。
- [ ] scheduler 在读取 tracker/Redis 或构造 stream 前校验有效 actor。
- [ ] chat 在 search/LLM/background task/stream 前校验 read+write 并集。
- [ ] 运行相关测试，预期全部 PASS。
- [ ] 提交：`git commit -m "fix(api): isolate scheduler and chat by authenticated user"`。

### Task 6：全量验证与 OpenAPI

- [ ] 运行 Ruff 格式/检查：`make format`，预期退出码 0。
- [ ] 运行 API 全量：`poetry run pytest tests/api/ -q`，预期全部通过，新增用例使数量高于审计时的 169。
- [ ] 按项目规则运行 `make openapi`；检查 `git diff -- docs/openapi.json`，预期仅可能有 dependency/security 的等价重排，paths/method/request/response schema 不变。若出现公开 contract 变化，停止并复核，不带入本 P0。
- [ ] 运行全量：`make test`，预期退出码 0。
- [ ] 再运行 `make format`，记录真实输出。
- [ ] 提交：`git commit -m "test(api): cover cube access control regressions"`。

## 7. 风险与非目标

- `UserManager()` 初始化会创建 SQLite 表/root user；必须复用共享实例，且测试注入临时路径，不能让测试读写开发者真实 `MEMOS_DIR/memos_users.db`。
- 两库没有事务一致性。本 P0 只定义稳定关联约定；API key 创建/用户创建的跨库编排、迁移和 orphan 清理另立任务。
- 普通 key 对应的 SQLite 用户被停用后，下一次请求立即 403；无缓存可避免权限撤销延迟。若未来加缓存，必须有短 TTL/显式失效。
- 当前 association 不区分 read/write；本次只修“是否可访问”，不扩展 schema。
- scheduler 单例串扰属于 P1。本次所有 scheduler handler 使用有效 actor 参数，为后续按 actor 分区预留边界，但不重构 scheduler 状态存储。
- 不把 `GET /product/get_memory/{memory_id}`、playground/business/internal dashboard 等未列入 Hermes 18 项的端点悄悄纳入本次。它们可能也需要后续安全审计；若在实施测试中确认同类直接对象引用，应另开审计项，不能无声扩大公开 API 变更。

## 8. 验收清单

- [ ] `verify_api_key` 的 disabled/internal/master/regular 四个成功分支都写入 `request.state.auth` 和 `request.state.user`。
- [ ] `get_current_user` 可供 router/handler 使用，且 dependency override 场景仍发布 state。
- [ ] `RequestContext.user_name` 在认证成功后等于认证 `user_name`；伪造 `X-User-Name` 不会进入 handler/后续日志上下文。
- [ ] PostgreSQL `api_keys.user_name` 只通过精确 `users.user_name` 映射为 SQLite `users.user_id`；代码中没有把二者当同一字段。
- [ ] 表 5.3 的 18 个端点逐项有失败测试和校验插入点，越权均在业务副作用前返回 403。
- [ ] owner 可访问自己的 Cube；`user_cube_association` 中的授权用户可访问共享 Cube。
- [ ] 多 Cube 请求执行 all-or-nothing 校验，任一未授权即整次 403。
- [ ] 不存在、inactive、未授权、缺 metadata 的 Cube 场景响应均为 `403 / Insufficient cube access`，不泄露存在性。
- [ ] 请求体/query user 与普通认证 actor 不一致时固定 403；认证身份优先于 header 和请求体。
- [ ] chat stream 和 scheduler wait stream 在发送响应头前拒绝越权请求。
- [ ] `AUTH_ENABLED=false` 时身份退回请求体/query -> header -> default，旧 handler 参数、fallback 和现有测试不变。
- [ ] master/internal privileged 行为有独立测试；普通 key 即使 `scopes=["all"]` 也不能绕过 Cube ACL。
- [ ] 没有新增依赖、DB schema、route path/method 或 request/response model 变化。
- [ ] `make format`、`poetry run pytest tests/api/ -q`、`make openapi`、`make test` 都有真实成功输出；不得只凭局部 mock 宣称完成。
