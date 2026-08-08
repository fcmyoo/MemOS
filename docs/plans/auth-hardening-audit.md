# MemOS 远程部署鉴权加固 — 缺失点审计清单

> 审计人：Hermes Agent（读码核实，非推测）
> 审计日期：2026-08-08
> 仓库版本：8d310a7a（v2.0.27 "Stardust" + 最新 main）
> 目的：为 Codex 方案阶段提供完整、可核实的事实输入

## 一、背景

MemOS 是 MemTensor 开源的 LLM 记忆操作系统，提供 `/product/*` REST API
（search/add/create_cube/scheduler 等 25 个端点）。官方文档建议公开部署用
`server_api.py`，但该入口**无鉴权**。鉴权基础设施代码已存在但未接入业务路由。

## 二、缺失点清单（全部经源码核实）

### M1. 默认入口 server_api.py 未启用鉴权（严重）
- 文件：`src/memos/api/server_api.py`
- 事实：仅 `app.include_router(server_router_module.router)`，无任何鉴权依赖；
  不挂 admin_router；无 rate limit；无安全响应头
- 影响：任何人可调 `/product/*` 全部 25 个端点

### M2. server_router.py 25 个业务端点全部无鉴权（严重）
- 文件：`src/memos/api/routers/server_router.py`
- 事实：grep 结果为空 —— 该文件没有 `Depends(verify_api_key)` / `require_scope` 任何引用
- 影响：即便 AUTH_ENABLED=true，业务端点依然裸奔（auth.py 只保护 admin_router）

### M3. server_api_ext.py 业务端点同样裸奔（严重）
- 文件：`src/memos/api/server_api_ext.py`
- 事实：挂了 CORS/SecurityHeaders/RateLimit/admin_router，但
  `app.include_router(server_router_module.router)` 同样没挂 dependencies
- 影响：rate limit 挡不住未授权调用，业务端点仍无鉴权

### M4. .env.example 完全缺失鉴权配置项（中等）
- 文件：`docker/.env.example`（49 行）
- 事实：只有 LLM/embedder/Neo4j 配置，**没有任何** AUTH_ENABLED /
  MASTER_KEY_HASH / POSTGRES_* / INTERNAL_SERVICE_SECRET 项
- 影响：部署者无从知晓有鉴权开关，默认全开

### M5. AUTH_ENABLED 默认 false（中等）
- 文件：`src/memos/api/middleware/auth.py:26`
- 事实：`AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false")`，不开 = verify 直接放行
- 影响：忘了设 env 就裸奔

### M6. docker-compose.yml 弱密码 + 端口全映射（中等）
- 文件：`docker/docker-compose.yml:42`
- 事实：`NEO4J_AUTH: "neo4j/12345678"`；Neo4j 7474/7687、Qdrant 6333/6334 全部映射 0.0.0.0
- 影响：内网横向渗透风险

### M7. Dockerfile.krolik 引用不存在的 overlays/krolik/（低）
- 文件：`docker/Dockerfile.krolik`
- 事实：`overlays/` 目录在仓库根不存在，直接构建必失败

### M8. api_keys 表无 schema 初始化文件（中等）
- 事实：`auth.py` / `admin_router.py` 查询 `api_keys` 表（key_hash/key_prefix/
  user_name/scopes/description/expires_at/is_active/last_used_at/created_by），
  但仓库内**搜不到 CREATE TABLE 语句**，也没有 migrations 目录
- 影响：部署者不知道要建什么表，鉴权开启后连库会挂

### M9. 鉴权零测试覆盖（低）
- 事实：`tests/` 下没有 auth 相关测试文件
- 影响：改动无回归保障

## 三、已存在的可用资产（方案可复用，勿重复造轮子）

| 资产 | 路径 | 说明 |
|---|---|---|
| 鉴权中间件 | `src/memos/api/middleware/auth.py` | verify_api_key / require_scope(admin/read/write) / master key / 内部服务白名单，完整可用 |
| API key 工具 | `src/memos/api/utils/api_keys.py` | 生成 krlk_* key / master key / 建库 / 吊销 / 列表 |
| admin 路由 | `src/memos/api/routers/admin_router.py` | 5 端点已挂鉴权：POST/GET/DELETE /keys、GET/POST /health 等 |
| 扩展入口模板 | `src/memos/api/server_api_ext.py` | RateLimit + SecurityHeaders + CORS + admin 路由的正确挂法参照 |
| rate limit 中间件 | `src/memos/api/middleware/rate_limit.py` | 已存在 |

## 四、修复方向（给 Codex 的约束）

1. **一行式修复**：`include_router(server_router.router, dependencies=[Depends(verify_api_key)])`
   给全部业务端点统一挂鉴权 —— 必须同时应用于 server_api.py 与 server_api_ext.py
2. 补 .env.example 鉴权配置段（AUTH_ENABLED / MASTER_KEY_HASH / POSTGRES_* /
   INTERNAL_SERVICE_SECRET）
3. 提供 api_keys 表 schema（SQL 文件或初始化脚本），并纳入启动路径
4. 收紧 docker-compose（强密码、端口绑定 127.0.0.1 或注释）
5. 补鉴权测试（至少：无 key 401、错误 key 401、master key 通过、内部服务白名单通过）
6. 不引入新依赖；不动现有鉴权中间件逻辑；兼容 AUTH_ENABLED=false 的本地开发场景
7. Dockerfile.krolik 问题只需在方案中标注处置建议（修复或删除），不强制改

## 五、验收标准（Codex 方案需包含自检清单）

- [ ] 两个入口文件 25 个业务端点全部挂上 verify_api_key
- [ ] AUTH_ENABLED=false 时现有功能不回归（本地开发照常）
- [ ] AUTH_ENABLED=true 时：无 key → 401；错 key → 401；master key → 200
- [ ] .env.example 含完整鉴权配置段
- [ ] api_keys 表有可执行的 schema 文件
- [ ] 鉴权测试通过（pytest 或等价）
