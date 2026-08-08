# MemOS 远程部署鉴权加固实施方案

> **给实施代理：** 按任务顺序执行；使用 `superpowers:test-driven-development` 先写失败测试，完成后使用 `superpowers:verification-before-completion` 做最终验证。
>
> **执行要求：** 实施阶段按 TDD 执行；先补失败测试，再做最小实现。涉及 `src/memos/api/` 的变更必须运行 `make openapi`。涉及 `pyproject.toml` 或数据库初始化方式的变更须按 `AGENTS.md` 在实施前取得批准。

**目标：** 关闭 Hermes 审计 M1-M9，确保两个 API 入口的 25 个 `/product/*` 业务端点统一经过 API key 鉴权，同时补齐密钥表、部署配置、Docker 网络边界和回归测试。

**总体架构：** 鉴权只在应用组装层统一挂载，不在 `server_router.py` 的 25 个端点逐个添加依赖。`server_api.py` 与 `server_api_ext.py` 都通过 `app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])` 保护整个业务 router；`/health`、文档和静态下载不随业务 router 自动受保护。常规 API key 使用 PostgreSQL 中的 SHA-256 hash 校验，master key 使用环境变量中的 hash；内部服务保留现有“受信来源白名单或正确共享密钥”的兼容语义，但共享密钥未配置时不得因空值比较而放行。

**技术栈：** Python 3.11、FastAPI dependency injection、PostgreSQL 16、psycopg2、Docker Compose、pytest/TestClient、Ruff。

---

## 1. 范围、决策与非目标

### 1.1 本次范围

| 审计项 | 方案落点 | 完成条件 |
|---|---|---|
| M1 | `src/memos/api/server_api.py` | 默认入口统一保护业务 router |
| M2 | 应用组装层保护 `server_router.py` | 25 个 `/product/*` operation 全部声明且执行鉴权，不逐端点改代码 |
| M3 | `src/memos/api/server_api_ext.py` | 扩展入口使用同一挂载方式 |
| M4 | `docker/.env.example` | 增加完整鉴权、PostgreSQL及内部服务配置 |
| M5 | `src/memos/api/middleware/auth.py` | 默认开启鉴权，配置缺失时 fail-closed，显式关闭仍兼容本地开发 |
| M6 | `docker/docker-compose.yml` | 移除硬编码弱密码，内部存储不再对所有网卡开放 |
| M7 | `docker/Dockerfile.krolik` | 删除失效 overlay 复制，或退役该参考 Dockerfile；本方案推荐前者 |
| M8 | 新增 `docker/postgres/init/001_api_keys.sql` 并挂入 Compose | 新库首次启动自动创建可用 `api_keys` 表 |
| M9 | 新增鉴权、入口、schema 与 Compose 测试 | 正反路径和路由覆盖均有回归保护 |

### 1.2 关键决策

1. **鉴权挂在业务 router 的 include 点。** 这是唯一能同时满足统一、低侵入、未来新增 `/product/*` 端点自动受保护的方案。
2. **默认 fail-closed。** `AUTH_ENABLED` 缺省值改为 `true`；只有显式 `AUTH_ENABLED=false` 才绕过鉴权。
3. **修复空 secret 旁路但不破坏白名单兼容性。** 保留当前受信来源直接放行，以及非受信来源凭 `X-Internal-Service` 共享密钥放行的两条路径；共享密钥必须非空，并使用恒定时间比较。当前实现中 header 与环境变量同时缺失时 `None == None` 会错误放行，这是开启默认鉴权前必须修复的前置缺陷。
4. **schema 采用 PostgreSQL 官方镜像 initdb 机制。** SQL 放入 `/docker-entrypoint-initdb.d/`，首次初始化 volume 时自动执行；不在每次 API 启动时隐式修改 schema。
5. **不把限流当鉴权。** `rate_limit.py` 保持职责不变；扩展入口继续在鉴权前削减暴力请求。

### 1.3 非目标

- 不在本轮设计细粒度 `/product/*` read/write scope；统一依赖只验证身份，现有 admin router 继续使用 `admin` scope。
- 不修改业务 handler、请求/响应模型或 25 个 endpoint 函数。
- 不把 API key 明文写入数据库、日志、示例配置或测试 fixture。
- 不在本方案文档编写阶段修改任何业务代码、Compose 或环境文件。

## 2. 目标请求链路

```text
客户端请求 /product/*
        |
        v
RateLimitMiddleware（仅 server_api_ext，启用时）
        |
        v
include_router dependencies=[Depends(verify_api_key)]
        |
        +-- AUTH_ENABLED=false ------------------> 开发兼容放行
        |
        +-- 受信内部源白名单 --------------------> 内部服务放行
        |
        +-- 非空且正确的内部共享密钥 ------------> 内部服务放行
        |
        +-- MASTER_KEY_HASH 命中 ----------------> master 放行
        |
        +-- krlk_* hash 命中有效 api_keys 记录 -> 常规 key 放行并更新时间
        |
        `-- 缺失/错误/吊销/过期/DB 不可用 ------> 401，业务 handler 不执行
```

`/health` 保持公开，供容器与负载均衡器探活。`/admin/health` 当前公开且只返回布尔状态，本轮不改变其契约；其余 admin key 管理端点继续由已有 `require_scope("admin")` 保护。默认入口 `server_api.py` 仍不挂 `admin_router`，因此默认部署使用 master key 启动，普通 key 通过受控离线工具或 `server_api_ext.py` 的 admin API 创建；把管理面暴露到默认入口不属于 M1-M9，需单独设计和审批。`/download` 不属于 `server_router`，本轮不改变其公开性；若其中可能包含用户数据，应单独发起静态文件授权设计，不能误认为本方案已覆盖。

## 3. 文件改动总览

### 3.1 新增文件

| 文件 | 责任 |
|---|---|
| `docker/postgres/init/001_api_keys.sql` | 幂等创建 `api_keys` 表、约束和索引 |
| `tests/api/test_auth.py` | 鉴权函数的单元测试，不连接真实 PostgreSQL |
| `tests/api/test_auth_router_mounts.py` | 两个入口的统一鉴权与 25 个业务 operation 覆盖测试 |
| `tests/api/test_auth_schema.py` | 校验 schema 与运行时代码字段/类型契约 |
| `tests/api/test_admin_auth.py` | 管理端点的 scope、创建、列出和吊销生命周期测试 |
| `tests/docker/test_auth_compose.py` | 静态校验 Compose 密码、端口和 initdb 挂载 |

### 3.2 修改文件

| 文件 | 具体责任 |
|---|---|
| `src/memos/api/server_api.py` | 引入 `Depends`/`verify_api_key`，统一保护业务 router |
| `src/memos/api/server_api_ext.py` | 同上，保留 admin、CORS、安全头和限流；健康信息的鉴权缺省值同步为开启 |
| `src/memos/api/middleware/auth.py` | 默认开启；修复内部服务空 secret 放行；修正 `TIMESTAMPTZ` 过期时间比较；使用项目 logger 规范 |
| `src/memos/api/routers/admin_router.py` | 仅把 `/admin/health` 的鉴权缺省展示值同步为开启，不改 key 管理路由依赖或响应结构 |
| `docker/.env.example` | 增加生产安全的鉴权与数据库配置模板 |
| `docker/docker-compose.yml` | 增加 PostgreSQL、initdb、健康检查和 secrets 透传；收紧存储端口 |
| `docker/Dockerfile.krolik` | 移除不存在的 `overlays/krolik/` COPY，直接使用仓库内扩展模块 |
| `README.md` | Docker/uvicorn 启动步骤补充鉴权配置，调用示例增加 `Authorization` header |
| `docs/cn/open_source/getting_started/rest_api_server.md` | 中文自托管说明同步鉴权启用与密钥配置 |
| `docs/en/open_source/getting_started/rest_api_server.md` | 英文自托管说明同步相同内容 |
| `docs/openapi.json` | 由 `make openapi` 生成，记录 `/product/*` 的 API key security requirement |

### 3.3 明确不修改

- `src/memos/api/routers/server_router.py`：保留 25 个业务端点原样。统一挂载后，未来加入该 router 的端点自动继承鉴权。
- `src/memos/api/middleware/rate_limit.py`：保留限流逻辑，不承担身份校验。
- `src/memos/api/routers/admin_router.py` 的 key 管理端点：继续使用现有 `require_scope("admin")`，不改依赖、请求模型或响应结构；只同步 `/admin/health` 的缺省状态值。

## 4. 分项实施步骤

### 任务 1：先建立鉴权中间件失败测试（M5、M9）

**文件：**

- 新增：`tests/api/test_auth.py`
- 新增：`tests/api/test_admin_auth.py`（本任务先覆盖 `/admin/health`，任务 9 再补 key 生命周期）
- 后续修改：`src/memos/api/middleware/auth.py`
- 后续修改：`src/memos/api/routers/admin_router.py`

先写测试，并用 `monkeypatch` 修改模块常量；不要依赖导入顺序或真实 `.env`。每个测试构造 Starlette `Request` scope，显式设置 `client=(host, port)`。

测试用例：

1. `test_auth_defaults_to_enabled_when_env_missing`：移除 `AUTH_ENABLED` 后重新加载模块，断言 `AUTH_ENABLED is True`。
2. `test_explicit_auth_disabled_bypasses_auth`：`AUTH_ENABLED=False`、无 header 时返回 `auth_bypassed=True`，保证本地兼容。
3. `test_missing_key_returns_401_when_enabled`：外部 IP、无 key、无内部 header，断言 401、`detail == "Missing API key"`、`WWW-Authenticate == "ApiKey"`。
4. `test_invalid_key_format_returns_401`：`Authorization: not-a-key`，断言 401 且不访问数据库。
5. `test_bearer_master_key_is_accepted`：固定 `mk_*` key 及 SHA-256 hash，使用 `Authorization: Bearer ...`，断言 `is_master_key=True`。
6. `test_token_master_key_is_accepted`：覆盖 `Token` 前缀兼容性。
7. `test_regular_key_is_accepted`：patch `lookup_api_key` 返回用户和 scope，断言规范 `krlk_<64hex>` 被接受。
8. `test_unknown_regular_key_returns_401`：lookup 返回 `None`，断言 401。
9. `test_revoked_or_expired_key_returns_401`：分别 mock lookup 无结果，覆盖运行时统一拒绝分支；数据库级有效性由后续 lookup 测试细化。
10. `test_missing_internal_header_and_unset_secret_is_not_internal`：`INTERNAL_SERVICE_SECRET` 和 header 都不存在时，断言 `is_internal_request()` 为 false，防止当前 `None == None` 漏洞。
11. `test_untrusted_source_with_matching_internal_secret_is_accepted`：外部 IP 携带正确、非空 secret 时返回 `is_internal=True`，锁定现有容器间共享密钥能力。
12. `test_untrusted_source_with_wrong_internal_secret_is_rejected`：外部 IP 携带错误 secret 时进入常规 key 校验并返回 401。
13. `test_trusted_internal_service_is_accepted_without_header`：`INTERNAL_SERVICE_IPS` 中的 host/IP 无 header 也返回 `is_internal=True`，锁定 Hermes 要求的内部服务白名单兼容性。
14. `test_database_unavailable_fails_closed`：合法格式 key、lookup 返回 `None`，断言 401，不允许认证数据库故障时放行。
15. `test_lookup_rejects_inactive_key`、`test_lookup_rejects_expired_key`、`test_lookup_accepts_unexpired_key_and_updates_last_used`：使用 fake pool/connection/cursor 覆盖 SQL 查询、`last_used_at` 更新和连接归还。
16. `test_admin_health_defaults_to_auth_enabled`：移除 `AUTH_ENABLED` 后调用 `/admin/health`，断言 `auth_enabled is True`。
17. `test_auth_configuration_loads_dotenv_before_constants`：在临时目录写入不含真实凭据的 `.env`，重新导入鉴权模块，断言 `AUTH_ENABLED`/`MASTER_KEY_HASH` 在模块常量初始化前已加载；测试结束恢复模块与环境，避免污染其他用例。

先运行：

```bash
poetry run pytest tests/api/test_auth.py -q
poetry run pytest tests/api/test_admin_auth.py -q -k health
```

预期首次失败：默认值仍为 `false`；空内部 secret 会放行；`TIMESTAMPTZ` schema 对应的 `datetime` 与 `time.time()` 浮点比较不兼容。

### 任务 2：加固鉴权基础逻辑（M5）

**文件：**

- 修改：`src/memos/api/middleware/auth.py`
- 修改：`src/memos/api/routers/admin_router.py`

按以下 diff 级别修改：

```diff
+import hmac
 import os
-import time
+from datetime import UTC, datetime
+
+from dotenv import load_dotenv
 ...
+load_dotenv()
+
-AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"
+AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
 MASTER_KEY_HASH = os.getenv("MASTER_KEY_HASH")
+INTERNAL_SERVICE_SECRET = os.getenv("INTERNAL_SERVICE_SECRET")
```

`load_dotenv()` 必须位于环境常量初始化之前。原因是两个入口会在应用模块导入阶段导入 `verify_api_key`；若仍只依赖 `server_api.py` 后面的 `load_dotenv()`，本地 `.env` 中的 `AUTH_ENABLED`、`MASTER_KEY_HASH` 和内部 secret 已经来不及进入模块常量，`server_api_ext.py` 则完全不会加载它。Docker/进程环境变量仍优先于 `.env`，不改变容器部署语义。

`lookup_api_key()` 中按 PostgreSQL `TIMESTAMPTZ` 返回的 aware `datetime` 比较：

```diff
-if expires_at and expires_at < time.time():
+if expires_at and expires_at <= datetime.now(UTC):
```

`is_internal_request()` 保留“受信来源 OR 非空共享密钥”的既有能力，只修复两个空值相等导致任意外部请求被当作内部请求的问题：

```python
def is_internal_request(request: Request) -> bool:
    client_host = request.client.host if request.client else None
    if client_host in INTERNAL_SERVICE_IPS:
        return True

    internal_header = request.headers.get("X-Internal-Service")
    return bool(INTERNAL_SERVICE_SECRET and internal_header) and hmac.compare_digest(
        internal_header,
        INTERNAL_SERVICE_SECRET,
    )
```

同时把本文件内 logger 的 f-string 调用改为参数化形式，例如：

```diff
-logger.error(f"Failed to initialize auth pool: {e}")
+logger.error("Failed to initialize auth pool: %s", e)
```

覆盖数据库错误、无效 key、内部请求、认证用户等所有现存 logger 调用；不记录完整 key/hash，仅保留安全前缀。此项不改变 key 格式、master key 算法、scope 或连接池规模。

`admin_router.py` 的健康状态必须与新的缺省值一致：

```diff
-auth_enabled = os.getenv("AUTH_ENABLED", "false").lower() == "true"
+auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"
```

在 `tests/api/test_admin_auth.py` 增加未设置 `AUTH_ENABLED` 时 `/admin/health` 返回 `auth_enabled: true` 的用例；不改变该端点当前公开可访问的属性。

运行：

```bash
poetry run pytest tests/api/test_auth.py -q
```

预期：全部通过。

### 任务 3：默认入口统一挂鉴权（M1、M2）

**文件：**

- 修改：`src/memos/api/server_api.py`
- 新增测试：`tests/api/test_auth_router_mounts.py`

先在测试中 patch `memos.api.handlers.init_server`，沿用 `tests/api/test_server_router.py` 的 mock component 集合，再导入入口 app。为避免模块级 `AUTH_ENABLED` 污染，使用 `app.dependency_overrides[verify_api_key]` 观察依赖是否执行；业务 handler 用 mock，不能初始化真实 LLM/数据库。

`server_api.py` 精确改动：

```diff
-from fastapi import FastAPI, HTTPException
+from fastapi import Depends, FastAPI, HTTPException
 ...
+from memos.api.middleware.auth import verify_api_key
 ...
-app.include_router(server_router_module.router)
+app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])
```

这里必须保持 `app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])` 这一行式统一挂载。禁止在 `server_router.py` 的 25 个 endpoint 上逐一加 `Depends`。

测试必须证明：

- `/product/search` 在鉴权依赖拒绝时返回 401，search handler 未调用。
- 鉴权依赖通过后，同一请求进入 handler。
- `/health` 不执行该依赖且返回 200。
- 从 `app.routes`/OpenAPI 中枚举所有以 `/product/` 开头的 operation，共 25 个；每一个都含 API key security requirement，防止未来漏挂或误用另一 router。

运行：

```bash
poetry run pytest tests/api/test_auth_router_mounts.py -q -k server_api
```

### 任务 4：扩展入口使用完全相同的统一挂载（M3）

**文件：**

- 修改：`src/memos/api/server_api_ext.py`
- 扩展：`tests/api/test_auth_router_mounts.py`

精确改动：

```diff
-from fastapi import FastAPI
+from fastapi import Depends, FastAPI
 ...
+from memos.api.middleware.auth import verify_api_key
 ...
-app.include_router(server_router_module.router)
+app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])
```

健康状态的缺省值同步修改：

```diff
-"auth_enabled": os.getenv("AUTH_ENABLED", "false").lower() == "true",
+"auth_enabled": os.getenv("AUTH_ENABLED", "true").lower() == "true",
```

保留相邻的 `app.include_router(admin_router)` 不变，避免给整个 admin router 叠加普通身份依赖并改变其现有 scope 行为。扩展入口测试复用任务 3 的参数化断言，另验证：

- 25 个 `/product/*` operation 全部受保护。
- `/health` 公开。
- `/admin/keys` 仍声明 admin scope 依赖。
- 开启 `RateLimitMiddleware` 时，鉴权拒绝仍为 401；限流达到阈值时 429，不以限流替代鉴权。

运行：

```bash
poetry run pytest tests/api/test_auth_router_mounts.py -q
```

### 任务 5：提供可执行的 `api_keys` schema（M8）

**文件：**

- 新增：`docker/postgres/init/001_api_keys.sql`
- 新增：`tests/api/test_auth_schema.py`

SQL 文件使用以下完整内容：

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    key_prefix VARCHAR(12) NOT NULL,
    user_name VARCHAR(255) NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[],
    description VARCHAR(500),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by VARCHAR(255),
    CONSTRAINT api_keys_key_hash_sha256 CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT api_keys_key_prefix_format CHECK (key_prefix ~ '^krlk_[0-9a-f]{7}$'),
    CONSTRAINT api_keys_scopes_nonempty CHECK (cardinality(scopes) > 0)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_created
    ON api_keys (user_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_api_keys_expiration
    ON api_keys (expires_at)
    WHERE is_active = TRUE AND expires_at IS NOT NULL;
```

说明：PostgreSQL 13+ 内置 `gen_random_uuid()`，计划采用 `postgres:16-alpine`，无需 `uuid-ossp` extension。`key_hash` 的 `UNIQUE` 约束已经产生索引，因此不再创建重复的 hash 索引；仅保留用户列表和过期扫描索引。schema 不增加 scope 枚举约束，因为当前 `CreateKeyRequest.scopes` 接受任意字符串；本轮不夹带公开请求模型变更，允许值收紧应另开任务并先取得批准。

测试静态解析 SQL 并断言所有运行时字段存在、`scopes` 为 `TEXT[]`、`expires_at/last_used_at/created_at` 为 `TIMESTAMPTZ`、`key_hash` 唯一、默认 active、scope 非空。若 CI 提供 Docker，再执行真实 PostgreSQL 集成测试：运行 SQL 两次证明幂等，使用 `create_api_key_in_db()` 插入、`lookup_api_key()` 查询并更新 `last_used_at`、`list_api_keys()` 返回、`revoke_api_key()` 吊销。

### 任务 6：补全环境变量模板与自托管文档（M4、M5、M6、M8）

**文件：**

- 修改：`docker/.env.example`
- 修改：`README.md`
- 修改：`docs/cn/open_source/getting_started/rest_api_server.md`
- 修改：`docs/en/open_source/getting_started/rest_api_server.md`

在模型配置之前新增如下鉴权段；示例只写占位符，不提交真实 secret/hash：

```dotenv
# API authentication (enabled by default; set false only for isolated local development)
AUTH_ENABLED=true

# SHA-256 hex digest of an mk_* master key; never place the plaintext master key here
MASTER_KEY_HASH=

# Shared secret for internal callers outside INTERNAL_SERVICE_IPS
INTERNAL_SERVICE_SECRET=

# PostgreSQL backing store for API key hashes
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=memos
POSTGRES_USER=memos
POSTGRES_PASSWORD=

# Docker service credentials and optional host bindings
NEO4J_USER=neo4j
NEO4J_PASSWORD=
MEMOS_BIND_ADDRESS=127.0.0.1
NEO4J_BIND_ADDRESS=127.0.0.1
QDRANT_BIND_ADDRESS=127.0.0.1
POSTGRES_BIND_ADDRESS=127.0.0.1
```

随后删除原 Neo4j 段中重复的 `NEO4J_USER`/`NEO4J_PASSWORD=12345678`，保留 URI、backend、database 等非 secret 配置。密码和内部 secret 可用第一条命令分别生成；master key 必须在受控终端中仅显示一次，调用方保存明文，仓库和数据库只保存 hash：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from memos.api.utils.api_keys import generate_master_key; key, digest = generate_master_key(); print(f'ONE_TIME_MASTER_KEY={key}'); print(f'MASTER_KEY_HASH={digest}')"
```

`ONE_TIME_MASTER_KEY` 只进入调用方 secret manager，不写入 `.env`。上述 secret/hash 示例值故意留空，复制为根目录 `.env` 后必须填写；Compose 的 required interpolation 保证密码/hash 为空时直接失败，避免示例占位字符串被误当成有效配置。`AUTH_ENABLED=false` 只作为隔离开发环境的显式回退，不应成为示例默认值。

同步三个自托管文档：明确执行 `cp docker/.env.example .env` 后必须生成并填写 master hash、PostgreSQL/Neo4j 密码和内部 secret；Docker 命令统一为 `docker compose --env-file ../.env up --build`（从 `docker/` 目录执行），uvicorn 调用示例增加 `Authorization: Bearer <master-or-api-key>`。文档不得放真实 key，也不得暗示默认入口提供 `/admin/keys`；普通 key 的离线创建步骤与扩展入口使用方式需明确区分。

默认入口的普通 key 可用现有 utility 一次性创建，文档给出以下受控终端示例；输出的 `API_KEY` 立即进入调用方 secret manager，不写文件：

```bash
docker compose --env-file ../.env exec -T memos python - <<'PY'
import os
import psycopg2

from memos.api.utils.api_keys import create_api_key_in_db

connection = psycopg2.connect(
    host=os.environ["POSTGRES_HOST"],
    port=int(os.environ["POSTGRES_PORT"]),
    dbname=os.environ["POSTGRES_DB"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
)
try:
    generated = create_api_key_in_db(
        connection,
        user_name="bootstrap-client",
        scopes=["read", "write"],
        description="initial self-hosted client",
        created_by="offline-bootstrap",
    )
    print(f"API_KEY={generated.key}")
finally:
    connection.close()
PY
```

### 任务 7：收紧 Compose 并纳入 schema 初始化（M6、M8）

**文件：**

- 修改：`docker/docker-compose.yml`
- 新增测试：`tests/docker/test_auth_compose.py`

#### 7.1 `memos` 服务

精确修改：

```diff
 ports:
-  - "8000:8000"
+  - "${MEMOS_BIND_ADDRESS:-127.0.0.1}:8000:8000"
 depends_on:
-  - neo4j
-  - qdrant
+  neo4j:
+    condition: service_healthy
+  qdrant:
+    condition: service_started
+  postgres:
+    condition: service_healthy
 environment:
+  - AUTH_ENABLED=${AUTH_ENABLED:-true}
+  - MASTER_KEY_HASH=${MASTER_KEY_HASH:?MASTER_KEY_HASH must be set}
+  - INTERNAL_SERVICE_SECRET=${INTERNAL_SERVICE_SECRET:?INTERNAL_SERVICE_SECRET must be set}
+  - POSTGRES_HOST=postgres
+  - POSTGRES_PORT=5432
+  - POSTGRES_DB=${POSTGRES_DB:-memos}
+  - POSTGRES_USER=${POSTGRES_USER:-memos}
+  - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}
```

注意：Compose 当前已有 `env_file: ../.env`，但 `${VAR}` 插值来自 shell 或 Compose CLI 的 `--env-file`，不是 service `env_file`。文档固定使用以下命令（仓库根目录执行）：

```bash
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

从 `docker/` 目录执行时，等价命令固定为 `docker compose --env-file ../.env up --build`。任务 6 同步更新 README 与中英文部署文档，不保留依赖隐式 `.env` 搜索路径的旧命令。

#### 7.2 新增 `postgres` 服务

```yaml
  postgres:
    image: postgres:16-alpine
    container_name: memos-postgres
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-memos}
      POSTGRES_USER: ${POSTGRES_USER:-memos}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}
    ports:
      - "${POSTGRES_BIND_ADDRESS:-127.0.0.1}:5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-memos} -d ${POSTGRES_DB:-memos}"]
      interval: 5s
      timeout: 5s
      retries: 12
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./postgres/init/001_api_keys.sql:/docker-entrypoint-initdb.d/001_api_keys.sql:ro
    restart: unless-stopped
    networks:
      - memos_network
```

并在顶层 `volumes:` 增加 `postgres_data:`。

`docker-entrypoint-initdb.d` 只在空数据卷首次初始化时执行。已有 volume 的升级命令必须写入部署说明：

```bash
docker compose --env-file .env -f docker/docker-compose.yml exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  < docker/postgres/init/001_api_keys.sql
```

#### 7.3 Neo4j 与 Qdrant

```diff
 neo4j:
   ports:
-    - "7474:7474"
-    - "7687:7687"
+    - "${NEO4J_BIND_ADDRESS:-127.0.0.1}:7474:7474"
+    - "${NEO4J_BIND_ADDRESS:-127.0.0.1}:7687:7687"
   environment:
-    NEO4J_AUTH: "neo4j/12345678"
+    NEO4J_AUTH: "${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set}"
 qdrant:
   ports:
-    - "6333:6333"
-    - "6334:6334"
+    - "${QDRANT_BIND_ADDRESS:-127.0.0.1}:6333:6333"
+    - "${QDRANT_BIND_ADDRESS:-127.0.0.1}:6334:6334"
```

更严格的生产 profile 可完全删除 PostgreSQL、Neo4j、Qdrant 的 `ports:`，仅通过 `memos_network` 访问；本方案以默认 loopback 绑定兼顾本地调试。公网只应由反向代理暴露 TLS 端口，`MEMOS_BIND_ADDRESS=0.0.0.0` 必须是部署者的显式决定。

#### 7.4 静态测试

`tests/docker/test_auth_compose.py` 使用已存在的 YAML 能力或文本断言，不新增依赖，覆盖：

- 文件中不再出现 `12345678`。
- 三个存储服务无裸 `HOST:CONTAINER` 端口映射，默认绑定均为 `127.0.0.1`。
- `POSTGRES_PASSWORD`/`NEO4J_PASSWORD` 使用 required interpolation，无弱默认值。
- `memos` 显式收到鉴权与 PostgreSQL变量。
- init SQL 以只读方式挂载到 `/docker-entrypoint-initdb.d/`。
- PostgreSQL 有 healthcheck，memos 等待其 healthy。

本机有 Docker 时再运行：

```bash
docker compose --env-file .env -f docker/docker-compose.yml config
```

预期：配置展开成功，无未设置必填变量、无 YAML 错误。本方案编写环境未安装 Docker，因此实施验收必须在 CI 或具备 Docker 的主机补跑。

### 任务 8：修复失效的 Krolik 构建路径（M7）

**文件：** `docker/Dockerfile.krolik`

当前 `server_api_ext.py`、鉴权、限流和 admin 文件已经在 `src/memos/`，不存在也不需要 `overlays/krolik/`。推荐做最小修复：

```diff
-# Apply Krolik overlay (AFTER base install to allow easy updates)
-COPY overlays/krolik/ ./src/memos/
```

同时更新文件头注释，说明扩展入口直接来自仓库源码。不要创建一个重复 overlay 目录，否则会产生两份实现和版本漂移。

构建验证：

```bash
docker build -f docker/Dockerfile.krolik -t memos:krolik-auth .
```

预期：不再因 `overlays/krolik/` 缺失失败，容器 healthcheck 通过。若维护者决定退役参考实现，替代方案是删除 `Dockerfile.krolik` 并同步删除中英文 `api_deployment.md` 的引用；删除文件需按 `AGENTS.md` 先获批。

### 任务 9：API key 管理与 schema 集成测试（M8、M9）

**文件：**

- 扩展：`tests/api/test_auth_schema.py`
- 扩展：`tests/api/test_admin_auth.py`

使用临时 PostgreSQL/CI service，执行 SQL 后覆盖完整生命周期：

1. 生成 key 后数据库仅存在 64 位 SHA-256 hash，不存在 `krlk_*` 明文。
2. `POST /admin/keys` 仅 master/admin scope 可访问，普通 read scope 返回 403。
3. 创建返回的 key 只出现一次；`GET /admin/keys` 仅返回 prefix 与元数据。
4. 新 key 可访问 `/product/search`。
5. `DELETE /admin/keys/{id}` 后相同 key 返回 401。
6. 已过期 key 返回 401；未来到期 key通过。
7. `last_used_at` 在成功认证后更新。
8. 数据库不可用时返回 401/可观测错误，不因异常放行。

`CreateKeyRequest.scopes` 当前不限制字符串枚举，本方案的 schema 因此只要求数组非空，不额外增加允许值约束。scope 枚举校验属于公开请求模型变更，明确排除在本轮之外。

### 任务 10：更新 OpenAPI 与全量回归（M1-M3、M9）

**文件：** 自动生成 `docs/openapi.json`

因为 `APIKeyHeader(name="Authorization")` 经 FastAPI `Security()` 引入，统一 router dependency 应在 `/product/*` operation 中生成 security requirement。执行：

```bash
make openapi
```

校验：

- `components.securitySchemes` 含 FastAPI 生成的 API key scheme（默认 scheme 名为 `APIKeyHeader`），其 `in` 为 `header`、`name` 为 `Authorization`。
- 25 个 `/product/*` operation 均引用该 scheme。
- `/health` 不含 security requirement。
- 除预期 security metadata 外，没有请求/响应 schema 或 path 漂移。

再依次运行：

```bash
poetry run pytest tests/api/test_auth.py -q
poetry run pytest tests/api/test_auth_router_mounts.py -q
poetry run pytest tests/api/test_auth_schema.py -q
poetry run pytest tests/docker/test_auth_compose.py -q
poetry run pytest tests/api/ -q
make format
make test
```

预期：全部命令退出码 0；Ruff 不产生未提交的二次格式化；全量测试无回归。

## 5. 鉴权测试用例清单

### 5.1 单元测试

- [ ] 环境缺省时 `AUTH_ENABLED=True`。
- [ ] 显式 `AUTH_ENABLED=false` 时无 key 仍按原逻辑放行。
- [ ] 鉴权开启时无 key 返回 401。
- [ ] 错误格式 key 返回 401。
- [ ] 不存在、吊销、过期 key 返回 401。
- [ ] `Bearer` 与 `Token` master key 均通过。
- [ ] 常规 `krlk_*` key hash 命中时通过。
- [ ] DB/pool 不可用时 fail-closed。
- [ ] 非受信来源、header 与内部 secret 都缺失时不发生空值旁路。
- [ ] 非受信来源携带正确非空 secret 时通过，错误 secret 返回 401。
- [ ] `INTERNAL_SERVICE_IPS` 白名单来源无需 header 仍按现有语义通过。
- [ ] 成功使用常规 key 更新 `last_used_at`。

### 5.2 入口与路由测试

- [ ] `server_api.py` 的所有 25 个 `/product/*` operation 都执行 `verify_api_key`。
- [ ] `server_api_ext.py` 的所有 25 个 `/product/*` operation 都执行 `verify_api_key`。
- [ ] 鉴权失败时业务 handler 零调用。
- [ ] 鉴权成功时请求继续到 handler。
- [ ] 两个入口的 `/health` 均公开。
- [ ] 扩展入口的 admin scope 与 rate limit 行为不回归。
- [ ] OpenAPI 的 25 个业务 operation 均声明 Authorization security scheme。

### 5.3 数据库与 admin 测试

- [ ] SQL 可在空 PostgreSQL 16 执行两次。
- [ ] 表字段、默认值、唯一约束、scope 非空约束和索引符合运行时代码。
- [ ] 创建、查询、列出、吊销 API key 生命周期成功。
- [ ] 数据库只存 hash/prefix，不存明文 key。
- [ ] read scope 无法调用 admin key 管理端点（403）。
- [ ] master/admin 可创建与列出 key；仅 master 可生成新 master key。

### 5.4 Docker 与配置测试

- [ ] 示例配置包含全部必需变量且无真实 secret/弱默认值。
- [ ] Compose 未配置必填密码/hash 时直接失败。
- [ ] Neo4j、Qdrant、PostgreSQL 默认仅 loopback 暴露。
- [ ] PostgreSQL schema init 文件只读挂载并有健康检查。
- [ ] 基础 Dockerfile 构建后含 `psycopg2-binary`（当前 `requirements.txt` 已含）。
- [ ] Krolik Dockerfile 不再引用不存在的 overlay。

## 6. 发布与回滚策略

1. **先发布配置与数据库。** 在现有 PostgreSQL 执行幂等 schema；创建并安全保存 master key；填充 `MASTER_KEY_HASH`、数据库密码、内部 secret。
2. **影子验证。** 在非生产环境先以 `AUTH_ENABLED=false` 部署新版本，验证业务无回归、schema 连接正常和 OpenAPI 正确。
3. **创建客户端 key。** 按调用方最小权限发放 key，确认客户端使用 `Authorization: <key>` 或 `Bearer <key>`。
4. **开启鉴权。** 设置 `AUTH_ENABLED=true`，依次验证无 key 401、错误 key 401、master key 200、常规 key 200、内部服务正确 secret 200。
5. **监控。** 观察 401/403/429 比例、auth DB 连接池错误和客户端升级遗漏；日志不得包含完整 key/hash/secret。

紧急回滚只需显式设置 `AUTH_ENABLED=false` 并滚动重启 API，不需回滚 schema；这会恢复未鉴权状态，因此只能作为短时受控回滚，并应同时把 API 入口限制在 loopback/VPN/反向代理访问控制之后。端口收紧与密码更换不应随应用回滚撤销。

## 7. 验收清单

### 7.1 M1-M9 覆盖

- [ ] M1：`server_api.py` 使用 `app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])`。
- [ ] M2：`server_router.py` 25 个 endpoint 无需逐项改动，入口测试与 OpenAPI 证明全部继承统一鉴权。
- [ ] M3：`server_api_ext.py` 使用同样的 `app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])` 一行式依赖。
- [ ] M4：`docker/.env.example` 包含 `AUTH_ENABLED`、`MASTER_KEY_HASH`、`POSTGRES_*`、`INTERNAL_SERVICE_SECRET` 及安全注释。
- [ ] M5：未设置 `AUTH_ENABLED` 时默认开启；只有显式 false 才绕过；空内部 secret 不再放行。
- [ ] M6：Compose 无 `12345678`，密码无弱默认值，存储端口默认绑定 `127.0.0.1`。
- [ ] M7：Krolik 构建不再引用不存在的 `overlays/krolik/`，或经批准退役该文件。
- [ ] M8：`api_keys` schema 可执行、幂等，并纳入 PostgreSQL 首次启动路径。
- [ ] M9：鉴权、两个入口、数据库生命周期和 Docker 静态测试均通过。

### 7.2 行为验收

- [ ] `AUTH_ENABLED=false`：现有 `/product/*` 成功路径不回归。
- [ ] `AUTH_ENABLED=true` + 无 key：401，业务 handler 不执行。
- [ ] `AUTH_ENABLED=true` + 错误 key：401。
- [ ] `AUTH_ENABLED=true` + master key：200。
- [ ] `AUTH_ENABLED=true` + 有效普通 key：200；吊销/过期后 401。
- [ ] 受信内部来源按白名单通过；非受信来源只有携带正确非空 secret 才能走内部共享密钥路径。
- [ ] auth DB 不可用时拒绝普通 key，不发生 fail-open。
- [ ] `/health` 仍可供容器和负载均衡器无凭据探活。

### 7.3 工程验收

- [ ] `poetry run pytest tests/api/test_auth.py -q` 通过。
- [ ] `poetry run pytest tests/api/test_auth_router_mounts.py -q` 通过。
- [ ] `poetry run pytest tests/api/test_auth_schema.py -q` 通过。
- [ ] `poetry run pytest tests/api/test_admin_auth.py -q` 通过。
- [ ] `poetry run pytest tests/docker/test_auth_compose.py -q` 通过。
- [ ] `poetry run pytest tests/api/ -q` 通过。
- [ ] `make openapi` 成功且 diff 仅含预期鉴权 metadata。
- [ ] `make format` 通过。
- [ ] `make test` 通过。
- [ ] `docker compose --env-file .env -f docker/docker-compose.yml config` 通过。
- [ ] 基础与 Krolik 两个 Dockerfile 构建成功。
- [ ] `git diff --check` 无空白错误，`git status` 不含 `.env`、secret、日志或临时文件。

## 8. 实施前审批点

根据仓库 `AGENTS.md`，开始编码前需一次性确认以下边界：

1. 修改 `src/memos/api/` 会改变公开业务路由的 security requirement，并生成 `docs/openapi.json`，需批准。
2. 新增 `api_keys` 表及 PostgreSQL init schema 属于 DB schema 变更，需批准。
3. `CreateKeyRequest.scopes` 枚举收紧属于公开请求模型变更，本轮明确不实施；后续若需要必须单独批准。
4. 本方案不要求修改 `pyproject.toml`：默认 Docker `requirements.txt` 已有 `psycopg2-binary==2.9.11`。若要让非 Docker 的 `MemoryOS` 安装也正式支持该鉴权后端，应另行申请在合适 optional extra 中加入驱动，并同步 lockfile。

---

方案完成后，建议按任务 1-10 顺序实施，每个任务保持可测试、可审查的小提交；任何时候不得通过 `--no-verify`、提交真实 `.env`，或把完整 API key 写入测试输出。
