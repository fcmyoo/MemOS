# MemOS 多用户隔离 P0 修复 — 审计清单（Codex 方案输入）

> 审计人：Hermes Agent（逐行核实源码）| 日期：2026-08-09
> 分支：feat/api-auth-hardening（6f37591f）
> 目标：修复 Cube 访问权限缺失（数据泄露风险），让管理员可安全开放多用户

## 一、问题定级

🔴 **P0-1 身份丢失**：API key 鉴权后，真实用户身份无法传到 handler
🔴 **P0-2 Cube 权限不校验**：所有 cube 相关端点不校验"此用户是否有权访问此 cube"

两个问题叠加 = 任意持 key 用户可读写任意 cube。

## 二、P0-1：鉴权身份丢失（根因）

### 事实

1. `server_api.py:51`：`app.include_router(server_router.router, dependencies=[Depends(verify_api_key)])`
   —— verify_api_key 挂为 **router 级依赖**，返回值无处传递
2. `server_router.py` 全部 25 个 handler 签名**均无 auth/user 参数**
   （如 `def search_memories(search_req: APISearchRequest)`）
3. `request_context.py:56`：`user_name = request.headers.get("x-user-name")`
   —— RequestContext.user_name 来自 **HTTP 请求头，客户端可任意伪造**
4. `verify_api_key()` 返回的 `auth["user_name"]`（真实用户，来自 api_keys 表）
   **没有任何机制注入到请求上下文或 handler**

### 后果

- handler 里所有 `user_id`/`mem_cube_id` 均来自**客户端请求体自报**（可信度=0）
- `X-User-Name` 头可伪造 → 日志/审计/后续校验全部失真

### 修复方向（待 Codex 细化）

方案 A：verify_api_key 返回后写入 `request.state.user`，新增依赖注入到 handler
方案 B：在 verify_api_key 内同步 `set_request_context` 的 user_name（认证用户覆盖 header）
方案 C：router 依赖返回 user 字典，handler 通过新注入参数 `auth: dict = Depends(get_current_user)` 获取

## 三、P0-2：Cube 访问权限不校验（根因）

### 事实

1. 校验函数存在：`user_manager.py:305 validate_user_cube_access(user_id, cube_id)`
   —— 校验 user 存在/active、cube 存在/active、owner 或 user_cube_association 含该用户
2. **仅两处调用，都在 SDK 库层**：`mem_os/core.py:228, 512`
3. **HTTP API handlers 路径零调用**：
   - `search_handler.py:1031 _resolve_cube_ids()` —— 直接返回请求体 `readable_cube_ids`，无校验
   - `memory_handler.py:56,124,224` —— 直接用请求 `mem_cube_id` 读写
   - `cube_handler.py:98` —— 只 `validate_user`（用户存在），**不校验 cube 归属**
   - `suggestion_handler.py:108` —— 用请求 user_id
   - `scheduler_handler.py:223-264` —— 按请求 user_id 查任务状态

### 需要加校验的端点（server_router.py 25 端点中涉 cube/user 的）

| 端点 | handler | 涉及字段 | 缺校验 |
|---|---|---|---|
| POST /product/search | search_handler | readable_cube_ids / user_id | ✅ 缺 |
| POST /product/add | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/create_cube | cube_handler | owner_id | ⚠️ 部分（validate_user） |
| POST /product/register_cube | cube_handler | user_id/mem_cube_id | ✅ 缺 |
| POST /product/get_all | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/get_memory | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/get_memory_by_ids | memory_handler | user_id/mem_cube_id | ✅ 缺 |
| POST /product/delete_memories | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/delete_memory_by_record_id | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/recover_memory_by_record_id | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/feedback | memory_handler | mem_cube_id | ✅ 缺 |
| POST /product/suggestions | suggestion_handler | user_id | ⚠️ 部分 |
| GET /product/scheduler/status | scheduler_handler | user_id | ✅ 缺 |
| POST /product/scheduler/wait | scheduler_handler | user_id | ✅ 缺 |
| GET /product/scheduler/wait/stream | scheduler_handler | user_id | ✅ 缺 |
| POST /product/scheduler/task_queue_status | scheduler_handler | user_id | ✅ 缺 |
| POST /product/chat/complete | chat_handler | user_id/mem_cube_id | ✅ 缺 |
| POST /product/chat/stream | chat_handler | user_id | ✅ 缺 |

### 语义设计（待 Codex 确认）

- user 身份应取 **P0-1 修复后的认证用户**（auth.user_name），而非请求体
- 用户想操作自己未授权的 cube → 403（`Insufficient cube access`）
- 兼容旧行为：cube_id 缺省时 fallback 到 user_id 的默认 cube（用户自己的）
- 请求体 user_id 与认证 user 不一致时：以认证 user 为准（或拒绝，待定）

## 四、约束（Codex 方案必须遵守）

1. 不动 server_router.py 的 25 个端点注册方式（继续用 router 级依赖统一挂鉴权）
2. 不改变 api_keys 表结构 / verify_api_key 的 key 校验逻辑（已加固完成）
3. UserManager 默认 SQLite（MEMOS_DIR/memos_users.db）——注意**用户表与 cube 表在 SQLite，api_keys 在 PostgreSQL**，两库并存
4. 兼容 AUTH_ENABLED=false 本地开发模式（此时 user 身份退回请求体/X-User-Name，不破坏现有测试）
5. 不引入新依赖；不修改 pyproject.toml
6. TDD：先写失败测试（越权访问返回 403），再实现
7. 提供回归测试：合法用户访问自己 cube 通过；访问他人 cube 403；AUTH_ENABLED=false 时行为不变

## 五、验收标准

- [ ] 鉴权后真实 user 身份注入请求上下文，且优先于 X-User-Name header
- [ ] 上述 18 个端点全部校验 cube/user 归属，越权返回 403
- [ ] owner 用户可访问自己的 cube；被授权用户（user_cube_association）可访问
- [ ] 未授权 cube 一律 403，不泄露 cube 是否存在（防枚举）
- [ ] AUTH_ENABLED=false 时现有功能与测试零回归
- [ ] 新增测试覆盖：越权 403、授权通过、owner 通过、header 伪造被忽略
- [ ] 全量 pytest tests/api/ 通过（现 169 个）

## 六、风险提示

- SQLite 用户库与 PG key 库并存：权限校验查 UserManager（SQLite），身份查 api_keys（PG），
  两库的 user_name 需一致约定（api_keys.user_name 应等于 users.user_name）
- scheduler 单例状态串扰（P1）不在本次范围，但方案应预留接口
- `_resolve_cube_ids` fallback 到 user_id 的逻辑：若认证用户与请求 user_id 不同，
  fallback 目标必须用认证用户

## 七、参考文件

- `src/memos/api/middleware/auth.py` — verify_api_key（157-237 行，返回 auth dict）
- `src/memos/api/routers/server_router.py` — 25 端点（111-506 行）
- `src/memos/api/handlers/search_handler.py` — _resolve_cube_ids（1031 行）
- `src/memos/api/handlers/memory_handler.py` — 直接读写（56/124/224 行）
- `src/memos/api/handlers/cube_handler.py` — register_cube 自认缺集成（85-130 行）
- `src/memos/api/middleware/request_context.py` — user_name 从 header 来（56 行）
- `src/memos/context/context.py` — RequestContext（25-60 行）
- `src/memos/mem_user/user_manager.py` — validate_user_cube_access（305 行）
- `src/memos/api/product_models.py` — 请求模型（user_id/mem_cube_id/readable_cube_ids）
