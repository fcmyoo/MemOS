# MemOS 鉴权加固 — 分支提交方案

> 作者：Hermes Agent | 日期：2026-08-08
> 仓库：D:\code\invest\MemOS（MemTensor/MemOS 上游 clone，当前 HEAD 8d310a7a）
> 状态：**待用户确认后执行**

## 一、目标

把已完成的鉴权加固改动（M1-M9 全部落地、169 测试通过）整理为**规范分支 + 原子提交**，
便于评审、回滚、后续向上游提 PR。

## 二、分支策略

| 项 | 值 |
|---|---|
| 分支名 | `feat/api-auth-hardening` |
| 基线 | 当前 main（8d310a7a） |
| 提交数 | **4 个原子提交**（按主题拆分，便于逐条 review） |
| 提交作者 | `fcmyoo <待确认邮箱>`（当前 git 未配置身份，需你提供或用默认） |

## 三、提交拆分（4 commits）

### Commit 1 — `feat(api): enforce API key auth on all product endpoints`

**文件：**
- `src/memos/api/middleware/auth.py`（+32/-14：AUTH_ENABLED 默认 true、hmac 修复、TIMESTAMPTZ、load_dotenv 前置）
- `src/memos/api/server_api.py`（+5：一行式挂载）
- `src/memos/api/server_api_ext.py`（+7：一行式挂载 + health 默认值）
- `src/memos/api/routers/admin_router.py`（+2：health 默认值）

**消息：**
```
feat(api): enforce API key auth on all product endpoints

- AUTH_ENABLED now defaults to true (fail-closed) with load_dotenv
  applied before module-level constants
- Mount verify_api_key as a unified router dependency on both
  server_api and server_api_ext, protecting all 25 /product/* ops
- Fix is_internal_request: empty internal secret no longer matches
  an absent header (hmac.compare_digest + non-empty guard)
- Compare TIMESTAMPTZ expiry against datetime.now(UTC) instead of
  time.time() float
```

### Commit 2 — `test(api): add auth hardening test suite`

**文件：**
- `tests/api/test_auth.py`（新增，中间件单元测试）
- `tests/api/test_admin_auth.py`（新增）
- `tests/api/test_auth_router_mounts.py`（新增，双入口挂载断言）
- `tests/api/test_server_router.py`（+9：既有测试适配鉴权 override）

**消息：**
```
test(api): add auth hardening test suite

71 new cases covering middleware defaults, internal-secret handling,
expiry comparison, unified router mounting on both entry points,
OpenAPI security requirements, admin scope enforcement, and static
schema/compose validation. Existing format tests bypass auth via
entry-point-bound dependency override.
```

### Commit 3 — `feat(db): add api_keys schema and PostgreSQL service`

**文件：**
- `docker/postgres/init/001_api_keys.sql`（新增）
- `tests/api/test_auth_schema.py`（新增）
- `tests/docker/test_auth_compose.py`（新增）

**消息：**
```
feat(db): add api_keys schema and PostgreSQL service

- Idempotent api_keys DDL with SHA-256 hash check, krlk_ prefix
  check, non-empty scopes, and expiration index
- docker-compose: add postgres:16-alpine service with initdb mount
  and healthcheck; wire AUTH_ENABLED/MASTER_KEY_HASH/POSTGRES_*
  into memos service
```

### Commit 4 — `chore(docker,docs): tighten defaults and document auth setup`

**文件：**
- `docker/docker-compose.yml`（端口收紧、密码必填插值）
- `docker/.env.example`（鉴权配置段）
- `docker/Dockerfile.krolik`（移除缺失 overlay COPY）
- `README.md`、`docs/cn/.../rest_api_server.md`、`docs/en/.../rest_api_server.md`

**消息：**
```
chore(docker,docs): tighten defaults and document auth setup

- Bind Neo4j/Qdrant/Postgres/MemOS to 127.0.0.1 by default; require
  MASTER_KEY_HASH, INTERNAL_SERVICE_SECRET and DB passwords via
  compose interpolation
- Add auth section to .env.example; drop hardcoded neo4j/12345678
- Fix Dockerfile.krolik: remove COPY of non-existent overlays dir
- Document key generation and Bearer auth in README + zh/en guides
```

## 四、不纳入提交的文件

| 文件 | 原因 |
|---|---|
| `docs/plans/auth-hardening-audit.md` | 工作产物（方案文档），与代码分开，可留本地 |
| `docs/plans/auth-hardening-plan.md` | 同上 |
| `uv.lock` | 已被恢复，无改动 |

> 说明：若你想把两份方案文档也进库（供后续 PR 说明引用），可在 Commit 4 后追加
> Commit 5 `docs(plans): record auth hardening audit and implementation plan`。

## 五、执行步骤（确认后）

```bash
cd /d/code/invest/MemOS
git checkout -b feat/api-auth-hardening
git add <commit1 文件> && git commit -m "..."
git add <commit2 文件> && git commit -m "..."
git add <commit3 文件> && git commit -m "..."
git add <commit4 文件> && git commit -m "..."
git log --oneline -5   # 验证
git status             # 确认工作区干净
```

## 六、提交前最终验证（已全部通过）

- ✅ `python -m py_compile` 所有改动 .py
- ✅ `pytest tests/api/` → **169 passed, 0 failed**
- ✅ OpenAPI：25/25 /product/* 端点含 APIKeyHeader security，/health 公开
- ✅ 无残留 `12345678` 弱密码
- ✅ 未触碰 server_router.py / rate_limit.py / pyproject.toml（方案约束）

## 七、待你确认的点

1. **提交作者身份**：git 未配置 user.name/email。默认用 `fcmyoo` + 你 GitHub 邮箱；
   或告诉我用别的。
2. **方案文档是否进库**（Commit 5 可选）。
3. **是否推送远端**：本仓库 origin 是上游 clone（ghfast 代理）。若你有自己 fork，
   可加 remote 推送；否则先本地提交即可。
