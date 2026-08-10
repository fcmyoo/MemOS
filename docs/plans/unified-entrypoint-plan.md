# MemOS 单一 API 入口实施方案

> 本文是 `docs/plans/unified-entrypoint-audit.md` 的实施规格。只描述后续实现，不在本次变更中修改业务代码。
>
> **实施要求：** 执行者按本文 TDD 阶段逐项实现、验证并记录真实输出；不得重新引入第二个 FastAPI app。

## 目标与决策

将 `src/memos/api/server_api.py` 变成唯一真实 FastAPI 实现，保留现有业务路由、鉴权、插件、下载、trace 上下文及全部 P0 加固，并把 ext 入口拥有的管理面、限流、安全响应头和 CORS 合并进来。`server_api_ext.py` 不再创建第二个 app，而是保留一个只转发 `app` 的兼容 shim；Docker、Compose、CLI、README 和部署文档均以 `server_api` 为运行入口。

明确的安全选择：

- `RATE_LIMIT_ENABLED` 默认仍为 `true`，与当前 ext 行为一致；它不依赖 `AUTH_ENABLED`，因此 `AUTH_ENABLED=false` 时仍限制请求，防止开发配置误部署后被无限滥用。Redis 不可用时继续使用现有进程内滑动窗口回退。
- `AUTH_ENABLED=true` 时挂载 `/admin`，其中 `/admin/keys*` 和 `/admin/generate-master-key` 继续使用 `require_scope("admin")`，不改请求/响应模型、scopes 约定或数据库 schema；`/admin/health` 保持现有公开契约。
- `AUTH_ENABLED=false` 时不挂载 `admin_router`，所有 `/admin/*` 均返回 404（包括 `/admin/health`）。这是 fail-closed 的管理面策略，避免关闭业务鉴权时意外暴露签发、吊销、生成 master key 的管理能力。该环境变量在 app 导入/进程启动时读取，切换后必须重启进程。
- `/health` 只返回 `{"status": "healthy"}`，不暴露版本、鉴权开关、限流开关或部署名称；仍公开且继续作为限流豁免探针。

## 现状依据与边界

- `server_api.py:41-78` 已创建 FastAPI、挂载 `/download`、注册 `RequestContextMiddleware`、业务 router 和统一异常处理，并调用 `plugin_manager.discover()/init_app`。
- `server_api_ext.py:72-127` 是第二个独立 app，包含 CORS、`SecurityHeadersMiddleware`、可选 `RateLimitMiddleware`、`admin_router`，但缺少上面的插件、下载和 request context。
- `admin_router.py:73-228` 的 key 管理接口已有 scope 保护；本方案只改变它是否被 app 挂载，不改变路由定义。
- `rate_limit.py:24-221` 已包含 Redis/内存回退、key SHA-256 哈希和受控 XFF 逻辑；不重写算法。
- `auth.py:47-57,192-324` 已将 `load_dotenv()` 前置、默认鉴权设为开启，并提供 `verify_api_key`、`get_current_user`、`require_scope`；不回退这些 P0 修复。
- `RequestContextMiddleware` 负责 trace/user 初始上下文；由于按要求位于限流内侧，被限流直接拒绝的请求不建立业务 trace，这一点必须写入测试/运维说明。
- 不改变 25 个 `/product/*` operation 的 path、method、参数、response model 或 OpenAPI security requirement；不改变 `api_keys` 表及 `CreateKeyRequest.scopes`。

## 目标拓扑与中间件顺序

运行时请求链为：

```text
CORS -> SecurityHeaders -> RateLimit -> RequestContext -> FastAPI router/dependencies
                                                               |
                                                               +-> verify_api_key
                                                               +-> admin require_scope("admin")
```

`FastAPI.add_middleware()` 使用 `insert(0)`；最后添加的 middleware 会进入 `app.user_middleware` 首位，并成为最外层用户 middleware。因此，为得到上面的**运行时/栈顺序**，源码调用顺序必须反向书写：

```python
app.add_middleware(RequestContextMiddleware, source="server_api")
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CORSMiddleware, ...)
```

最终 `app.user_middleware` 及运行时外层到内层才是 CORS、Security、RateLimit、RequestContext。审计文档中的箭头应理解为运行时顺序，不能机械照抄为 `add_middleware()` 调用顺序。理由如下：

1. CORS 是最外层用户 middleware，保证预检请求及 401/403/429 等正常生成的错误响应也带跨域响应头。
2. SecurityHeaders 次外层保证健康、鉴权错误、业务错误和限流拒绝等正常响应都有安全头。
3. RateLimit 在 FastAPI 依赖解析前执行，故一定先于 `verify_api_key`，可以限制缺失/错误 key 的暴力尝试；`OPTIONS` 和现有 exempt paths 仍由 middleware 自己跳过。
4. RequestContext 位于业务处理边界，保留现有 trace/user 初始化和日志语义；被 RateLimit 直接拒绝的请求不进入该层，避免为未处理请求制造业务上下文。

测试应直接断言 `app.user_middleware` 的 class names 为 `CORSMiddleware, SecurityHeadersMiddleware, RateLimitMiddleware, RequestContextMiddleware`，并另以请求行为证明 RateLimit 先于鉴权。仅断言源码调用顺序或把列表再次反转都会掩盖真实栈顺序。

## 文件变更清单

### 1. `src/memos/api/middleware/security.py`（新建）

从 ext 移出公共 `SecurityHeadersMiddleware`，类名、响应头和值保持不变：

```diff
*** Begin Patch
*** Add File: src/memos/api/middleware/security.py
+from collections.abc import Callable
+
+from starlette.middleware.base import BaseHTTPMiddleware
+from starlette.requests import Request
+from starlette.responses import Response
+
+
+class SecurityHeadersMiddleware(BaseHTTPMiddleware):
+    """Add the baseline security headers to every response."""
+
+    async def dispatch(self, request: Request, call_next: Callable) -> Response:
+        response = await call_next(request)
+        response.headers["X-Content-Type-Options"] = "nosniff"
+        response.headers["X-Frame-Options"] = "DENY"
+        response.headers["X-XSS-Protection"] = "1; mode=block"
+        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
+        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
+        return response
*** End Patch
```

实现时按仓库 Ruff/typing 约定补齐 `Callable` 的准确签名（若当前 Starlette 类型要求更具体的 `Receive/Send`，沿用 `rate_limit.py` 的项目兼容写法）。

### 2. `src/memos/api/middleware/__init__.py`

增加公共导出，便于测试和其他 app 复用；保留既有 auth/rate-limit 导出：

```diff
 from .rate_limit import RateLimitMiddleware
+from .security import SecurityHeadersMiddleware
 
 __all__ = [
     "AuthContext",
     "RateLimitMiddleware",
+    "SecurityHeadersMiddleware",
     ...
 ]
```

### 3. `src/memos/api/server_api.py`

以下是合并后的完整目标代码。业务 router、异常处理、插件初始化、下载挂载和 lifespan 的既有行为全部保留；新增部分只负责公共 app 组装。

```python
import logging
import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

from memos.api.access_control import AccessForbiddenError
from memos.api.exceptions import APIExceptionHandler
from memos.api.lifecycle import shutdown_components
from memos.api.middleware.auth import AUTH_ENABLED, verify_api_key
from memos.api.middleware.rate_limit import RateLimitMiddleware
from memos.api.middleware.request_context import RequestContextMiddleware
from memos.api.middleware.security import SecurityHeadersMiddleware
from memos.api.routers import server_router as server_router_module
from memos.api.routers.admin_router import router as admin_router
from memos.plugins.manager import plugin_manager


load_dotenv()
plugin_manager.discover()

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logger.info(
    "[SERVER_API] load_dotenv completed. env_MEMSCHEDULER_STREAM_KEY_PREFIX=%s, "
    "env_MEMSCHEDULER_REDIS_STREAM_KEY_PREFIX=%s",
    os.getenv("MEMSCHEDULER_STREAM_KEY_PREFIX"),
    os.getenv("MEMSCHEDULER_REDIS_STREAM_KEY_PREFIX"),
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    shutdown_components(server_router_module.components)


app = FastAPI(
    title="MemOS Server REST APIs",
    description="A REST API for managing multiple users with MemOS Server.",
    version="1.0.1",
    lifespan=lifespan,
)

app.mount("/download", StaticFiles(directory=os.getenv("FILE_LOCAL_PATH")), name="static_mapping")

app.add_middleware(RequestContextMiddleware, source="server_api")
if RATE_LIMIT_ENABLED:
    app.add_middleware(RateLimitMiddleware)
    logger.info("Rate limiting enabled")
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-User-Name"],
)

app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])
if AUTH_ENABLED:
    app.include_router(admin_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Public, fingerprint-free container and load-balancer health endpoint."""
    return {"status": "healthy"}


app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)
app.exception_handler(ValueError)(APIExceptionHandler.value_error_handler)
app.exception_handler(HTTPException)(APIExceptionHandler.http_error_handler)
app.exception_handler(Exception)(APIExceptionHandler.global_exception_handler)
app.exception_handler(AccessForbiddenError)(APIExceptionHandler.access_forbidden_handler)

plugin_manager.init_app(app)


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    uvicorn.run("memos.api.server_api:app", host="0.0.0.0", port=args.port, workers=args.workers)
```

对应当前文件的精确改动要点：

```diff
 from fastapi.exceptions import RequestValidationError
 from starlette.staticfiles import StaticFiles
+from fastapi.middleware.cors import CORSMiddleware
 ...
 from memos.api.middleware.auth import verify_api_key
+from memos.api.middleware.rate_limit import RateLimitMiddleware
 from memos.api.middleware.request_context import RequestContextMiddleware
+from memos.api.middleware.security import SecurityHeadersMiddleware
 from memos.api.routers import server_router as server_router_module
+from memos.api.routers.admin_router import router as admin_router
@@
 app.mount("/download", ...)
 
+RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
+# Calls are reverse of the required runtime order because Starlette prepends each addition.
+app.add_middleware(RequestContextMiddleware, source="server_api")
+if RATE_LIMIT_ENABLED:
+    app.add_middleware(RateLimitMiddleware)
+app.add_middleware(SecurityHeadersMiddleware)
+app.add_middleware(CORSMiddleware, ...)
 app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])
+if AUTH_ENABLED:
+    app.include_router(admin_router)
@@
-    return {"status": "healthy", "service": "memos", "version": app.version}
+    return {"status": "healthy"}
@@
-    parser.add_argument("--port", type=int, default=8001)
+    parser.add_argument("--port", type=int, default=8000)
```

不要把 `admin_router` 作为 `server_router` 的 `dependencies=[...]` 重新 include；admin 路由已经在每个敏感操作上有 `require_scope("admin")` 和 `verify_api_key`，直接挂载可保持 OpenAPI 与现有依赖结构。`AUTH_ENABLED=false` 的条件挂载必须在 app 创建时执行，不能仅依赖 admin handler 内部判断。

### 4. `src/memos/api/server_api_ext.py`

推荐保留 shim，而不是立即删除：外部脚本、旧测试或部署插件可能仍执行 `import memos.api.server_api_ext; app`，shim 可以无停机地兼容这些导入；删除会把兼容性问题变成启动时 ImportError。shim 不得再定义 FastAPI、lifespan、middleware、router 或 health handler，也不应导出自己的配置常量。

```diff
-<删除当前 133 行的独立 FastAPI 实现>
+"""Deprecated compatibility alias; use ``memos.api.server_api:app``."""
+
+from memos.api.server_api import app
+
+__all__ = ["app"]
+
+
+if __name__ == "__main__":
+    import uvicorn
+
+    uvicorn.run("memos.api.server_api:app", host="0.0.0.0", port=8000, workers=1)
```

保留 shim 的代价是模块名短期仍会出现在源码搜索和旧文档中，因此必须加 Deprecated docstring，并在下一次主版本再考虑删除。无论选择 shim 还是未来删除，`docker/Dockerfile`、`docker/Dockerfile.krolik`、`docker/docker-compose.yml`、运行手册和 CI/部署脚本都不得引用 `server_api_ext:app`；运行时只允许 `memos.api.server_api:app`。

### 5. `docker/Dockerfile`、`docker/Dockerfile.krolik`、`docker/docker-compose.yml`

`docker/Dockerfile` 当前 CMD 已指向 `server_api`，保持该值，不重新引入 `--reload`。`docker/Dockerfile.krolik:63-64` 必须改为同一真实入口，保留 gunicorn worker 参数但替换 module：

```diff
-# Use extended entry point with security features
-CMD ["gunicorn", "memos.api.server_api_ext:app", "--preload", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--timeout", "120"]
+# Use the single canonical entry point; security features are assembled there.
+CMD ["gunicorn", "memos.api.server_api:app", "--preload", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--timeout", "120"]
```

同时删除 Dockerfile.krolik 注释中“extension entry point/server_api_ext”的表述；不再新增 overlay COPY。Compose 当前没有 ext CMD，验收时只需断言 service command/image 配置不含 ext，并确认 `AUTH_ENABLED`, `RATE_LIMIT_ENABLED`, `CORS_ORIGINS` 可通过 `env_file`/环境传入。为避免配置不可发现，建议在 `docker/.env.example` 增加空安全默认：`RATE_LIMIT_ENABLED=true`、`CORS_ORIGINS=`、以及现有 `RATE_LIMIT`/`RATE_WINDOW_SEC` 的注释；不改变 Compose 必填 secret 约束。

### 6. CLI、README 和文档引用

- `src/memos/cli.py:get_openapi_app()` 已导入 `memos.api.server_api:app`，不改。理由是 OpenAPI 导出应天然反映唯一真实入口；不要改成 shim 或复制 app。
- `README.md:145-190` 已示范 Docker 与 `uvicorn memos.api.server_api:app`，保留并补充“单一入口包含 admin/rate-limit/security/CORS；ext 仅兼容导入，不作为部署入口”。
- `docs/cn/open_source/modules/api_deployment.md:3-13` 与 `docs/en/open_source/modules/api_deployment.md:3-13` 删除“ext 是仍在测试的扩展实现”的误导，改为说明 `server_api` 是唯一入口，`Dockerfile.krolik` 也使用该入口；若保留 shim，只标注为 deprecated compatibility alias。
- 对 `rg -n "server_api_ext|server_api_ext:app" README.md docs docker src tests .github Makefile pyproject.toml` 的结果逐项处理：可执行命令、Docker CMD、部署文档全部替换为 `server_api`；历史审计/实施计划中的事实记录可以保留文件名，但涉及“继续使用 ext”或旧架构的验收/操作指令必须改成 shim/单一入口语义。`src/memos/api/middleware/auth.py` 的 stale 注释也应删掉“server_api_ext 不加载 dotenv”的旧描述。
- 不改 `Makefile` 的入口（已是 `server_api`）；不改 25 个接口文档路径。

## TDD 实施顺序

每一步先添加失败测试并单独运行确认失败，再实现最小改动；实现后运行对应测试，最后跑格式化和全量验收。建议拆为以下任务，避免测试与组装代码互相遮蔽：

### 阶段 A：先写失败测试

1. 改造 `tests/api/test_auth_router_mounts.py`：移除 `entry_modules` 对 `server_api_ext` 的双入口参数化；fixture 只导入 `server_api`（继续 patch `memos.api.handlers.init_server`），并保留 25 个业务 operation 的 auth、健康公开、401 不调用 handler 等断言。
2. 将原 `TestExtendedEntryPoint` 的 admin/rate-limit 断言改成 `server_api` 断言：
   - 默认环境下 OpenAPI 存在 `/admin/keys`、`/admin/generate-master-key`、`/admin/health`；普通 `read` scope 访问 `/admin/keys` 得 403，`admin` scope 在 body 缺失时进入 422，证明 router 已挂载且 scope 依赖优先于 body 校验。
   - `app.user_middleware` 的 class names 精确为 `CORSMiddleware`, `SecurityHeadersMiddleware`, `RateLimitMiddleware`, `RequestContextMiddleware`（环境默认 `RATE_LIMIT_ENABLED=true`）；这也是外层到内层的运行顺序。
   - patch `_get_redis=lambda: None`、清空 `_memory_store`，设置 `RATE_LIMIT=2`，将 `server_api.verify_api_key` override 为 401；连续三次 `/product/search` 应为 `[401, 401, 429]`，证明限流在鉴权前且不是替代鉴权。
   - `GET /health`、业务 401 和限流 429 都断言 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy` 存在；使用 `CORS_ORIGINS=http://test.example` 的 preflight 断言 `access-control-allow-origin` 正确，默认空 origins 不放行任意第三方 origin。
3. 新增/改造 `tests/api/test_auth_router_mounts.py` 的 `AUTH_ENABLED=false` 回归：在导入 app 前设置环境，先 reload `memos.api.middleware.auth`，再 reload `memos.api.server_api`（清理依赖缓存和 overrides），保证 admin 条件挂载与真实 `verify_api_key` 使用同一个启动时配置快照。断言 `/product/search` 无 key 时沿现有 bypass 路径到达 mock handler、auth context 含 `auth_bypassed=True`，但 `/admin/keys` 和 `/admin/health` 均为 404；断言 RateLimit 仍在 middleware stack 中。测试结束恢复环境，并按相同顺序重新加载默认模块，避免模块级状态污染。
4. 为 shim 增加轻量测试（可放同一文件）：`server_api_ext.app is server_api.app`，且 ext 模块不存在第二个 `FastAPI` 实例/独立 user middleware；该测试只验证兼容别名，不再把 ext 当作第二个入口测试。
5. 增加 `tests/api/test_security_middleware.py`（或同文件集中测试）直接实例化公共 `SecurityHeadersMiddleware`，验证其五个 header 值，避免未来删除 shim 时失去类级回归。

这些测试在当前代码上应先失败：server_api 缺 admin/rate-limit/security/CORS，health 字段仍带版本，ext 仍是独立 app，且 middleware 顺序断言不成立。

### 阶段 B：实现最小改动

1. 新建 `middleware/security.py`，从 ext 原样迁移安全头；在 `middleware/__init__.py` 导出。
2. 按“完整目标代码”改造 `server_api.py`：导入公共 middleware/CORS/admin；按四行注册顺序组装；按 `AUTH_ENABLED` 条件挂载 admin；health 改为无指纹响应；保留所有现有异常 handler、plugin、download、RequestContext、业务 router 和 P0 逻辑。
3. 将 ext 替换为 shim，并把其 `__main__` 的 uvicorn module 也指向 `server_api`。
4. 更新 `Dockerfile.krolik`、README、双语部署文档、必要的 `.env.example` 和 stale 注释；确认 `docker/Dockerfile`/compose 不出现 ext 运行引用。

### 阶段 C：逐层验收

```powershell
# 先执行新增/改造的快速测试
python -m pytest tests/api/test_auth_router_mounts.py tests/api/test_security_middleware.py -q

# API 相关回归
python -m pytest tests/api/ -q

# 228 测试基线（以仓库当前测试收集数为准，验收记录必须贴真实输出）
python -m pytest -q

# 代码格式与 lint
make format

# API 变更后的 OpenAPI 快照
make openapi
git diff -- docs/openapi.json
```

若环境没有 Poetry 或 Make wrapper，使用项目实际可用的等价命令，但验收记录必须包含真实 pytest 输出；不得仅凭静态检查声称“228 测试全绿”。`make openapi` 后确认只出现预期的 admin paths/中间件不影响 schema，25 个既有 operation 的 schema 无差异。

## 测试断言细节

### 单一入口能力存在性

- `server_api.app.routes` 包含 `/download` mount、`/health`、25 个 `/product/*` operation，以及鉴权开启时的 `/admin/*`。
- `app.openapi()` 的 `/product/*` 数量仍为 25，每个 operation 的 security requirement 含 `APIKeyHeader`。
- `app.user_middleware` 顺序如上；当 `RATE_LIMIT_ENABLED=false` 重新导入 app 后，顺序应为 CORS、Security、RequestContext，且不存在 RateLimit。

### `AUTH_ENABLED=false` 回归

- 业务鉴权依赖按现有 bypass 语义返回 `auth_bypassed=True`/`X-User-Name` 身份，原 handler 的 body/query fallback 不变（复用 `tests/api/test_auth.py` 和 cube access 的既有覆盖）。
- admin router 完全未注册：所有 admin paths 不在 OpenAPI，HTTP 请求返回 404，而不是 401/403/422/500。
- RateLimit 与安全头仍生效；这两个保护层不能因鉴权关闭而被条件移除。

### 限流先于鉴权

除了列表顺序断言，还要用请求序列断言：同一 client/key 在限制内收到 auth 的 401，超过限制收到 429；第三次不应执行 `verify_api_key` override 或业务 handler。这样可以防止未来有人只调整列表而改变实际执行顺序。

## 影响面与回滚

### 影响面

- 应用启动：默认 8000 入口从“基础能力”升级为完整能力；`server_api_ext` 的 8001 独立行为消失，任何仍指向 8001 的部署需改为 8000。
- 管理员：鉴权开启后可从 8000 直接签发/吊销 key；鉴权关闭时管理面消失，必须重新开启并重启后操作。
- 客户端：25 个业务接口契约不变；健康响应字段收窄为 status，依赖旧 `version/service/auth_enabled/rate_limit_enabled` 字段的探针需更新。
- 运维：CORS 需通过 `CORS_ORIGINS` 明确配置；限流默认开启，Redis 缺失仍是本地回退，不应当作禁用。

### 回滚

回滚应回到同一分支上一个已验证提交，不通过把 Docker 重新指向 ext 来“回滚”，因为 ext 是被废弃的平行实现。若必须临时绕过业务鉴权，仅允许在隔离环境显式设置 `AUTH_ENABLED=false`，仍保留限流和 admin 404 策略；生产回滚后应恢复 `AUTH_ENABLED=true`。

## 最终验收清单

- [ ] `server_api` 是唯一真实 FastAPI app；`server_api_ext.app` 仅为同一 app 的兼容别名。
- [ ] `/download`、plugins、RequestContext/trace、25 个业务路由鉴权在 `server_api` 上继续存在。
- [ ] `admin_router` 仅在 `AUTH_ENABLED=true` 时挂载；敏感 admin 操作仍 require admin scope；false 时 `/admin/*` 全部 404。
- [ ] `RATE_LIMIT_ENABLED` 默认 true；关闭时没有 RateLimit；AUTH false 不影响限流开关；Redis 不可用回退内存。
- [ ] `SecurityHeadersMiddleware` 位于 `middleware/security.py`，并为健康、业务错误、429 响应提供安全头。
- [ ] CORS 配置只接受 `CORS_ORIGINS`，默认空列表；预检和错误响应行为有测试。
- [ ] 运行时 middleware 顺序断言为 CORS -> SecurityHeaders -> RateLimit -> RequestContext，且行为测试证明 RateLimit 先于 `verify_api_key`。
- [ ] `/health` 无版本/部署/开关指纹，只返回 healthy status，并保持公开与限流豁免。
- [ ] Dockerfile、Dockerfile.krolik、Compose、README、中文/英文部署文档和可执行脚本不再使用 `server_api_ext:app`。
- [ ] `cli.py:get_openapi_app()` 仍直接导入 `server_api`；OpenAPI 25 个既有 operation 无意外变化。
- [ ] P0 加固全部保留：403 专用异常、`get_current_user`、Cube ACL、日志脱敏、限流 key 哈希、XFF 防伪造、非 root Docker、CORS 配置化、health 去指纹、restart 策略。
- [ ] 先失败测试、后实现、再 `make format`、API 回归和全量 pytest；验收记录贴出真实结果，目标为仓库约定的 228 tests 全绿。

## 开放实施注意事项

- app 模块级条件挂载意味着修改 `AUTH_ENABLED` 后需重启 worker；测试必须通过 reload/隔离进程验证，不要在同一已导入 app 上只 monkeypatch 环境变量。
- `StaticFiles(directory=FILE_LOCAL_PATH)` 沿用现有部署要求；本方案不扩大 `/download` 的授权范围。若下载内容含用户数据，应另行设计静态文件鉴权。
- 不新增第三方依赖；`redis`、`psycopg2` 等现有可选依赖继续按当前 guarded import/extra 规则处理。
- 生产加固的未提交改动属于本任务前置状态，实施时必须先确认没有被其他提交覆盖，再按本方案做最小合并。
