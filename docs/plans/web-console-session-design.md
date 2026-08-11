# Web 控制台持久会话与独立前端仓库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变既有 `mk_*`/`krlk_*`、业务 API 与 Cube ACL 语义的前提下，为 Web 控制台增加刷新后仍保持登录、可轮换且可服务端吊销的持久会话，并明确前端独立仓库的契约、CI 和发布流程。

**Architecture:** 控制台采用服务端保存的 opaque access/refresh 双 token；两个 token 都通过 JSON/`Authorization` 显式传递，不使用 cookie。浏览器把最小 token bundle 持久化到 `localStorage`，后端在共享 SQLite 中保存哈希并用事务完成 refresh 轮换、并发控制和重放检测。前端作为独立仓库和制品发布，通过固定的 console OpenAPI v1 契约与 MemOS 后端协调版本。

**Tech Stack:** FastAPI、SQLAlchemy、SQLite/WAL、Argon2id、Vue 3.5、Vite 8、TypeScript 6、Pinia 4、Vue Router 4、Nuxt UI 4、Tailwind CSS 4、Vitest、Playwright。

---

## 0. 文档地位与现状约束

本文是 `docs/plans/web-console-revamp.md` 的增量覆盖方案。发生冲突时，以本文为准；未列为覆盖的注册、RBAC、Cube、API key、CORS、技术栈和部署决策继续沿用上一版。

源码基线决定了以下边界：

1. `src/memos/api/middleware/auth.py` 的 `verify_api_key()` 只处理 master key、`krlk_*`、内部请求和 `AUTH_ENABLED=false` bypass。Web token 不得进入该依赖，也不得获得 `is_master_key`/`auth_bypassed`/`is_internal`。
2. `src/memos/api/server_api.py` 当前把业务 router 统一挂在 `verify_api_key()` 下，`admin_router` 仅在 `AUTH_ENABLED=true` 时挂载；新增 `auth_router`/`me_router` 必须独立挂载。
3. `src/memos/api/routers/admin_router.py` 与 `src/memos/api/utils/api_keys.py` 的 `mk_*`/`krlk_*` 规则不变。特别是 API key 仍只在创建时返回一次，PostgreSQL 只存其 SHA-256。
4. `src/memos/mem_user/user_manager.py` 当前使用单个 `memos_users.db` 文件，尚无密码列和 `web_sessions`。多 worker 必须通过同一个 SQLite 文件、WAL 和数据库事务共享状态，不能依赖进程内 session map 或锁。
5. 本文只产出方案。实现阶段会涉及公开路由、OpenAPI、SQLite schema 和可选依赖，必须先按 `AGENTS.md` 分别取得批准。

## 1. 会话模型最终设计

### 1.1 选择：opaque access + refresh 双 token

最终选择是服务端有状态的 opaque 双 token，而不是单 token 滑动过期。

| 方案 | 结果 | 取舍 |
|---|---|---|
| **短期 access + 长期 refresh，服务端保存哈希** | **采用** | access 泄漏窗口短；refresh 只用于 `/auth/refresh`；两者均可即时吊销；轮换与重放检测边界清楚；适合单机共享 SQLite。 |
| 单 opaque token + sliding session | 不采用 | 每次普通请求都可能写 SQLite 延长过期时间，放大多 worker 写锁竞争；长期 token 同时用于全部控制台 API，泄漏后的权限与存活窗口更大；重放与正常并发更难区分。 |
| JWT access + refresh | 不采用 | access JWT 在到期前难以即时吊销，或需要 denylist，抵消无状态收益；当前单机 SQLite 没有引入 JWT 的必要。 |
| HttpOnly refresh cookie + 内存 access | v1 不采用 | XSS 抗窃取更强，但改变了“非 cookie、Authorization header”的既定认证载体，并重新引入跨 origin cookie、CSRF、SameSite 和反代配置复杂度。 |

access 与 refresh 都是 32 字节 CSPRNG secret 的 opaque token，使用不同前缀，建议格式：

```text
wca_<128-bit-session-selector>.<256-bit-secret>
wcr_<128-bit-session-selector>.<256-bit-secret>
```

- `wca_` 只允许出现在控制台路由的 `Authorization: Bearer` 中；`wcr_` 只允许出现在 `/auth/refresh`、过期 access 下的 `/auth/logout` 请求体中。
- selector 只是高熵查询键，不授予权限；服务端存其 SHA-256。token 整串再做 SHA-256 后比较，数据库不保存明文。
- token 本身有至少 256 bit 熵，不用 Argon2id。Argon2id 继续只用于低熵密码；对随机 token 使用 SHA-256 可在每次请求低成本查询，同时不降低穷举安全性。
- 每次登录/注册创建一条独立 session family；一次 refresh 同时签发新 access 和新 refresh。

### 1.2 明确边界

- refresh 不是无限滑动：每个 family 有固定 30 天绝对截止时间，轮换不延长该时间。到期后才因自然过期要求重新登录。
- 以下不是“自然过期”，但必须强制重新认证：用户主动登出、退出所有设备、改密、账号停用/删除、管理员撤销、refresh 重放检测。
- access 只访问 `/auth/me`、`/me/*`、允许 session principal 的 `/admin/*`；不能访问现有业务 router，不能自动兑换 `krlk_*`。
- v1 目标是同一主机上的多 Uvicorn worker 共享本地 SQLite 文件。多主机/多容器同时写同一网络文件系统不在边界内；横向扩展到多主机前应迁移 session store 到 PostgreSQL/Redis。
- access 校验是读 SQLite；`last_seen_at` 只在 refresh 时更新，不在每个 API 请求上写库，从而避免把读请求变为 SQLite 写热点。

## 2. 持久化存储与页面恢复

### 2.1 localStorage 的最终用法

采用 `localStorage`，只保存一个可原子替换的版本化 envelope：

```ts
type PersistedAuthV1 = {
  schemaVersion: 1
  accessToken: string
  accessExpiresAt: string
  refreshToken: string
  refreshExpiresAt: string
  rotation: number
}

const STORAGE_KEY = 'memos.console.auth.v1'
```

Pinia 保存同一份运行时状态及 `/auth/me` 返回的用户摘要。用户摘要无需持久化，页面恢复后重新读取。不要持久化密码、完整 `mk_*`/`krlk_*`、一次性 key、请求头、用户内容或后端错误详情。

选择同时持久化 access 和 refresh，而不是只持久化 refresh，理由是：

- 刷新页面时，只要 access 仍有足够剩余时间，就可直接验证 `/auth/me`，不会把每次页面刷新都变成 refresh 轮换和 SQLite 写操作。
- refresh 已经能够签发新 access；在同一 `localStorage` 安全边界里，仅把 access 留在内存并不能实质降低成功 XSS 的危害。
- 单 JSON envelope 用一次 `setItem()` 替换，避免分别写两个键导致只更新一半；解析失败、字段缺失、前缀不符或时间无效时整体清除。

这是明确接受的风险：同源任意 JavaScript 都能读取 `localStorage`，因此持久 XSS 可窃取 refresh token。该选择服务于“刷新不重登录”和“非 cookie header 认证”两个已确认约束；安全控制重点转为严格缩小 XSS 面、缩短 access TTL、轮换 refresh 和即时吊销。

### 2.2 XSS 与内容安全控制

1. **Vue DOM 边界**：ESLint 全局启用 `vue/no-v-html: error`，业务组件不得用 `v-html`、`innerHTML`、`outerHTML`、`insertAdjacentHTML` 或动态模板编译。API 文本、错误、key、用户名只通过 Vue 插值或 `textContent` 输出。
2. **Markdown**：`markdown-it` 固定 `html: false`，对 `parse()` token 做允许列表并映射为 Vue VNode；只支持段落、标题、列表、引用、强调、代码、受限链接和数学 token，不把 `render()` 生成的 HTML 字符串交给 DOM。链接协议只允许 `https:`,`http:`,`mailto:`，外链强制 `target="_blank" rel="noopener noreferrer"`。
3. **KaTeX**：使用组件 `ref` 调用 KaTeX DOM 渲染，`trust: false`、`strict: 'error'`，禁止 `\href`/`\html*` 等信任命令；不使用 `v-html`。
4. **第三方脚本**：控制台 origin 不加载 CDN script、标签管理器、在线客服、广告或任意远程模块；所有运行时代码进入锁定的构建产物。依赖使用 lockfile、Dependabot/Renovate 和 CI 审计。
5. **CSP**：由 Nginx/Caddy/CDN 按部署的 API origin 下发，先 report-only 验证，再强制执行。基线如下；不允许 `unsafe-eval` 或 script `unsafe-inline`：

   ```text
   default-src 'self';
   script-src 'self';
   style-src-elem 'self';
   style-src-attr 'unsafe-inline';
   img-src 'self' data:;
   font-src 'self';
   connect-src 'self' https://api.example.com;
   object-src 'none';
   base-uri 'none';
   frame-ancestors 'none';
   form-action 'self';
   worker-src 'self';
   manifest-src 'self';
   upgrade-insecure-requests
   ```

   若 Nuxt UI/KaTeX 验证后不需要 style attribute，则进一步把 `style-src-attr` 收紧为 `'none'`。生产 CSP 不由用户可控字符串拼接。
6. **凭证外泄面**：token 不进入 URL、query、fragment、日志、toast、错误监控、analytics、OpenAPI example 或浏览器标题；响应带 `Cache-Control: no-store` 和 `Pragma: no-cache`；Referrer Policy 使用 `no-referrer`。

### 2.3 刷新页面恢复登录的完整流程

```text
main.ts 启动
  └─ authStore.bootstrap()（router 挂载前必须 await）
      ├─ localStorage 无 envelope → anonymous → 登录/注册页
      ├─ envelope 非法或 refresh 已绝对过期 → 清空 → 登录页
      ├─ access 剩余 > 2 分钟
      │   └─ 装入 Pinia → GET /auth/me
      │       ├─ 200 → authenticated → 按 role 放行
      │       └─ 401 → 进入 single-flight refresh
      └─ access 缺失/已过期/剩余 ≤ 2 分钟
          └─ POST /auth/refresh
              ├─ 200 → 原子替换 envelope → GET /auth/me → authenticated
              ├─ 401 终态 → 清空 → 登录页
              └─ 网络/429/5xx → 保留 envelope → 显示“重试/离线”，不误判登出
```

- 路由 guard 在 `bootstrap()` 完成前只显示启动页，不得先跳登录再回跳。
- 浏览器 `storage` 事件和 `BroadcastChannel('memos-console-auth')` 同步登录、轮换与登出；另一标签页收到 logout 时立即清内存并停掉待发请求。
- 用户点击登录/注册后，先将完整 token envelope 持久化，再进入受保护路由；`/auth/me` 失败时按上述 refresh/终态规则处理。

## 3. 后端精确 diff 与 `/auth/refresh`

### 3.1 `web_sessions` 最终表

上一版的单 token 表由以下最终模型替换。字段名是实现契约，不保存任何 token 明文：

```sql
CREATE TABLE web_sessions (
    session_id_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    access_token_hash TEXT NOT NULL UNIQUE,
    access_expires_at DATETIME NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    previous_refresh_token_hash TEXT UNIQUE,
    previous_refresh_valid_until DATETIME,
    refresh_expires_at DATETIME NOT NULL,
    rotation_counter INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    last_refreshed_at DATETIME,
    revoked_at DATETIME,
    revoke_reason TEXT
);

CREATE INDEX idx_web_sessions_user_active
ON web_sessions(user_id, revoked_at, refresh_expires_at);
```

约束与迁移：

- 从当前仓库实现时，密码字段与上述完整 session 表一次迁移到 `PRAGMA user_version=2`；不要先落上一版草案的单 token 表。若部署环境已人工落过草案 v2，则另写 v2→v3 迁移，不得假设列存在。
- 初始化连接设置 `foreign_keys=ON`、`journal_mode=WAL`、`busy_timeout=5000`、`check_same_thread=False`。迁移使用 `BEGIN IMMEDIATE`、`PRAGMA table_info` 和幂等建索引。
- 所有时间使用 UTC aware datetime；SQLite 统一序列化，比较由服务层使用同一个可注入 clock，测试不依赖真实等待。
- 登录时清理已过 `refresh_expires_at + 30 天` 的 revoked/expired 行，每次最多 100 行；启动时也做一次有界清理，不增加常驻 scheduler。

### 3.2 token 服务与认证依赖

| 文件 | 精确变化 |
|---|---|
| `src/memos/mem_user/user_manager.py` | 增加最终 `WebSession` ORM、幂等迁移、按 selector/hash 查询、原子 rotate、按 family/用户撤销、过期清理。注册+默认 Cube 事务保持上一版设计。 |
| `src/memos/api/web_auth.py` | 增加 token 生成/解析/哈希、Argon2id 密码服务、`SessionService.issue_pair/validate_access/rotate_refresh/revoke*`；clock 与 token generator 可注入。 |
| `src/memos/api/middleware/auth.py` | 保留 `verify_api_key()` 原样；新增独立 `WebPrincipal` 与 `verify_web_access_token()`，只识别 `wca_`，写 `request.state.web_principal`。 |
| `src/memos/api/console_models.py` | 定义 token pair、refresh/logout/capabilities 请求响应；Pydantic 字段标为 sensitive，schema 不带真实 example。 |
| `src/memos/api/routers/auth_router.py` | register/login 返回双 token；新增 refresh、logout、logout-all、capabilities。 |
| `src/memos/api/server_api.py` | `AUTH_ENABLED=true` 时独立挂载 auth/me/扩展 admin router；业务 router 仍只依赖 `verify_api_key()`；CORS 保持上一版的显式 allowlist 和 `allow_credentials=False`。 |
| `src/memos/api/routers/admin_router.py`、`src/memos/api/utils/api_keys.py` | 只沿用上一版 RBAC/owner-safe key 改动；refresh 不写 PostgreSQL `api_keys`。 |

access 认证每次检查：格式与 selector、token hash 常量时间比较、`revoked_at IS NULL`、`access_expires_at > now`、关联用户 active。失败返回稳定 401 code：`access_token_invalid`、`access_token_expired` 或 `session_revoked`，且带 `WWW-Authenticate: Bearer`；不泄露用户/session 是否存在。

### 3.3 `/auth/refresh` 契约

```http
POST /auth/refresh
Content-Type: application/json

{"refresh_token":"wcr_..."}
```

该路由不要求 access token，也不读取 cookie。成功响应：

```json
{
  "access_token": "wca_...",
  "token_type": "Bearer",
  "access_expires_at": "2026-08-10T10:15:00Z",
  "refresh_token": "wcr_...",
  "refresh_expires_at": "2026-09-09T10:00:00Z",
  "rotation": 7
}
```

登录和注册使用同一 token pair 字段，不再使用含糊的单个 `expires_at`。refresh 限流为同一 selector + IP 每分钟 30 次；响应始终 `Cache-Control: no-store`。

精确事务顺序：

1. 解析 `wcr_`，计算 selector hash 与整 token hash；格式错误或 selector 不存在返回 `401 refresh_token_invalid`。
2. `BEGIN IMMEDIATE` 读取 family，检查用户 active、`revoked_at` 和固定 `refresh_expires_at`。
3. 当前 hash 匹配：进入正常轮换。前一 refresh hash 被移到 `previous_refresh_token_hash`，宽限截止设为 `now + 10 秒`。
4. previous hash 匹配且仍在 10 秒宽限内：视为丢包/合法并发恢复，再轮换一次；它不是无限宽限，每次只保留紧邻上一代。
5. selector 存在但既不匹配 current，也不匹配有效 previous：认定 refresh 重放，原子设置 `revoked_at/revoke_reason='refresh_reuse'`，提交后返回 `401 refresh_token_reused`。撤销范围是该 family，其他设备 session 不受影响。
6. 生成新 access/new refresh，只把新 hash 写入当前槽；`rotation_counter += 1`，更新 `last_seen_at/last_refreshed_at`，固定 `refresh_expires_at` 不延长。更新必须带旧状态 CAS 条件，rowcount 不为 1 时重新判定为并发/重放。
7. commit 后才返回明文 token；任何异常回滚。日志只记录 user_id 的不可逆摘要、session selector 前缀、rotation 和结果，不记录 token。

10 秒宽限解决浏览器多标签并发或 refresh 响应丢失后的单次恢复；代价是被窃取的上一代 refresh 在 10 秒内仍可能成功。因此前端仍必须使用跨标签互斥，宽限不能配置超过 30 秒。超过宽限的旧 token 一律触发重放撤销。

### 3.4 其他 auth 路由变更

| 方法/路径 | 认证与行为 |
|---|---|
| `POST /auth/register` | 沿用公开/invite/admin_only；成功返回 user、default_cube 和完整 token pair。 |
| `POST /auth/login` | 成功新建独立 family 并返回 token pair；每次登录独立于其他设备已有的 family。 |
| `POST /auth/logout` | 接受有效 access bearer；若 access 已过期，可在 body 传当前 refresh。解析到 family 后幂等撤销当前设备的 access+refresh，始终不返回 token。 |
| `POST /auth/logout-all` | 需有效 access（必要时前端先 refresh）；原子撤销该用户全部 family，204。 |
| `GET /auth/me` | 只接受 access；返回用户和 Cube 摘要，不刷新、不延长期限。 |
| `GET /auth/capabilities` | 公共、无用户数据；返回 `console_api_major=1`、minor 和 registration mode，供独立前端做兼容判断。不得改变 fingerprint-free `/health`。 |

前端退出流程先尽力调用 logout/logout-all，收到响应或网络失败后都立刻清除本地 access、refresh、Pinia 和 BroadcastChannel 状态。网络失败时服务端 token 可能仍有效，这是 header + localStorage 模式无法通过“清浏览器”替代服务端撤销的边界；UI 应提示“本机已退出，服务端撤销未确认”，但不得保留 token 自动重试。

## 4. 前端 API client：401 → refresh → 重放

### 4.1 精确算法

`src/api/http.ts` 保持唯一 `request<T>()` 入口，但新增以下状态：

```ts
let refreshPromise: Promise<TokenPair> | null = null
```

1. `authMode='session'` 时读取 Pinia 中的 access，注入 `Authorization: Bearer`。`apiKey` 和 `public` 模式绝不参与 refresh。
2. 发请求前若 access 剩余不超过 2 分钟，先调用 `refreshOnce()`；若还有充足时间则正常发送。
3. 收到 401 时，仅当请求是 session 模式、不是 auth login/register/refresh/logout、且 `_authRetry !== true` 时进入 refresh。403/409/422/429/5xx 不触发 refresh。
4. 同一标签页的并发请求共享 `refreshPromise`；只有第一个真正发 refresh，其余等待。
5. `refreshOnce()` 优先使用 `navigator.locks.request('memos-console-refresh')` 做同 origin 跨标签互斥。获得锁后重新读 `localStorage`：若 `rotation` 已增加，直接采用新 pair；否则用当前 refresh 调专用的 `rawFetchRefresh()`。不支持 Web Locks 时退化为标签页内 single-flight，并依赖 BroadcastChannel、storage event 与服务端 10 秒宽限收敛。
6. refresh 200 后，先用一次 `localStorage.setItem()` 原子替换 envelope，再更新 Pinia、广播 rotation；等待者读取新值。
7. 原请求用新 access **最多重放一次**，标记 `_authRetry=true`。wrapper 必须保存可重建的 method/headers/JSON body，而不是复用已消费的 `Request` stream；流式上传标为 `nonReplayable`，401 后提示用户重试，不自动重放。
8. refresh 的 `401 refresh_token_invalid/expired/revoked/reused` 是终态：清空全部本地认证状态、广播 logout、保存原目标 route，跳登录。重新登录成功后只恢复 GET 导航，不自动重复写操作。
9. refresh 的网络错误、429 或 5xx 不是 session 终态：保留 envelope，不跳登录，原请求返回可恢复错误并提供重试。client 不自动重试普通网络失败，避免重复写操作。
10. 重放后仍 401：清空并跳登录，不再递归 refresh。

后端必须保证认证依赖在 endpoint body 执行前返回 access 401；业务校验不得复用 401。这样对 JSON POST 的一次认证重放不会重复已执行的业务副作用。对未来无法保证此前置关系的写接口，必须增加 `Idempotency-Key` 后才允许自动重放。

### 4.2 多标签与多设备

- 同一浏览器 profile 的标签共享 envelope；Web Locks + rotation 复读避免拿旧 refresh 并发轮换。
- 不同浏览器/profile/设备各有独立 family。一个设备普通 logout/replay 只撤销本 family；logout-all、改密、停用用户撤销该用户全部 family。
- v1 不限制设备数量，但默认最多 20 个 active family/用户；第 21 次登录先撤销最早 `last_seen_at` 的 family，避免无界表增长。管理员可配置 1–100。
- v1 不增加设备指纹、session 枚举或按设备命名功能；它们不是刷新与多设备隔离的必要条件。用户通过当前设备 logout 或 logout-all 管理会话。

## 5. 过期窗口与清理参数

| 参数 | 默认值 | 可配置范围 | 行为 |
|---|---:|---:|---|
| access TTL | 15 分钟 | 5–60 分钟 | 从签发起固定；每次 refresh 发新 access。 |
| refresh 绝对 TTL | 30 天 | 1–90 天 | 从登录/注册创建 family 起固定，轮换不延长；到期要求重登录。 |
| 前端主动刷新阈值 | access 剩余 2 分钟 | 30 秒–5 分钟，且小于 access TTL | 请求前或 bootstrap 时触发；不使用常驻秒级轮询。 |
| clock skew | 30 秒 | 固定 | 前端按 `expires_at - 30s` 判断；服务端时间是最终依据。 |
| previous refresh 宽限 | 10 秒 | 0–30 秒 | 仅紧邻上一代可恢复；过窗重放撤销 family。 |
| refresh 限流 | 30 次/分钟/selector+IP | 10–120 | 防错误循环；429 不清本地 token。 |
| active family 上限 | 20/用户 | 1–100 | 超限撤销最旧 session，不影响其他用户。 |
| revoked/expired 保留 | refresh 到期后 30 天 | 7–90 天 | 保留审计/重放证据；之后有界清理。 |

登出清理矩阵：

| 事件 | 服务端 | 当前浏览器 | 其他设备 |
|---|---|---|---|
| 当前设备 logout | 撤销当前 family | 清 access、refresh、Pinia、广播状态 | 不影响 |
| logout-all | 撤销用户全部 family | 同上 | 下次请求/refresh 401 并清理 |
| 改密 | 提交密码事务后撤销全部 family | 当前请求 204 后清理 | 全部失效 |
| 停用/删除用户 | 撤销全部 family；access 校验同时检查 active | 清理 | 全部失效 |
| refresh 重放 | 撤销命中的 family，记录 `refresh_reuse` | 终态 401 后清理 | 其他 family 不影响 |
| 网络/5xx/429 | 不主动撤销 | 保留 token，允许重试 | 不影响 |

## 6. 前端独立仓库方案

### 6.1 仓库初始化清单

目标仓库示例为 `fcmyoo/memos-web-console`。上一版列出的 `package.json` 视为已存在，本次不重新脚手架；先核对版本，再补齐工程设施：

- [ ] 初始化独立 git history，默认分支 `main`、保护规则、CODEOWNERS、Apache-2.0 LICENSE、SECURITY.md、CONTRIBUTING.md、PR/issue 模板。
- [ ] 固定 Node `22.13+` 和唯一包管理器，在 `package.json` 增加 `packageManager`，提交 lockfile；CI 一律 frozen install。
- [ ] 补脚本：`typecheck`、`test:coverage`、`test:e2e`、`format:check`、`contract:check`、`build`；`build` 继续执行 typecheck。
- [ ] 增加 `.env.example`，只包含公开配置；推荐使用同源 `/config.js` 提供运行时 `apiBase`、locale 和 console API major，使同一 dist 可跨环境发布。禁止把 token/key/secret 编译进 `VITE_*`。
- [ ] 增加 `.github/workflows/ci.yml`：install → lint/format → typecheck → Vitest+coverage → contract check → build → Playwright smoke；上传 `dist` 和测试报告。
- [ ] 增加 `.github/workflows/release.yml`：仅 tag `v*`，复用已通过 CI 的 commit，产出 `dist.tar.gz`、checksum、SBOM 和只含静态站点的 OCI image；禁止从开发机手工发布。
- [ ] 增加 Dependabot/Renovate；依赖升级 PR 必须通过 XSS、CSP、构建体积与浏览器 smoke。
- [ ] README 写清本地开发、运行时配置、反代/直连 CORS、CSP、兼容矩阵、升级/回滚和安全报告入口。

建议目录：

```text
memos-web-console/
├── .github/
│   ├── workflows/{ci,release}.yml
│   ├── CODEOWNERS
│   └── dependabot.yml
├── contracts/
│   ├── memos-console-openapi-v1.json
│   └── README.md
├── deploy/
│   ├── Dockerfile
│   ├── nginx.conf.template
│   └── config.js.template
├── public/config.js
├── scripts/check-contract.mjs
├── src/
│   ├── api/{http,auth,admin,me,product}.ts
│   ├── auth/{storage,refreshCoordinator,bootstrap}.ts
│   ├── components/content/{MarkdownContent,KatexFormula}.vue
│   ├── stores/{auth,users,keys}.ts
│   ├── router/index.ts
│   ├── types/
│   └── ...上一版页面与布局
├── tests/{unit,contract,e2e}/
├── package.json
├── lockfile
├── README.md
├── SECURITY.md
└── LICENSE
```

### 6.2 前后端版本与契约协调

**路由前缀决定：v1 不给现有 FastAPI 路由强加 `/v1`。** 对外 `/api` 仍是部署反代前缀，不是版本号；控制台继续使用 `/auth/*`、`/me/*`、`/admin/*`，尤其是 `/auth/refresh`。原因是现有 25 个业务端点和上一版控制台契约都未版本化，此时全局改前缀会产生无收益的兼容破坏。

版本化改为显式“契约 major”机制：

1. 后端 `docs/openapi.json` 仍是源；发布后从中筛出 console 路由，生成并附带 `memos-console-openapi-v1.json` 与 SHA-256。
2. 前端仓库把经审核的该文件固定在 `contracts/`，从它生成/校验 TypeScript 类型；CI 禁止手写类型与 snapshot 漂移。
3. `GET /auth/capabilities` 返回 console API major/minor；前端支持 `major=1`，major 不匹配时停止登录并显示可操作的升级信息。minor 只做能力提示，不阻塞兼容的增量升级。
4. console v1 内只允许向后兼容变化：新增 optional 字段/endpoint；删除、改名、改变状态码或收紧既有字段属于 breaking change。
5. 第一个 breaking 版本新建 `/v2/auth/*`、`/v2/me/*`、`/v2/admin/*`，v1 路由保留至少一个后端 minor 发布周期；不就地改变 unversioned v1。
6. 后端版本、前端版本各自使用 SemVer，不要求数字相同。README 维护矩阵，例如“console 1.4–1.x ↔ console API major 1 ↔ MemOS ≥ 某版本”。部署 pin 精确镜像 digest/tag。

后端 contract PR 在合并前用前端的 `contract:check` 或独立兼容检查验证；前端 PR 至少针对“支持范围内最老的 v1 snapshot”和“当前 v1 snapshot”运行 contract tests。

### 6.3 独立发布流程

```text
后端 PR
  → pytest / make format / make openapi
  → OpenAPI breaking-change gate
  → 发布 MemOS + console-v1 contract artifact

前端 PR
  → 更新/核对 contracts snapshot
  → lint/typecheck/Vitest/Playwright/build
  → tag 独立前端版本
  → 发布 dist + 静态 OCI image + checksum/SBOM

部署
  → 先升级向后兼容的后端
  → canary 校验 /auth/capabilities 和 refresh
  → 再升级前端静态制品
  → 前端可独立回滚；不回滚 SQLite migration
```

- 纯前端兼容修复可单独发布，不触发 MemOS release。
- 后端新增 optional 能力可先发布；旧前端继续工作，新前端通过 capabilities/contract 启用。
- breaking v2 必须先让后端并行提供 v1/v2，再发布前端，最后经过公告窗口移除 v1。
- `config.js` 与 CSP/反代配置是部署层文件，不含 secret；缓存策略为 `index.html/config.js: no-cache`、带 hash 的 assets 长缓存 immutable。

## 7. `web-console-revamp.md` 覆盖与修订清单

以下精确节名需要修订；未列出的内容继续有效。

| 原节名 | 覆盖范围 | 修订结论 |
|---|---|---|
| 文首“架构结论” | “opaque session”仍成立，但单 token 表述不完整 | 改为 opaque access/refresh 双 token、服务端 family 可吊销。 |
| `## 2. 七个新决策点` → “1. 跨域认证载体” | **整行覆盖**：“仅放 Pinia 内存、默认不写 localStorage”和“refresh cookie 不纳入 v1” | 改为两个非 cookie token；Authorization access + JSON refresh；版本化 envelope 写 localStorage。 |
| `## 2. 七个新决策点` → “3. 前后端部署拓扑” | 补充，不推翻静态独立部署 | 明确前端是独立 git 仓库、独立 SemVer/CI/制品，不进入 MemOS tree/package。 |
| `### 3.1 推荐拓扑` | 拓扑图保留，部署说明补充 | 增加独立仓库制品、运行时 config 和独立回滚；`/api` 仍只是反代前缀。 |
| `### 3.2 Token 流程` | **整节覆盖** | 删除“Pinia 内存/刷新页面重新登录”和未来 refresh cookie 假设，替换为本文 bootstrap、双 token 和 refresh 流程。 |
| `### 4.1 通用约定` | 补充 | 仍无全局版本前缀；新增 console contract major 与 capabilities；Web access 仍只走独立 principal。 |
| `### 4.2 认证与注册` | 表内 register/login/logout 与响应字段、路由集合 | register/login 返回 token pair；新增 `/auth/refresh`、`/auth/logout-all`、`/auth/capabilities`；`expires_at` 拆为两个时间。 |
| `### 4.5 源码精确改动边界` | auth、UserManager、新建文件三行 | 加入 token rotation、refresh router/models 和 storage contract；`verify_api_key()` 不改的边界保留。 |
| `### 4.6 数据模型与迁移` | **`web_sessions` 定义整段覆盖** | 用本文双 hash、previous hash、双过期时间、rotation/revoke 字段和索引替换单 token 表；Argon2id 密码段保留。 |
| `## 5. 前端工程方案` | 工程归属 | 整章移至独立仓库语境；MemOS 仓库不创建 `web-console/`。 |
| `### 5.1 最终 package.json 依赖清单` | 补充现有 package.json | 依赖技术栈保留，补 packageManager、typecheck/coverage/e2e/contract/format 脚本与 lockfile；不得重复脚手架。 |
| `### 5.3 目录结构与页面` | 根目录与 auth/content/CI/contract 结构 | 页面保留；新增 `src/auth`、contracts、deploy、workflows；Markdown 改为 token→VNode，禁止 `v-html`。 |
| `### 5.4 API client 层` | **整节覆盖** | 删除“401 立即清空”“不使用 localStorage”“后续再设计 refresh”，替换为 single-flight、跨标签锁、终态分类和单次重放。 |
| `## 6. 与 admin-console-plan.md 差异对照` | “认证/session”“OpenAPI/部署”相关行 | 更新为持久双 token；静态产物不仅独立部署，也来自独立仓库/发布流。 |
| `## 7. 安全与运维要求` → 1、2、5、7、8 | CSRF/XSS/token 生命周期/日志/CSP | cookie 仍不用；加入 localStorage 风险、refresh 脱敏轮换；**第 5 项整项覆盖**为 15 分钟 access + 30 天 refresh。 |
| `## 8. TDD 顺序` → 阶段 1、2、5、6 | 会话模型、auth API、前端 client、契约 | 按本文第 8 节重新排序并补 rotation/replay/multi-worker/持久恢复测试。 |
| `## 9. 验收清单` | “session token 仅内存”等条目 | 将仅内存/刷新重登替换为持久化、刷新、重放、独立仓库、兼容契约验收。 |
| `## 10. 方案自审结论` | 结论过时 | 补充本文八项输出，并引用本文为会话与仓库决策的最终来源。 |

## 8. 更新后的 TDD 实施顺序

每个阶段都遵循“先写失败测试并确认失败 → 最小实现 → 相关测试通过 → 格式检查”的顺序。下面是实现计划，不授权本次修改代码。

### 阶段 1：契约与可测试时钟

- [ ] 在 `tests/api/test_console_models.py` 先断言 login/register/refresh 的 token pair schema、敏感字段无 example、稳定 401 error code。
- [ ] 在 `tests/api/test_web_auth.py` 先用 fake clock/token generator 写 access/refresh 前缀、哈希不等于明文、双 TTL 和格式拒绝测试。
- [ ] 最小实现 `console_models.py` 与 `web_auth.py` primitives；运行 `poetry run pytest tests/api/test_console_models.py tests/api/test_web_auth.py -q`。

### 阶段 2：SQLite migration 与多 worker 原子性

- [ ] 在 `tests/mem_user/test_user_schema_migration.py` 从当前旧库启动，断言完整列/索引、`user_version=2`、WAL/外键/busy_timeout、重复启动幂等。
- [ ] 写两个独立 SQLAlchemy session/connection 并发 rotate 测试：只能一个 current CAS 成功，另一个落入 previous 宽限或 replay 路径，数据库不出现半更新。
- [ ] 写 revoked/expired 有界清理与 20 family 上限测试，再最小实现 `WebSession` 和 UserManager 方法。
- [ ] 运行 `poetry run pytest tests/mem_user/test_user_schema_migration.py tests/mem_user/test_web_sessions.py -q`。

### 阶段 3：轮换、宽限和重放检测

- [ ] 先测正常 refresh 每次两个 token 都变化、rotation +1、绝对 refresh 期限不延长、旧 access 失效。
- [ ] 先测 previous refresh 在 10 秒内只恢复一次；超时或更老 token 返回 `refresh_token_reused` 并撤销 family。
- [ ] 先测 logout 当前、logout-all、改密/停用全部撤销，以及一个设备重放不影响另一设备。
- [ ] 实现 `SessionService` 事务，运行 `poetry run pytest tests/api/test_web_auth.py tests/mem_user/test_web_sessions.py -q`。

### 阶段 4：auth router 与独立 principal

- [ ] 在 `tests/api/test_auth_router.py` 先覆盖 register/login 双 token、refresh 200/401/429、logout access/refresh 两种路径、capabilities 和 `AUTH_ENABLED=false` 404。
- [ ] 在 `tests/api/test_web_principal.py` 先断言 `wca_` 可访问控制台路由但被 `verify_api_key()`/业务 router 拒绝；`wcr_` 不能当 access。
- [ ] 实现/mount router 与 dependency；更新 OpenAPI 前先取得公开 API 审批。
- [ ] 运行 `poetry run pytest tests/api/test_auth_router.py tests/api/test_web_principal.py tests/api/test_auth.py tests/api/test_admin_auth.py -q`。

### 阶段 5：前端 storage、bootstrap 与跨标签协调

- [ ] 在独立仓库用 Vitest 先测 envelope 正常/损坏/过期、atomic replace、logout clear、storage/BroadcastChannel 同步。
- [ ] fake clock 测 access 充足直接 `/auth/me`、阈值内 refresh、refresh 终态跳登录、网络/429/5xx 保留 token。
- [ ] mock Web Locks 测多标签只有一次 refresh，rotation 已变化时不再用旧 token。
- [ ] 最小实现 `storage.ts`、`refreshCoordinator.ts`、`bootstrap.ts` 和 Pinia store。

### 阶段 6：API client 单次重放

- [ ] 先测 N 个并发 401 只发一次 refresh，每个原请求只重放一次且使用新 Authorization。
- [ ] 先测 public/apiKey/auth endpoint 不 refresh，403/429/5xx 不 refresh，重放后 401 清理，nonReplayable 不自动重放。
- [ ] 先测 JSON POST 重建 body、refresh raw client 不递归、headers/body/token 不进入日志。
- [ ] 实现 `http.ts`；运行 `pnpm test:coverage`（或仓库锁定的等价命令）。

### 阶段 7：内容安全与 CSP

- [ ] ESLint fixture 证明任意业务组件使用 `v-html` 会失败。
- [ ] Vitest 输入 script、事件属性、`javascript:` URL、原始 HTML、危险 KaTeX trust 命令，断言只输出安全 VNode/文本。
- [ ] Playwright 在强制 CSP 下验证登录、Markdown、KaTeX、Nuxt UI；断言无 CSP violation、无远程 script、localStorage 只有约定 envelope。

### 阶段 8：独立仓库契约、CI 与发布

- [ ] `contract:check` 先对缺失 `/auth/refresh`、字段删除、状态码变化制造失败，再对 v1 snapshot 通过。
- [ ] CI 在全新 checkout/frozen lockfile 下完成 lint、typecheck、unit、contract、build、Playwright，并上传 dist。
- [ ] release dry-run 校验 tar/OCI 内容不含 `.env`、token、source map secret；生成 checksum/SBOM。
- [ ] README compatibility matrix 同时覆盖支持范围内最老 v1 和当前 v1。

### 阶段 9：端到端与回归

- [ ] Playwright：注册 → 刷新页面保持登录 → access 到阈值自动 refresh → 原请求重放 → 重启一个 API worker 后仍登录。
- [ ] Playwright：双标签并发 refresh、当前设备 logout、logout-all、多设备隔离、refresh 到期/replay 强制登录。
- [ ] 分别验证 `/api` 反代与直接跨 origin；CORS `allow_credentials=false`、OPTIONS、CSP 和 HTTPS 正确。
- [ ] 后端运行 `make openapi`、`make format`、相关 pytest、`make test`；前端运行完整 CI。保存真实输出，不能用计划文本替代执行证据。

## 9. 验收清单

### 会话与恢复

- [ ] 登录/注册返回 `wca_` access 与 `wcr_` refresh，SQLite 只存哈希；OpenAPI、日志、错误和数据库均无明文 token。
- [ ] 刷新浏览器页面、关闭后重新打开、API worker 重启均可恢复登录；只有 refresh 过期或明确撤销/重放等安全终态才跳登录。
- [ ] access 默认 15 分钟、refresh 固定 30 天；阈值 refresh 不延长 30 天绝对期限。
- [ ] refresh 每次轮换两个 token；旧 refresh 超宽限触发 family 撤销，宽限内丢包恢复和双标签并发有自动化测试。
- [ ] 401 只触发一次 single-flight refresh，所有等待请求至多重放一次；refresh 网络/429/5xx 不误清 token。
- [ ] 当前设备 logout、logout-all、改密、停用和多设备隔离符合清理矩阵。

### 浏览器安全

- [ ] `localStorage` 只有版本化 token envelope 和非敏感 client metadata；完整 `mk_*`/`krlk_*`/一次性 key 永不持久化。
- [ ] 全仓 `v-html`/原生 HTML 注入 lint 为零；Markdown token allowlist、链接协议和 KaTeX trust 测试通过。
- [ ] CSP 无 script `unsafe-inline`/`unsafe-eval`，`connect-src` 只允许部署 API；没有第三方运行时脚本。
- [ ] token 不进入 URL、Referrer、toast、telemetry、console、source map 或缓存响应。

### 后端兼容与并发

- [ ] 多 worker 对同一 SQLite 文件执行 refresh 时 CAS/`BEGIN IMMEDIATE` 保证一致，无进程内 session 真相源。
- [ ] `verify_api_key()`、25 个业务端点、`mk_*`/`krlk_*`、master-only 路由和 Cube ACL 语义不变；Web access 无 privileged bypass。
- [ ] `AUTH_ENABLED=false` 时新增控制台路由 404；CORS 显式 allowlist、`allow_credentials=false`。
- [ ] migration 对当前旧库幂等，用户/Cube 数据无损；清理有界，session 数量有限制。

### 独立仓库与发布

- [ ] 前端代码、git history、CI、release、issue/安全策略均在独立仓库；MemOS Python 包不包含前端源码或 dist。
- [ ] 已有 package.json 补齐 scripts/packageManager/lockfile；全新 checkout 可复现安装、测试和构建。
- [ ] `contracts/memos-console-openapi-v1.json`、capabilities major 和 README 兼容矩阵一致；breaking diff 会阻断 CI。
- [ ] 前端 dist/OCI 可独立发布、部署和回滚；运行时 config 不含 secret，后端升级不依赖同步发布同版本前端。
- [ ] 后端 `make openapi`、相关 pytest、`make format`、`make test`，以及前端完整 CI/Playwright 都有真实通过记录。

## 10. 方案自审

- 八项要求均有对应章节：模型选择（1）、localStorage/XSS/恢复（2）、后端 refresh diff 与前端重放（3–4）、窗口表（5）、轮换/重放/哈希/登出（3–5）、独立仓库（6）、上一版覆盖清单（7）、TDD 与验收（8–9）。
- 所有过期字段、token 名称、路由和 error code 在前后端章节一致；refresh 固定绝对期限，没有混入 sliding session。
- 没有修改现有业务认证语义，也没有要求把 session 写入 PostgreSQL `api_keys`。
- 无待定项。实现前仍需对 API/OpenAPI、SQLite schema、`pyproject.toml` optional extra 和部署文件逐项取得仓库要求的批准。
