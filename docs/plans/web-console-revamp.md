# MemOS Web 控制台升级方案

**目标**：把现有纯 REST 的 MemOS 升级为可面向用户发布的产品：用户可自助注册，Vue SPA 可独立部署，后端保持纯 API，同时不破坏现有 `mk_*`/`krlk_*`、Cube ACL 和 25 个业务端点。

**架构结论**：控制台使用服务端保存的 opaque session，但通过 `Authorization: Bearer <session-token>` 传递，避免跨站 cookie 和 JWT 无法即时吊销的问题。业务 API 仍只接受 `mk_*`/`krlk_*`；控制台的 session 只访问 `/auth/*`、`/me/*`、`/admin/*`。

**技术栈**：Vue 3.5 + Vite 8 + TypeScript 6 + Pinia 4 + Vue Router 4 + vue-i18n 11 + Nuxt UI 4（Vite 模式）+ Tailwind CSS 4 + `fetch` API；`markdown-it`/KaTeX 作为用户门户的按需渲染能力保留。

---

## 1. 基线与不可破坏约束

已阅读：

- `docs/plans/web-console-revamp-input.md`
- `docs/plans/admin-console-audit.md`
- `docs/plans/admin-console-plan.md`
- `src/memos/api/server_api.py`
- `src/memos/api/middleware/auth.py`
- `src/memos/api/routers/admin_router.py`
- `src/memos/api/utils/api_keys.py`
- `src/memos/mem_user/user_manager.py`
- `src/memos/api/access_control.py`

源码事实：

1. `server_api.py` 已有 `RateLimitMiddleware`、`SecurityHeadersMiddleware`、配置化 `CORSMiddleware`；业务 router 统一依赖 `verify_api_key`，`admin_router` 仅在 `AUTH_ENABLED` 时挂载。
2. `verify_api_key()` 校验 `mk_*`、`krlk_*`、内部请求和 `AUTH_ENABLED=false` bypass，并把 `user_name/scopes` 写入 `request.state`。不能把 Web session 塞进该依赖，否则会改变业务认证语义。
3. `admin_router.py` 目前只有 `/admin/keys` 和 `/admin/generate-master-key`，管理员权限由 master key 或 `admin` scope 控制；`api_keys.py` 的 key 只在创建响应中返回一次，数据库只存 SHA-256。
4. `UserManager` 的 SQLite 模型目前只有 `user_id/user_name/role/is_active` 和 Cube 关联；`create_cube()` 会把 owner 加入 Cube，`validate_user_cube_access()` 是现有租户隔离入口。
5. `CubeAccessControl` 对普通 `krlk_*` 解析 actor 并校验 Cube，master/internal/bypass 才可绕过。新的 Web session 必须继续被视为非 privileged。

不改动：25 个业务端点的路径、请求/响应和 `verify_api_key` 语义；`CreateKeyRequest.scopes`、`api_keys` PostgreSQL schema、key 前缀和一次性返回规则；Cube ACL 的统一 403；现有限流、安全头和日志脱敏原则。

## 2. 七个新决策点

| 决策点 | 明确选择 | 理由、边界与回退 |
|---|---|---|
| 1. 跨域认证载体 | **服务端 opaque session + `Authorization: Bearer` header**。登录返回随机 32 字节 token，服务端只存 SHA-256；前端仅放 Pinia 内存，默认不写 `localStorage`。 | 保留 session 的即时吊销、登出生效和改密批量撤销能力；不像 JWT 需要等待过期或维护 denylist；header 天然跨 origin，不受第三方 cookie 策略影响，也不需要 cookie CSRF。若企业必须跨站免登录，可后续增加受同源约束的 refresh cookie，但不纳入 v1。 |
| 2. 注册模式 | **公开注册为默认模式**，`REGISTRATION_MODE=public`；同时支持 `invite` 和 `admin_only` 配置开关。公开注册只收 `user_name/password`，不把邮箱验证、OAuth、找回密码塞入 v1。 | 面向用户发布必须有入口；用户名唯一且密码策略、IP+用户名限流、注册配额和可选反滥用 webhook 防止资源滥用。切到 `invite` 时要求一次性邀请码（服务端哈希存储），切到 `admin_only` 时关闭 `/auth/register`。 |
| 3. 前后端部署拓扑 | **前端独立构建并由 Nginx/Caddy/CDN 托管，API 独立服务**。生产推荐同一站点的 `console.example.com` 静态站 + `api.example.com` API；可选由边缘层把 `/api` 反代到后端。 | 前端无需 Python 运行时，API 可独立扩容和升级。`/api` 反代只减少浏览器跨 origin 的运维复杂度，不把前端产物打回 Python 包；直接跨域仍是受支持的部署形态。 |
| 4. CORS 策略 | **显式 allowlist，`allow_credentials=false`**；`CORS_ORIGINS` 只能是完整 scheme+host+port，禁止 `*`，允许方法 `GET/POST/PUT/PATCH/DELETE/OPTIONS`，允许 headers `Authorization,Content-Type,X-Request-ID`。 | 认证不依赖 cookie，因此不需要凭据模式；预检由现有 `CORSMiddleware` 处理，响应带 `Vary: Origin`。反代同源时仍保留 allowlist，避免误配置后开放 API。 |
| 5. 技术栈优化裁剪 | **保留 Nuxt UI 4、Tailwind v4、markdown-it、KaTeX、VueUse、dayjs；移除 axios 和 Nuxt 全家桶**。使用原生 `fetch` + 一个薄拦截器；markdown/KaTeX 通过动态 import，仅在用户内容页加载。 | Nuxt UI 的表单、表格、Dialog、Toast、键盘可达性正好覆盖管理台；仅使用其 Vite 入口，不引入 Nuxt SSR。markdown-it/KaTeX 是记忆内容的真实用户需求，按需加载控制首屏体积；不再增加 Pinia plugin、UI 图标 CDN 或重复日期库。 |
| 6. 多租户 Cube 分配 | 注册成功在 **同一 SQLite 事务中创建一个专属默认 Cube**，`cube_id` 为新 UUID，owner 为用户，自动加入 association；用户 v1 只能拥有这个默认 Cube，不能自行创建共享 Cube；每用户最多 10 个 active API key。 | 保证“一用户一租户”默认隔离，避免依赖 `cube_id == user_id` 的隐式约定；管理员可通过 `/admin/users/{id}/cubes` 分配已有 Cube，但必须显式操作和审计。现有系统没有可可靠计量的存储/token quota，v1 不伪造容量配额；后续应先增加用量计量再引入软/硬配额。 |
| 7. session 与 API key 映射 | **不自动互换、不自动签发 key**。session 只保护控制台 API；用户在 `/me/keys` 显式创建 `krlk_*`，owner 固定为 session 的 `user_name`，完整 key 仅返回一次。 | 业务调用需长期、可轮换的 API key；控制台 session 短期且不能进入 `verify_api_key`/`CubeAccessControl.is_privileged()`。前端将 console client（session bearer）和 product client（用户复制的 krlk bearer）分开，避免凭证混用。 |

## 3. 部署拓扑与认证流程

### 3.1 推荐拓扑

```text
                       HTTPS
浏览器 ───────────────────────────────────────────────┐
  │ GET / (SPA 静态文件)                               │
  ▼                                                   │
Nginx/Caddy/CDN                                      │
  │  /assets/*, /index.html  ── 静态缓存             │
  │  /api/*  ── 反代、限速、WebSocket/stream 保持 ────┼──▶ MemOS API (FastAPI)
  │                                                   │     ├─ /auth/*
  └─ console.example.com                              │     ├─ /me/*
     (可与 api.example.com 同站点)                    │     ├─ /admin/*
                                                      │     └─ /product/*
                                                      ▼
                                            SQLite users/cubes/sessions
                                            PostgreSQL api_keys
                                            Vector/graph/LLM providers
```

`/api` 反代示例（Nginx）：

```nginx
location /api/ {
    proxy_pass http://memos-api:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID $request_id;
    proxy_read_timeout 300s;
}
location / {
    root /srv/memos-console/dist;
    try_files $uri $uri/ /index.html;
}
```

Caddy 等价配置为 `handle /api/* { reverse_proxy memos-api:8000 }` 与 `handle { root * /srv/memos-console/dist; try_files {path} /index.html; file_server }`。反代部署时前端 `VITE_API_BASE=/api`；直接跨域部署时设为 `https://api.example.com`。

### 3.2 Token 流程

```text
注册/登录 POST /api/auth/*
    └─ 200 {access_token, token_type:"Bearer", expires_at, user}
       └─ Pinia 内存保存 access_token（刷新页面重新登录）
后续 /auth/me、/me/*、/admin/* ── Authorization: Bearer <opaque session>
登出 POST /auth/logout ── 服务端 revoke session，前端清空内存
业务 /product/* ── Authorization: Bearer krlk_*（由用户在 /me/keys 复制）
```

不设置认证 cookie，因此浏览器不会自动携带控制台凭证；`allow_credentials=false` 和 header 认证共同消除跨域 CSRF 主路径。若未来增加 refresh cookie，必须限定 `SameSite=None; Secure; HttpOnly`、独立 refresh origin、双提交 CSRF 和轮换检测。

生产必须全链路 HTTPS（包括 CDN 到浏览器、边缘到 API、API 到 PostgreSQL/Redis）；禁止在 HTTP 上开放注册或返回 token。HSTS、`frame-ancestors 'none'`、CSP 等安全头继续由 `SecurityHeadersMiddleware` 或边缘层提供。

## 4. 后端 API 设计与精确 diff

### 4.1 通用约定

- 前缀：部署使用 `/api` 反代时由边缘去前缀，FastAPI 内部路由仍为 `/auth/*`、`/me/*`、`/admin/*`。
- 成功响应沿用项目包装器；本文用 `data` 展开核心字段。错误：未认证 401、已认证无权 403、冲突 409、校验失败 422、未知服务错误 500。
- 所有新路由均在 `AUTH_ENABLED=true` 时挂载；`false` 时返回 404，现有业务 bypass 行为不变。
- 前端请求携带 `Authorization`；API 不读取 `X-User-Name` 作为 Web 身份。`user_id`、owner、Cube 权限全部从 session principal 或服务端查询得到。

### 4.2 认证与注册

| 方法/路径 | 认证 | 请求关键字段 | 响应关键字段 | 失败语义 |
|---|---|---|---|---|
| `POST /auth/register` | 无 | `user_name`、`password`、`invite_code?`（仅 invite 模式） | 201：`user`（不含 hash）、`default_cube`、`access_token`、`expires_at` | 用户名冲突 409；模式关闭 404；弱密码/邀请码错误 422/403；限流 429 |
| `POST /auth/login` | 无 | `user_name`、`password` | 200：`user`、`access_token`、`token_type=Bearer`、`expires_at` | 用户不存在、密码错误、未设置密码、停用统一 401；限流 429，不泄露账号存在性 |
| `POST /auth/logout` | session bearer | 无 | 204 | token 缺失/失效 401；重复登出幂等 204 |
| `GET /auth/me` | session bearer | 无 | 200：当前用户、可访问 Cube 摘要 | 401 |

注册事务：校验用户名规范和密码策略 → 创建 `User(role=USER)` → 创建唯一默认 Cube → association 写入 → commit → 创建 session。任一环节失败整体回滚；不允许客户端提交 role、owner_id、cube_path 或 quota。

### 4.3 `/admin/users` 与 Cube

| 方法/路径 | 权限 | 行为 |
|---|---|---|
| `GET /admin/users` | ROOT/ADMIN session，或映射到 ROOT/ADMIN 且有 `admin` scope 的既有 key | 分页、`role`/`is_active` 过滤；响应只含 `user_id,user_name,role,is_active,created_at,default_cube_id` |
| `POST /admin/users` | ROOT/ADMIN 写权限 | 管理员代建用户；role 默认 USER；可传初始密码和 `cube_ids`；不返回密码/hash |
| `GET /admin/users/{user_id}` | ROOT/ADMIN | 不存在 404；返回用户和 Cube 摘要 |
| `PATCH /admin/users/{user_id}` | ROOT 可改任意目标；ADMIN 仅 USER/GUEST | 允许 `is_active`、role（受层级约束）、重置 password；`user_name` immutable；改密/停用撤销该用户全部 session |
| `DELETE /admin/users/{user_id}` | ROOT/ADMIN（层级约束） | 软删除/停用；ROOT 不可删除，最后一个 ROOT 不可停用；同步撤销 session |
| `GET /admin/cubes` | ROOT/ADMIN | 列出 active Cube，供分配对话框 |
| `PUT /admin/users/{user_id}/cubes` | ROOT/ADMIN 写权限 | 原子替换用户可访问 Cube 集合；owner Cube 不可移除；无效/停用 Cube 422 |

管理员规则：ROOT 可管理所有角色；ADMIN 不能创建、修改、停用或删除 ROOT/ADMIN；普通用户访问任何 `/admin/*` 固定 403，不能通过路径枚举用户。

### 4.4 `/me` 与自助 API key

| 方法/路径 | 权限 | 行为 |
|---|---|---|
| `GET /me/profile` | 任意 session | 当前用户信息、默认 Cube 和 key 数量 |
| `PATCH /me/profile` | 当前 session | 需 `current_password`；只允许改密码；成功撤销该用户全部 session 并返回 204/重新登录提示 |
| `GET /me/keys` | 任意 session | 仅返回自身 key 元数据（prefix/scopes/expiry/active），不返回完整 key |
| `POST /me/keys` | 任意 session | body 复用 `scopes,description,expires_in_days`；服务端强制 owner=`principal.user_name`，201 且完整 `krlk_*` 只出现一次 |
| `DELETE /me/keys/{key_id}` | 任意 session | owner 条件原子吊销；不存在、他人或已吊销统一 403，防止枚举 |

现有 `/admin/keys` 保留路径、schema、一次性 key 返回和 master key 能力；session 管理员通过新的 `require_admin_principal` 进入，旧的 admin-scope key 继续兼容。`/admin/generate-master-key` 仍只允许 `is_master_key=true`，不会被 Web session 放大权限。

`api_keys.py` 新增 owner-safe 函数，SQL 必须是单条原子更新：

```sql
UPDATE api_keys
SET is_active = FALSE
WHERE id = %s AND user_name = %s AND is_active = TRUE;
```

### 4.5 源码精确改动边界

| 文件 | 修改内容 | 明确不改 |
|---|---|---|
| `src/memos/api/server_api.py` | 新增 `auth_router`、`me_router`；`AUTH_ENABLED` 下挂载 `/admin` 扩展；CORS 改为显式 allowlist、`allow_credentials=False`、补 `PATCH`；保留 middleware 顺序和业务 router dependency | 不改 25 个业务路由、`/health`、`/download` 语义 |
| `src/memos/api/middleware/auth.py` | 保持 `verify_api_key()`；新增 `WebPrincipal`/session bearer 解析依赖，写入 `request.state.web_principal`，不把 session 写入 `AuthContext.is_master_key` | 不接受 session 访问 `/product/*`，不改变 key hash/格式 |
| `src/memos/api/routers/admin_router.py` | 增加 `/users*`、Cube 分配；既有 `/keys*` 认证改为混合管理员依赖；所有日志使用 `logger.info("... %s", value)` 且只记 id/prefix | 不改 `CreateKeyRequest.scopes`、`/generate-master-key` 的 master-only 规则 |
| `src/memos/api/utils/api_keys.py` | 增加 `revoke_api_key_for_user()` owner 条件函数 | 不改表结构、key 生成、列表字段 |
| `src/memos/mem_user/user_manager.py` | User 增加 `password_hash,password_updated_at,last_login_at`；增加幂等 SQLite migration、WebSession、注册+默认 Cube 原子服务、session revoke | 不改既有 `create_user()` 对 API 调用方的兼容语义 |
| `src/memos/api/access_control.py` | 仅补注释/测试：session 不进入该 gate；krlk 仍按 user_name→user_id→Cube ACL | 不改统一 403、master/internal/bypass 规则 |
| 新建 `src/memos/api/web_auth.py`、`console_models.py`、`auth_router.py`、`me_router.py` | 密码哈希、opaque session、注册/登录/登出、Pydantic schema、自助 key | 不把密码/hash/session 字段放响应或 OpenAPI schema |

### 4.6 数据模型与迁移

`users` 增加 nullable `password_hash`（历史用户保持可用但不能 Web 登录）、`password_updated_at`、`last_login_at`。新增 `web_sessions(session_id_hash PK,user_id FK,created_at,last_seen_at,expires_at,revoked_at)` 和 `idx_web_sessions_user_active`。启动时先 `Base.metadata.create_all()`，再用 `PRAGMA table_info` 检测缺列，在 `BEGIN IMMEDIATE` 中逐列 `ALTER TABLE`，设置 `PRAGMA user_version=2`；重复启动必须幂等。SQLite 连接启用 `foreign_keys=ON,WAL,busy_timeout=5000,check_same_thread=False`。

密码使用 Argon2id（memory 64 MiB、time 3、parallelism 4、hash_len 32、salt_len 16），只存 PHC 字符串；依赖放 `web-console` optional extra，是否修改 `pyproject.toml` 需单独批准。

## 5. 前端工程方案

### 5.1 最终 package.json 依赖清单

版本采用用户已核实的主版本，提交时用 lockfile 固定精确版本；下表中的“必选/可选”是产品功能开关，不代表 package manager 的 optionalDependencies。

```json
{
  "name": "memos-web-console",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc --noEmit && vite build",
    "preview": "vite preview",
    "test": "vitest run",
    "lint": "eslint ."
  },
  "dependencies": {
    "@nuxt/ui": "^4.10.0",
    "@vueuse/core": "^14.4.0",
    "dayjs": "^1.11.21",
    "katex": "^0.18.3",
    "markdown-it": "^15.0.0",
    "pinia": "^4.0.2",
    "vue": "^3.5.41",
    "vue-i18n": "^11.4.8",
    "vue-router": "^4.6.4"
  },
  "devDependencies": {
    "@tailwindcss/vite": "^4.3.3",
    "@types/katex": "^0.16.8",
    "@types/markdown-it": "^14.1.2",
    "@types/node": "^26.2.0",
    "@vitejs/plugin-vue": "^6.0.8",
    "@vue/compiler-sfc": "^3.5.41",
    "@vue/test-utils": "^2.4.11",
    "eslint": "^10.8.1",
    "eslint-plugin-vue": "^10.10.0",
    "happy-dom": "^20.11.2",
    "tailwindcss": "^4.3.3",
    "typescript": "^6.0.3",
    "typescript-eslint": "^8.66.0",
    "vite": "^8.2.1",
    "vitest": "^4.1.10",
    "vue-tsc": "^3.3.9"
  },
  "engines": {
    "node": ">=22.13.0"
  }
}
```

- **必选**：Vue、Vite、TypeScript、Pinia、Router、i18n、Nuxt UI、Tailwind、VueUse、dayjs。
- **按需可选**：`markdown-it` + `katex`。用户门户展示记忆正文/公式时动态加载；只部署管理台的客户可通过构建变量移除两包。
- **明确移除**：axios（`fetch` 已足够且避免重复拦截器）、Nuxt SSR/`nuxt`、React、独立状态持久化插件、图标 CDN、moment、重复 UI 框架。

### 5.2 Tailwind v4 + Nuxt UI Vite 模式要点

1. 使用 `@nuxt/ui@4.10+`：该版本的 peer range 明确包含 TypeScript 6，并导出 `@nuxt/ui/vite` 和 `@nuxt/ui/vue-plugin`。Node 固定为 22.13+，满足 Vite 8、Nuxt UI 和 ESLint 的 engines。
2. `vite.config.ts` 使用 Nuxt UI 的 Vite plugin；Nuxt UI 已集成 Tailwind v4 处理，避免同时重复注册 `@tailwindcss/vite`：

   ```ts
   import ui from '@nuxt/ui/vite'
   import vue from '@vitejs/plugin-vue'
   import { defineConfig } from 'vite'

   export default defineConfig({
     plugins: [vue(), ui()],
   })
   ```

3. `src/assets/styles.css` 使用 CSS-first 入口，不创建 v3 `tailwind.config.js`：

   ```css
   @import "tailwindcss";
   @import "@nuxt/ui";
   ```

4. `main.ts` 先导入全局 CSS，再安装 Pinia/i18n/router 和 Nuxt UI Vue plugin：`import ui from '@nuxt/ui/vue-plugin'`、`app.use(ui)`；主题 token 用 CSS 变量覆盖，不复制组件源码。
5. 生产构建设置 `VITE_API_BASE`，禁止把 master key、测试 key 或服务器 secret 编译进 `dist`；通过 `npm run build` 产出可独立托管的静态目录。
6. 仅使用 Nuxt UI 提供的按钮、表格、Dialog、Toast、Form、Pagination 等原子能力；页面布局用 Tailwind utility，避免引入卡片套卡片和第二套设计系统。

### 5.3 目录结构与页面

```text
web-console/
├── index.html
├── vite.config.ts
├── tsconfig.json
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── assets/styles.css
│   ├── router/index.ts
│   ├── stores/{auth,users,keys}.ts
│   ├── i18n/{index,zh-CN,en-US}.ts
│   ├── api/{http,auth,admin,me,product}.ts
│   ├── types/{auth,user,key,cube,api}.ts
│   ├── layouts/{PublicLayout,AdminLayout,UserLayout}.vue
│   ├── views/{LoginView,RegisterView,AdminDashboardView,UserPortalView}.vue
│   └── components/{UserTable,UserForm,CubeAssignment,KeyTable,OneTimeKeyDialog,MarkdownContent}.vue
└── tests/{api,stores,views}/*.spec.ts
```

关键页面：

- `LoginView`：用户名/密码、统一错误、登录限流提示；成功后调用 `/auth/me`，按 role 跳转。
- `RegisterView`：公开模式显示用户名/密码/确认密码；invite 模式增加邀请码；不显示 role/Cube 字段。
- `AdminDashboardView`：用户列表分页、创建/编辑/停用、Cube 分配、key 代签；ROOT/ADMIN 控件按服务端 role 再次隐藏，不能只靠前端鉴权。
- `UserPortalView`：当前 profile、默认 Cube、自己的 key 元数据；创建 key 后使用一次性 Dialog，关闭后只显示 prefix；改密后清空内存 token 并回登录。
- `MarkdownContent`：先用 markdown-it 白名单渲染，再把公式交给 KaTeX；禁止原始 HTML，链接统一 `rel="noopener noreferrer"`。

### 5.4 API client 层

`api/http.ts` 提供唯一 `request<T>(path, init, authMode)`：

- `authMode="session"` 从 Pinia 内存读取 opaque token，注入 `Authorization: Bearer`；不打印 headers/body。
- `authMode="apiKey"` 只由明确的产品调用传入 krlk/mk key，不从 session 自动回退。
- 处理 JSON/空响应、请求 ID、超时（`AbortController`）、网络错误和统一 `{code,message,data}` 包装。
- 401：清空 auth store、停止并发请求、跳转登录；403：显示固定无权限；409/422：映射字段错误；429：显示重试倒计时；5xx：显示 request id，不显示后端异常文本。
- 不使用 `localStorage/sessionStorage` 保存 session 或完整 key；刷新页面重新登录。若后续增加刷新机制，单独设计旋转 refresh token 和 CSRF。

`auth.ts` 封装 register/login/logout/me；`admin.ts` 封装 users/cubes/keys；`me.ts` 封装 profile/keys；`product.ts` 强制显式传入 krlk key。Pinia 只保存用户摘要、过期时间、session token 和 loading/error 状态。

## 6. 与 `admin-console-plan.md` 差异对照

| 主题 | 首版轻量方案 | 本方案 | 处理 |
|---|---|---|---|
| 密码哈希 | Argon2id 参数与 nullable hash | **沿用** | 仅把依赖纳入前后端分离部署说明 |
| session | SQLite 服务端 session + HttpOnly 同源 cookie | **推翻载体，沿用服务端 session**：opaque token 放 Authorization header | 适配独立域名、避免第三方 cookie；即时吊销理由保留 |
| 注册 | 方案未定，偏管理员代建 | **新增公开注册默认**，invite/admin_only 可配置 | 注册自动建 USER + 默认 Cube |
| 前端 | 原生 HTML/CSS/ES2022，挂 `/console/` | **推翻**为 Vue SPA，独立 Nginx/CDN 构建 | 后端不再托管前端静态文件 |
| CORS | 同源，无需凭据细节 | **新增显式 allowlist + Bearer header + 无 credentials** | 预检、Vary、HTTPS 纳入验收 |
| `/admin/keys` | 保留 master/admin scope | **沿用**，增加 session admin principal 兼容 | schema、一次性 key、master-only 规则不变 |
| `/me/keys` | 新增自助 key | **沿用并明确 owner-safe SQL** | session 不自动签 key |
| Cube | UserManager 既有 ACL，管理员分配 | **沿用 ACL，新增注册原子默认 Cube** | v1 禁止用户自建共享 Cube |
| AUTH_ENABLED=false | Web 面关闭 404 | **沿用** | 业务 bypass 不回归 |
| API client | 原生 JS fetch | **沿用思想**，实现为 TypeScript fetch 层 | 不引入 axios |
| OpenAPI/部署 | Python 包静态资源 | **推翻**静态挂载；新增 Docker/CDN smoke | API schema 与静态产物分开发布 |

## 7. 安全与运维要求

1. **跨域 CSRF**：v1 无认证 cookie，状态改变请求只接受 `Authorization` header，CORS 不允许 credentials；校验 `Origin` allowlist。若保留任何 cookie（如未来 refresh），所有写请求启用双提交 CSRF，并设置 `SameSite=None; Secure; HttpOnly`。
2. **XSS**：CSP 禁止 `unsafe-eval`，尽量禁止 `unsafe-inline`；markdown-it `html=false`，KaTeX 输出限定数学节点；Vue 模板不使用 `v-html`，确需使用时先白名单 sanitizer；key Dialog 使用纯文本。
3. **密码**：Argon2id PHC，长度 12–128，拒绝常见密码；错误登录统一 401；改密撤销全部 session；不记录密码、PHC、请求体或 token。
4. **登录/注册限流**：复用 `RateLimitMiddleware`，额外 bucket 为 `IP + 规范化 user_name`：登录 10 次/分钟、注册 3 次/小时；Redis 优先、内存回退；反向代理只信任受配置的 `X-Forwarded-For`。
5. **Token 生命周期**：session 绝对 TTL 8 小时、空闲 TTL 30 分钟；服务端存 hash、支持单用户/全局撤销；前端内存 token 过期前不静默延长，过期重新登录。krlk key 继续走 PostgreSQL active/expiry 校验。
6. **多租户**：每个用户默认 Cube 独占；所有业务请求仍由 `CubeAccessControl.resolve_actor()` 和 `require_cube_access()` 决定，session 不触发 `is_privileged()`；停用用户时 Web session 立即失效，既有 API key 按现有跨库语义由管理员吊销。
7. **日志脱敏**：只记 `user_id`、`key_id`/`key_prefix`、request id、结果和耗时；完整 `mk_*`、`krlk_*`、opaque session、CSRF、密码和 SQL/连接串永不落日志。错误响应只给稳定 message。
8. **边缘安全**：Nginx/Caddy 只暴露 443，API 端口置于私网；限制上传/请求体大小，开启 HSTS、`X-Content-Type-Options`、`Referrer-Policy`、`frame-ancestors 'none'`；CSP 的 `connect-src` 仅允许 console 和 API origin。

## 8. TDD 顺序

每一步都先写失败测试并确认失败，再实现最小代码；本任务只产出方案，以下是执行顺序。

### 阶段 1：模型、迁移与认证基础

1. `tests/mem_user/test_user_schema_migration.py`：旧 SQLite 加载后幂等增加密码列、`web_sessions`、WAL/外键；重复启动无 duplicate column。
2. `tests/api/test_web_auth.py`：Argon2id hash 不等于明文；正确/错误密码；停用/未设密码统一 401；session 过期/撤销。
3. 实现 `UserManager` migration、`web_auth.py`、`console_models.py`；运行上述 pytest 与 Ruff。

### 阶段 2：公开注册、登录登出、限流

1. `tests/api/test_auth_router.py`：public 注册 201+默认 Cube；冲突 409；`REGISTRATION_MODE=invite/admin_only`；登录/登出；错误登录 429。
2. 实现 `auth_router.py`，把注册用户、默认 Cube、session 放进同一事务；更新 `server_api.py` 挂载条件。
3. 运行 `pytest tests/api/test_auth_router.py tests/api/test_web_auth.py -q`，再执行 `ruff check`。

### 阶段 3：管理员 RBAC 与 Cube 分配

1. `tests/api/test_admin_console.py`：未登录 401、USER 403、ROOT CRUD、ADMIN 不能触碰 ROOT/ADMIN、最后 ROOT 保护、无效 Cube 422。
2. 实现 `/admin/users*`、`/admin/cubes`、混合 principal；响应断言不含 password/hash/session。
3. 运行管理员测试以及既有 `tests/api/`、`tests/mem_user/` 中 Cube/认证用例。

### 阶段 4：自助与兼容 key

1. `tests/api/test_me_keys.py`：用户仅见自身 key；创建响应一次返回完整 key；列表无完整 key；越权吊销固定 403；owner-safe SQL rowcount。
2. 实现 `revoke_api_key_for_user()`、`/me/keys`；验证既有 `/admin/keys` 与 master key 流程不变。
3. 运行新增测试和所有 API auth/Cube 回归测试。

### 阶段 5：前端构建与浏览器链路

1. 前端 Vitest：`api/http` 的 401/403/429/5xx、store 登录状态、路由 guard、一次性 key 清理、markdown XSS 过滤。
2. Playwright/Docker smoke：独立 `dist` 由 Nginx/Caddy 托管，浏览器从 `console.example.com` 跨 origin 调 `api.example.com`：注册 → 登录 → 管理员建用户 → 用户登录 → 自建 krlk → 调业务 API → 吊销。
3. 同时验证 `/api` 反代模式、直接跨域模式、预检 OPTIONS、HTTPS、CSP 和错误回显。

### 阶段 6：契约与全量验证

1. 后端路由完成后运行 `make openapi`，确认新 schema 不含 `password_hash/session_id_hash/csrf_token_hash/access_token` 的持久化字段说明，既有 25 个业务 schema 无 diff。
2. 依赖变更获批准后才修改 `pyproject.toml/poetry.lock`，运行 `poetry lock --no-update`。
3. 运行 `make format`、相关 pytest、`make test`，并保存 Docker smoke 的真实输出。

## 9. 验收清单

- [ ] `AUTH_ENABLED=true` 时公开注册/登录/登出、`/auth/me`、`/admin/users*`、`/me/*` 全部可用；`false` 时这些路径和任何控制台静态路径 404。
- [ ] 注册用户自动获得独立默认 Cube；另一用户的业务请求通过 `CubeAccessControl` 固定 403；管理员分配 Cube 可审计且 owner 不可移除。
- [ ] ROOT 可管理所有用户；ADMIN 只能管理 USER/GUEST；普通用户不能访问 `/admin/*` 或枚举他人 `/me/*`。
- [ ] 前端 `dist` 可在无 Python 运行时的 Nginx/Caddy/CDN 上启动；`/api` 反代和直接 CORS 两种模式都能完成登录、CRUD、自助 key。
- [ ] CORS 仅允许配置 origin，预检正确，`allow_credentials=false`；全链路 HTTPS、HSTS、CSP 生效。
- [ ] session token 仅内存、服务端可撤销；不写 api_keys；krlk key 仅由 `/me/keys` 明确签发，一次性明文返回，列表仅 prefix。
- [ ] 密码 Argon2id 存储；密码/完整 key/session/CSRF 不进日志、错误响应或 OpenAPI；登录和注册限流可复现 429。
- [ ] markdown/KaTeX 按需加载，恶意 HTML/script 不执行；构建无未使用 axios/Nuxt SSR 等重型依赖。
- [ ] 既有业务 25 端点、`verify_api_key`、`/admin/keys`、master key、Cube ACL、限流、安全头和既有测试全绿。
- [ ] 完成前端单测、后端 pytest、`make format`、`make openapi`、`make test` 和 Docker 全链路 smoke，并记录真实命令输出。

## 10. 方案自审结论

七个新决策、前端裁剪、Vite 配置、分离部署、API 精确 diff、目录与 client、安全、TDD 和独立部署验收均已覆盖。方案没有要求本次修改业务源码；实现阶段涉及 `pyproject.toml`、Docker 或公开 API/OpenAPI 的变更仍须按 `AGENTS.md` 先获批准。
