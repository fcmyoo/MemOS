# MemOS 双 API 入口差异性审计（统一入口方案输入）

> 审计人：Hermes Agent（源码逐行核实）| 日期：2026-08-10
> 分支：feat/api-auth-hardening（6f37591f + P0 修复 + 生产加固未提交改动）

## 一、问题定性

**同一套业务被复制成两份平行的 FastAPI app，能力分裂、维护双倍、部署踩坑。**
`server_api_ext.py` 不是"扩展"，而是从旧版派生的**另一份实现**——它缺默认入口的基础能力，默认入口缺它的增强能力。谁覆盖谁都会丢功能。

## 二、事实对比表（源码级）

| 能力 | `server_api.py`（默认 8000） | `server_api_ext.py`（8001） | 差异影响 |
|---|---|---|---|
| 25 业务端点 + verify_api_key | ✅ | ✅ | 两处平行挂载同一鉴权 |
| `RequestContextMiddleware`（trace_id 上下文） | ✅ | ❌ | ext 日志链路追踪退化 |
| `/download` 静态文件挂载 | ✅ | ❌ | ext 文件下载 404 |
| `plugin_manager.discover()/init_app` | ✅ | ❌ | ext 插件不加载 |
| `admin_router`（签发/吊销 key） | ❌ | ✅ | 默认入口无法在线管理 key |
| `RateLimitMiddleware` | ❌ | ✅ | 默认入口无限流，暴力破解风险 |
| `SecurityHeadersMiddleware` | ❌ | ✅ | 默认入口无安全响应头 |
| CORS | ❌ | ✅ | 默认入口跨域受限 |
| `APIExceptionHandler` 全套 | ✅ | ✅ | 一致（含 AccessForbiddenError） |
| health（公开、去指纹） | ✅ | ✅ | 一致 |
| `load_dotenv()` | ✅ 模块级 | ⚠️ 依赖 auth.py | 见 auth.py:49 注释 |
| cli.py `get_openapi_app` | ✅ 用它 | ❌ | OpenAPI 导出只认默认入口 |

**差异根因**：ext 是 MemOS 旧版（krolik overlay 模式）时代遗留的平行实现，docstring 仍写着已废弃的 `COPY overlays/krolik/` 流程；上游演进后（plugins/request_context/download 加入 server_api），ext 没有同步这些能力，反而把增强（admin/rate-limit/security）单独养在了自己身上。

## 三、正确架构方向

**消除双入口，单一真实入口 + 配置化组装。**

- `server_api.py` 成为唯一真实实现，包含全部能力：
  - 地基不变：RequestContextMiddleware、/download、plugin_manager、25 端点鉴权、异常处理
  - 合并增强：admin_router、RateLimit（`RATE_LIMIT_ENABLED` 开关）、SecurityHeaders、CORS（`CORS_ORIGINS` 配置化）
  - 中间件顺序：CORS → SecurityHeaders → RateLimit → RequestContext（CORS/限流在鉴权前，防暴力破解）
- `server_api_ext.py` **删除**或退化为兼容 shim（`from memos.api.server_api import app`），不保留独立实现
- Dockerfile/compose/文档统一指向 `server_api`
- cli.py 不动（本来就用默认入口）

## 四、约束

1. 不改 25 个业务端点 path/method/response_model（OpenAPI 兼容）
2. 不改 `CreateKeyRequest.scopes` 枚举、不改 `api_keys` 表 schema
3. `AUTH_ENABLED=false` 时：鉴权跳过，但 admin 端点仍应 404 或按旧行为（防意外暴露管理面）——需明确
4. 中间件顺序必须保证：RateLimit 在 verify_api_key 之前（防暴力破解）
5. 现有 228 测试全绿；`test_auth_router_mounts.py` 中针对 ext 的用例需改造为验证单一入口
6. 生产加固已做项（日志不泄 key、限流键哈希、XFF 防伪造、非 root、CORS 配置化、health 去指纹、restart 策略）保持不回归
7. `RATE_LIMIT_ENABLED` 默认值需与当前行为一致（现默认 true）；部署时无 Redis 则回退内存限流（现有逻辑已支持）
8. 不 PR 上游、不提交到 main；改动落在私有分支

## 五、验收标准（Codex 方案需覆盖）

1. `server_api` 单一入口同时具备：25 业务端点鉴权 + admin key 管理 + 限流 + 安全头 + CORS + plugins + download + trace 上下文
2. `server_api_ext` 不再存在独立实现（删除或 shim）
3. 中间件顺序正确（RateLimit 先于鉴权）
4. Dockerfile CMD / compose / 文档全部指向 `server_api`
5. 全量测试通过；新增测试覆盖：单一入口的 admin/rate-limit/security-header 存在性、AUTH_ENABLED=false 兼容
6. 部署验证：8000 端口直接签发 key、越权 403、限流生效、安全头存在
