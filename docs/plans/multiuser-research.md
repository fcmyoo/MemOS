# MemOS 多用户体系调研报告（feat/api-auth-hardening 分支）

> 调研人：Hermes Agent | 日期：2026-08-09
> 基于：D:\code\invest\MemOS @ feat/api-auth-hardening（6f37591f）

## 一、结论速览

**有完整的多用户体系**，分两层：
1. **业务用户层**（`mem_user/`）：User/Cube/Role 模型，管理"谁在用记忆"
2. **API 访问层**（`api/middleware/auth.py` + `api/utils/api_keys.py`）：API key 绑定用户，
   控制"谁在调用接口"

两层通过 `user_name` 关联。多租户隔离靠 **Cube（记忆立方体）** 实现。

## 二、业务用户层（mem_user）

### 数据模型（SQLite/MySQL/Redis 均可）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | user_id (UUID), user_name (unique), role, is_active | 用户 |
| `cubes` | cube_id, cube_name, owner_id | 记忆立方体（租户单元） |
| `user_cube_association` | user_id + cube_id | 用户↔立方体 多对多 |

### 角色体系 `UserRole`（user_manager.py:37）

```
ROOT → ADMIN → USER → GUEST
```

### 核心方法（UserManager）

| 方法 | 功能 |
|---|---|
| `create_user(name, role)` | 建用户（重名返回已有） |
| `get_user / get_user_by_name / list_users` | 查询 |
| `create_cube / get_cube / delete_cube` | 立方体管理 |
| `validate_user_cube_access(user, cube)` | **权限校验：用户能否访问该立方体** |
| `add_user_to_cube / remove_user_from_cube` | 用户-立方体授权 |
| `delete_user` | 删用户 |

### 持久化选择

| 后端 | 文件 | 适用 |
|---|---|---|
| SQLite | `user_manager.py` + `persistent_user_manager.py` | 单机默认 |
| MySQL | `mysql_user_manager.py` | 多实例 |
| Redis | `redis_persistent_user_manager.py` | 轻量缓存型 |

## 三、API 访问层（鉴权 + key 分配）

### 凭证类型（三种）

| 类型 | 格式 | 用途 | 存储 |
|---|---|---|---|
| **Master Key** | `mk_<64hex>` | 管理员操作（签发/吊销 key） | 只存 SHA-256 到 `MASTER_KEY_HASH` env |
| **API Key** | `krlk_<64hex>` | 业务调用（/product/*） | SHA-256 存 PostgreSQL `api_keys` 表 |
| **Internal Secret** | 任意串（`INTERNAL_SERVICE_SECRET` env） | 容器间/内部服务互调 | 环境变量 + `X-Internal-Service` 头 |

> 你问的 "banner/token" —— 分支里没有独立的 banner 概念；请求头统一用
> `Authorization: Bearer <key>` 或 `Authorization: Token <key>`（auth.py 两种前缀都认），
> 另有兼容头 `X-API-Key`（CORS 白名单已放行）。trace 用 `X-Trace-Id` 等。

### API key 生命周期（admin_router.py）

| 端点 | 方法 | 权限 | 功能 |
|---|---|---|---|
| `/admin/keys` | POST | admin scope 或 master key | 签发 key（指定 user_name/scopes/过期） |
| `/admin/keys` | GET | admin | 列出（只返回 prefix，不泄明文） |
| `/admin/keys/{id}` | DELETE | admin | 吊销 |
| `/admin/health` | GET | 公开 | 健康检查 |

### 签发 key 请求体（CreateKeyRequest）

```json
{
  "user_name": "alice",          // 绑定哪个业务用户
  "scopes": ["read", "write"],   // 权限范围
  "description": "web app",
  "expires_in_days": 90          // 过期（1-365，可空=永久）
}
```

### Scope 权限模型

```
all    → 全部（master key / 内部服务）
admin  → 管理端点
write  → 写操作（add/create_cube 等）
read   → 只读（search 等，key 默认值）
```

`require_scope()` 依赖工厂实现（auth.py:240）：`"all"` 或包含所需 scope 即通过。

## 四、多用户工作流（怎么用）

### 场景：给 3 个用户分配独立 key

```
1. 启动 PostgreSQL + 执行 001_api_keys.sql（建 api_keys 表）
2. 配置 AUTH_ENABLED=true + MASTER_KEY_HASH（master key 哈希）
3. 用 master key 调 POST /admin/keys 三次：
   → alice:  scopes=["read","write"], expires_in_days=90
   → bob:    scopes=["read"],         expires_in_days=30
   → carol:  scopes=["read","write"], expires_in_days=365
4. 每个用户拿自己的 krlk_ 明文 key（只在签发响应里出现一次！）
5. 调用：Authorization: Bearer krlk_xxx
```

### 隔离模型

- **Key 层面**：每个 key 绑定 user_name，鉴权通过后 `auth["user_name"]` 注入请求上下文
- **数据层面**：Cube 是隔离单元 —— 用户只能访问 `validate_user_cube_access` 通过的立方体；
  search 请求的 `mem_cube_id` 会被校验，未授权的 cube 拒绝访问（search_handler.py:1031）
- **推荐**：每个租户一个 cube + 各自 user_name 的 key

## 五、安全设计要点（本分支加固后）

1. **Fail-closed**：`AUTH_ENABLED` 默认 true；鉴权 DB 故障时拒绝（不放行）
2. **明文不落库**：key 只存 SHA-256；明文仅签发时返回一次
3. **防时序攻击**：内部 secret 用 `hmac.compare_digest`
4. **过期机制**：`expires_at`（TIMESTAMPTZ）+ 索引，过期即拒
5. **吊销即时生效**：`is_active=false` 后 lookup 返回 None → 401
6. **弱密码清零**：compose 不再有 neo4j/12345678

## 六、多用户场景的待办/建议

| 事项 | 现状 | 建议 |
|---|---|---|
| 用户注册/登录界面 | 无（仅 API 层管理） | 如需自助注册，需自建或用 memos-local-plugin 的前端 |
| 每个用户独立 cube 的自动创建 | create_cube 是 API 手动调 | 可在创建用户流程里联动建 cube |
| key 数量上限/配额 | 无限制 | 建议按 user 加配额（后续迭代） |
| 审计日志 | key 有 last_used_at，无完整审计 | 生产环境建议加 access log |
| key 轮换提醒 | 无 | 过期前通知可后续做 |

## 七、参考文件

- `src/memos/mem_user/user_manager.py` — 用户/角色/立方体模型 + 管理
- `src/memos/api/middleware/auth.py` — 鉴权中间件（verify_api_key/require_scope）
- `src/memos/api/utils/api_keys.py` — key 生成/签发/吊销/列表
- `src/memos/api/routers/admin_router.py` — /admin/* 管理端点
- `docker/postgres/init/001_api_keys.sql` — api_keys 表 DDL
- `src/memos/api/middleware/request_context.py` — 请求上下文（trace_id/user_name 注入）
