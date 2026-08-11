# MemOS Web 控制台 — 前后端分离 + 面向用户 需求补充（Codex 重规划输入）

> 审计人：Hermes Agent | 日期：2026-08-10
> 前置：`admin-console-audit.md`（管理后台审计）+ `admin-console-plan.md`（Codex 首版方案，轻量 HTML 方向）

## 一、需求升级（本补充文件的目的）

原方案（admin-console-plan.md）是"管理后台 + 用户门户，轻量原生 JS，挂 /console/ 随 Python 包发布"。
**用户现在明确升级**：

1. **面向用户发布**：最终是公开产品，普通用户要能自助使用（不只是管理员代建账号）
2. **前后端分离**：前端独立部署（单独服务/CDN），后端只暴露 REST API
3. **技术栈**：用户提供 Vue 3 + Vite + TypeScript + Pinia + Vue Router + vue-i18n + Nuxt UI/Tailwind 全套（已核实版本真实存在），要求优化

## 二、新架构约束（与旧方案的关键差异）

| 维度 | 旧方案（admin-console-plan） | 新方案（本补充） |
|---|---|---|
| 前端形态 | 轻量 HTML+JS，挂 /console/ | **独立 Vue SPA，独立部署**（Nginx/CDN） |
| 认证载体 | session cookie（同源） | **需支持跨域**：CORS + 凭据（cookie 跨域）或 token 头 |
| 用户获取 | 管理员代建 | **自助注册**（公开注册？邀请码？需决策） |
| 部署拓扑 | 单容器全包 | **前后端分离**：API 服务 + 静态前端（可 CDN） |
| CORS | 同源即可 | **必须开放**（前端域名 → API 域名） |

## 三、待 Codex 决策的关键点（新增）

1. **认证载体**：跨域场景下 session cookie（需 SameSite=None + Secure + 显式 CORS 凭据）vs **JWT/Bearer token 放 header**（前端 localStorage/内存，天然跨域）？之前选 session 的理由（可吊销）是否仍成立
2. **注册模式**：公开注册 vs 邀请码 vs 仅管理员代建（面向用户发布通常需注册入口；需防滥用——邮箱验证？验证码？）
3. **前后端分离的部署**：前端 Nginx 静态托管 + 反代 /api 到后端？还是前端独立构建产物由后端托管（同域，规避 CORS）？"分离"到什么程度
4. **CORS 策略**：允许哪些 origin、凭据模式、预检处理（复用现有 CORSMiddleware 配置化）
5. **技术栈优化**：Nuxt UI 对管理后台是否过重？KaTeX/markdown-it 是否保留？给出**面向管理后台 + 用户门户的合理裁剪**
6. **多租户**：注册用户默认 Cube 分配、配额（如果有）、用户自助开通流程
7. **会话与 API key**：Web 用户登录后调业务 API 是走 session 还是自动签发 krlk_ key？两者映射关系

## 四、保留不变（既有体系）

- 后端 25 业务端点 + verify_api_key + CubeAccessControl（403 统一）
- /admin/keys、master key、api_keys PostgreSQL schema
- 统一入口 server_api（admin_router + 限流 + 安全头 + CORS 配置化）
- 240 测试全绿、生产加固（日志脱敏/限流哈希/非 root/离线 HF）

## 五、验收标准（升级版）

1. 前端独立部署后能跨域调后端 API（登录/CRUD/自助 key 全通）
2. 公开注册流程可用（或按决策采用邀请码），注册用户自动获得独立 Cube 隔离
3. 管理员后台（全部用户）+ 用户门户（仅自己）权限边界严格
4. 前端构建产物可独立部署（Nginx 静态或 CDN），无需 Python 运行时
5. 技术栈裁剪后体积/构建合理，无未用重型依赖
6. 安全：跨域下 CSRF/凭据策略正确、无 key/密码泄露、登录限流
7. 全量测试 + Docker 实测（前端→API 全链路）

## 六、输出要求（Codex）

产出 `docs/plans/web-console-revamp.md`：
1. 7 个新决策点明确选择 + 理由
2. 前端技术栈**优化裁剪**后的完整 package.json 依赖清单（标注必选/可选）
3. 前后端分离的部署拓扑图（Nginx/Caddy 反代方案、CORS 配置、cookie/token 流程）
4. 后端新增/改造 API 清单（注册、登录、/admin/users、/me/keys，含跨域适配）
5. 前端目录结构 + 关键页面（登录/注册/管理台/用户门户）+ API 对接层设计
6. 与 admin-console-plan.md 的差异说明（哪些沿用、哪些推翻）
7. TDD 顺序 + 验收清单
