# Web 控制台补充决策 — 会话持久化与前端独立仓库（Codex 补充设计输入）

> 审计人：Hermes Agent | 日期：2026-08-10
> 前置：`web-console-revamp.md`（Codex 重规划方案，29.8KB）

## 一、用户新决策（覆盖 revamp 方案中对应点）

### 决策 1：会话必须持久化，刷新不重登录
- ❌ 推翻 revamp 方案中"前端仅放 Pinia 内存、默认不写 localStorage"的默认
- ✅ **要求**：
  1. 刷新页面**不重登录**（token 持久化）
  2. **session 过期后才要求重登录**
  3. **必须有 session 刷新机制**（refresh，不能过期就踢）
- 需 Codex 设计：
  - token 持久化存储方案（localStorage？安全权衡与缓解措施）
  - **双 token（access + refresh）还是滑动过期（sliding session）**？给出选择+理由
  - 刷新机制 API 设计（/auth/refresh？自动拦截器刷新？）
  - 过期窗口（access 短期 + refresh 长期，具体值）
  - 安全缓解：XSS 面控制、refresh token 轮换/重放防护、登出全清

### 决策 2：前端独立仓库
- ✅ 前端作为**独立 git 仓库**（如 fcmyoo/memos-web-console），不放 MemOS 仓库内
- 需 Codex 补充：
  - 独立仓库的初始化清单（目录、CI、构建、发布）
  - 前后端版本协调（后端 API 版本化？前端锁 API 版本？）
  - 前端部署与后端独立发布流程

## 二、保持不变的（revamp 方案其余部分）

- 认证载体：session token + `Authorization` header（非 cookie）
- 注册模式：公开注册默认 + invite/admin_only 开关
- 部署拓扑：前端 Nginx/CDN 独立 + API 独立
- CORS：显式 allowlist、allow_credentials=false、禁 *
- 技术栈：Vue 3.5 + Vite 8 + TS 6 + Pinia 4 + Router 4 + i18n 11 + Nuxt UI 4 + Tailwind 4 + markdown-it + KaTeX + VueUse + dayjs（去 axios/Nuxt SSR）
- 多租户：注册即建专属 Cube、每用户 ≤10 key
- session 与 API key 不自动互换；/me/keys 显式创建

## 三、Codex 输出要求

产出 `docs/plans/web-console-session-design.md`：
1. 会话模型最终设计：access + refresh 双 token vs 滑动过期——明确选择+理由+边界
2. 持久化存储：localStorage 方案 + XSS 缓解措施（内容安全、v-html 禁用面、CSP 建议）
3. /auth/refresh 精确 diff（后端）+ 前端拦截器自动刷新逻辑（401 → refresh → 重放）
4. 过期窗口参数表（access/refresh 时长、刷新阈值、登出清理）
5. refresh token 安全：轮换、重放检测、同设备多会话、服务端存储（web_sessions 表扩展）
6. 前端独立仓库：初始化清单、目录、CI（build/test）、与后端版本协调
7. 对 revamp 方案需要修订的段落清单（哪些行/节被决策 1 覆盖）
8. 更新后的 TDD 顺序 + 验收清单
只出方案不改业务代码（除方案文档外）。用中文。
