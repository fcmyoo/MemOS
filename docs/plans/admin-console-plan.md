# MemOS 管理后台与用户自助门户实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在不改变现有业务 API、Cube ACL 和 krlk_* key contract 的前提下，增加管理员 Web 后台、用户自助门户、密码登录和严格的 ROOT/ADMIN/USER 授权。

**Architecture:** 用户与 Cube 继续存放在 MEMOS_DIR/memos_users.db（SQLite）；API key 继续存放在 PostgreSQL api_keys 表。新增 Argon2id 密码字段和 SQLite 服务端 session 表。浏览器只拿 HttpOnly session cookie，业务 API 仍只接受 master key/API key；管理 API 通过混合依赖兼容 session、master key 和已有 admin-scope API key。

**Tech Stack:** FastAPI、Pydantic v2、SQLAlchemy 2、SQLite、PostgreSQL、Argon2id (argon2-cffi)、原生 HTML/CSS/ES2022，无前端构建链。

---

## 0. 范围与现状基线

已审读：

- docs/plans/admin-console-audit.md
- src/memos/mem_user/user_manager.py
- src/memos/api/routers/admin_router.py
- src/memos/api/utils/api_keys.py
- src/memos/api/middleware/auth.py
- src/memos/api/server_api.py
- src/memos/api/access_control.py
- src/memos/api/product_models.py

当前关键事实：

1. User 只有 user_id/user_name/role/is_active 等字段；Base.metadata.create_all() 不会给已有 SQLite 表补列。
2. UserManager() 默认连接 MEMOS_DIR/memos_users.db，Cube 与用户关联也在此库。
3. api_keys 在 PostgreSQL，create_api_key_in_db() 将 user_name 作为 owner，实际 key 只在创建响应中出现一次。
4. verify_api_key() 支持 mk_*、krlk_*、内部请求和 AUTH_ENABLED=false bypass；server_api 仅在 AUTH_ENABLED=true 时挂载现有 admin_router。
5. CubeAccessControl 只负责业务 Cube ACL。本方案不把 Web session 当成业务 API credential，不改 25 个业务端点的请求/响应 contract。

非目标：邮箱验证、找回密码邮件、OAuth/LDAP、细粒度 API key scope 新枚举、跨数据库分布式事务、修改既有业务路由。

## 1. 七个关键决策

| 决策点 | 明确选择 | 理由与边界 |
|---|---|---|
| 1. 密码哈希 | **Argon2id，使用 argon2-cffi.PasswordHasher**，memory=64 MiB、time=3、parallelism=4、hash_len=32、salt_len=16；数据库只存 PHC 字符串 | 抗 GPU/ASIC，参数可自描述并支持 check_needs_rehash()。不自行加盐、不记录明文。argon2-cffi 放入新的 web-console optional extra，并加入 all；实现用 try/except ImportError 提示安装 MemoryOS[web-console]。按项目要求，改 pyproject.toml 前需单独获批。 |
| 2. 会话机制 | **服务端 opaque session，不用 JWT**。随机 32 字节 token 只以 SHA-256 存 web_sessions；cookie 为 memos_session | 可立即吊销、登出生效、改密可批量撤销；避免 JWT 无法撤销和浏览器 localStorage XSS 风险。SQLite 是单机部署的权威 session store，多 worker 共享同一文件。 |
| 3. Web 前端 | **轻量 HTML+CSS+原生 JS**，挂 /console/，无 React/Vite/Node 构建 | 审计约束要求不引入重型构建链；静态资源少、可随 Python 包发布、同源 cookie 简单。一个入口根据角色显示管理员或用户视图。 |
| 4. /admin/keys 与 /me/keys | **并存**：保留 /admin/keys 的请求/响应模型和 master-key 能力；新增 /me/keys，owner 永远取 session 的 user_name | 管理员仍可代签、查全量、吊销任意 key；普通用户只能自建、列出、吊销自己的 key。普通 API key 访问 admin 面仍需 admin scope 且映射到 ROOT/ADMIN 用户。 |
| 5. master key 与 ROOT/ADMIN | **并存且分层**：master key 是不落库的 ROOT break-glass credential；ROOT/ADMIN 密码登录是可审计的数据库身份 | master key 继续用于 /admin/keys、/admin/generate-master-key 和首次设置 ROOT 密码；master 不创建 Web session，也不出现在用户列表。ROOT 可管理所有角色；ADMIN 可管理 USER/GUEST，不可创建、修改、删除 ROOT 或 ADMIN。 |
| 6. AUTH_ENABLED=false | **Web 面完全关闭**：不挂载 /auth/*、新 /admin/users、/me/* 和 /console/；路径返回 404。现有业务路由保留原 bypass 身份，RateLimit 与安全头仍启用 | 与现有 server_api 的 fail-closed admin 挂载一致，避免开发 bypass 意外暴露用户管理和签 key；切换需重启进程。 |
| 7. session 与 API key | **不统一**：session 仅用于 Web console 路由；krlk_*/mk_* 仍用于业务 API 和兼容的 admin key API | 两种 credential 的生命周期、撤销和泄漏面不同。/me/keys 创建出的 key 可调用业务 API，但 session 不能调用 /product/*；不会把 session token 写进 api_keys。 |

兼容边界：

- CreateKeyRequest.scopes、api_keys PostgreSQL schema、key 前缀和一次性返回语义不改。
- 现有 /admin/keys 的字段和状态码语义保持，只把认证依赖替换为混合管理员依赖。
- 业务 /product/* 仍由 verify_api_key 和 CubeAccessControl 保护；Web session 不进入 access_control.is_privileged()。

## 2. 目标数据流与文件边界

~~~text
浏览器 -- same-origin cookie --> /auth/login, /auth/logout, /me/*, /admin/users
浏览器 -- same-origin static --> /console/
脚本   -- Authorization: Bearer mk_*/krlk_* --> /product/*、兼容 /admin/keys

/auth|/me|/admin/users
    -> web_auth.py: session 校验 + CSRF + 角色校验
    -> UserManager(SQLite): users/cubes/sessions

/admin/keys|/me/keys
    -> api_keys.py: PostgreSQL api_keys
    -> owner 约束（/me）或管理员全量权限（/admin）
~~~

### 2.1 文件清单

| 文件 | 动作 | 单一职责 |
|---|---|---|
| src/memos/mem_user/user_manager.py | 修改 | User 新字段、WebSession ORM、幂等 SQLite migration、用户/角色/Cube 分配服务方法 |
| src/memos/api/web_auth.py | 新建 | Argon2id、opaque session、cookie、CSRF、混合 principal 依赖 |
| src/memos/api/console_models.py | 新建 | 登录、用户 CRUD、profile、key 自助、cube 分配的 Pydantic v2 schema |
| src/memos/api/routers/auth_router.py | 新建 | /auth/login、/auth/logout |
| src/memos/api/routers/me_router.py | 新建 | /me/profile、/me/keys |
| src/memos/api/routers/admin_router.py | 修改 | 保留既有 key 路由；增加用户/cube CRUD；认证改为 require_admin_principal |
| src/memos/api/utils/api_keys.py | 修改 | 增加带 owner 条件的原子吊销函数，不改表结构 |
| src/memos/api/middleware/auth.py | 修改 | AuthContext 增加 user_id/role/auth_type 可选字段；现有 API key 验证逻辑不变 |
| src/memos/api/server_api.py | 修改 | AUTH_ENABLED 条件挂载新 router 和 /console 静态目录；保持 middleware 顺序 |
| src/memos/api/static/console/index.html | 新建 | 单页壳、登录表单、管理员/用户容器 |
| src/memos/api/static/console/app.js | 新建 | fetch client、路由状态、表格/弹窗、CSRF header、一次性 key 展示 |
| src/memos/api/static/console/styles.css | 新建 | 响应式后台与门户样式 |
| tests/api/test_web_auth.py | 新建 | 登录/session/CSRF/过期/限流 |
| tests/api/test_admin_console.py | 新建 | RBAC、admin CRUD、Cube 分配和 key 代签 |
| tests/api/test_me_keys.py | 新建 | 自助 key CRUD 与越权隔离 |
| tests/mem_user/test_user_schema_migration.py | 新建 | 旧 SQLite migration、幂等性、哈希字段 |
| docs/openapi.json | 生成 | make openapi 后提交新 schema |
| pyproject.toml、poetry.lock、Docker 安装清单 | 需获批后修改 | web-console extra（argon2-cffi）和部署安装 |

## 3. SQLite migration 与模型方案

### 3.1 新模型字段

在 User 中新增：

~~~python
password_hash = Column(String(512), nullable=True)
password_updated_at = Column(DateTime, nullable=True)
last_login_at = Column(DateTime, nullable=True)
~~~

password_hash=NULL 表示历史用户尚未设置 Web 密码，不允许登录但不影响现有 API key/Cube 行为。user_name 在 v1 中作为登录名和 API key owner，**创建后不可修改**，避免 SQLite 与 PostgreSQL 跨库 rename 不一致。

新增 SQLAlchemy 模型：

~~~python
class WebSession(Base):
    __tablename__ = "web_sessions"

    session_id_hash = Column(String(64), primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id"), nullable=False, index=True)
    csrf_token_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.now, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    user = relationship("User")
~~~

### 3.2 可直接执行的 SQLite SQL

SQLite 不支持 ALTER TABLE ... ADD COLUMN IF NOT EXISTS，因此发布代码先检查 PRAGMA table_info(users)，只对缺失列执行下列 ALTER；整个过程使用 BEGIN IMMEDIATE，失败回滚：

~~~sql
BEGIN IMMEDIATE;

ALTER TABLE users ADD COLUMN password_hash VARCHAR(512);
ALTER TABLE users ADD COLUMN password_updated_at DATETIME;
ALTER TABLE users ADD COLUMN last_login_at DATETIME;

CREATE TABLE IF NOT EXISTS web_sessions (
    session_id_hash VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR NOT NULL REFERENCES users(user_id),
    csrf_token_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    revoked_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_user_active
    ON web_sessions(user_id, expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_web_sessions_expiration
    ON web_sessions(expires_at);

PRAGMA user_version = 2;
COMMIT;
~~~

### 3.3 UserManager 精确改动

__init__ 的顺序改为：

~~~python
self.engine = create_engine(
    f"sqlite:///{db_path}",
    echo=False,
    connect_args={"check_same_thread": False},
)
self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
Base.metadata.create_all(bind=self.engine)
self._migrate_schema()
self._init_root_user(user_id)
~~~

连接初始化还必须执行 PRAGMA foreign_keys=ON、PRAGMA journal_mode=WAL、PRAGMA busy_timeout=5000，避免外键失效和多 worker 短时锁冲突。_migrate_schema() 必须幂等：读取 PRAGMA table_info(users)，按列名集合逐列执行 ALTER TABLE；创建 web_sessions 与索引；提交后将 PRAGMA user_version 置为 2。旧数据库备份由发布脚本在启动前复制为 memos_users.db.bak-<UTC timestamp>，应用不删除备份。

新增并测试以下服务方法，原有方法默认行为保持：

~~~python
def create_user_with_password(
    self,
    user_name: str,
    password_hash: str,
    role: UserRole = UserRole.USER,
    user_id: str | None = None,
) -> User: ...

def update_user(
    self,
    user_id: str,
    *,
    role: UserRole | None = None,
    is_active: bool | None = None,
    password_hash: str | None = None,
) -> User | None: ...

def list_users(self, include_inactive: bool = False) -> list[User]: ...
def list_cubes(self, include_inactive: bool = False) -> list[Cube]: ...
def set_user_cubes(self, user_id: str, cube_ids: list[str]) -> list[Cube]: ...

def create_web_session(
    self,
    user_id: str,
    session_id_hash: str,
    csrf_token_hash: str,
    expires_at: datetime,
) -> None: ...
def get_web_session(self, session_id_hash: str) -> WebSession | None: ...
def revoke_web_session(self, session_id_hash: str) -> bool: ...
def revoke_all_web_sessions(self, user_id: str) -> int: ...
~~~

所有 session 查询必须同时过滤 revoked_at IS NULL、expires_at > now、用户 is_active=True；set_user_cubes() 校验用户和每个 Cube 均存在且 active，使用一个事务替换 association，Cube owner 关系不能被移除。不要改现有 create_user() 的幂等语义，业务/MCP 调用继续兼容。

UserManager 当前会在关闭 Session 后返回 ORM 对象，因此新增查询必须用 selectinload(User.cubes) 或在 Session 内构造不可变 DTO；get_web_session() 使用 joinedload(WebSession.user)。禁止让路由在 detached ORM 上触发 lazy load。

## 4. 认证、session、CSRF 目标代码

### 4.1 src/memos/api/web_auth.py

~~~python
from __future__ import annotations

from memos.exceptions import ConfigurationError

SESSION_COOKIE = "memos_session"
CSRF_COOKIE = "memos_csrf"
SESSION_TTL = int(os.getenv("WEB_SESSION_TTL_SEC", "28800"))
COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "true").lower() == "true"

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
except ImportError:  # optional web-console extra
    PasswordHasher = None
    InvalidHashError = VerificationError = VerifyMismatchError = ValueError

_PASSWORD_HASHER: PasswordHasher | None = None

def _get_password_hasher() -> PasswordHasher:
    global _PASSWORD_HASHER
    if _PASSWORD_HASHER is None:
        if PasswordHasher is None:
            raise ConfigurationError(
                "Web console password support requires MemoryOS[web-console]"
            )
        _PASSWORD_HASHER = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )
    return _PASSWORD_HASHER

def validate_password_strength(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise HTTPException(422, "Password must be 12-128 characters")
    patterns = (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    if not all(re.search(pattern, password) for pattern in patterns):
        raise HTTPException(422, "Password must contain upper, lower, digit and symbol")

def hash_password(password: str) -> str:
    validate_password_strength(password)
    return _get_password_hasher().hash(password)

def verify_password(password_hash: str | None, password: str) -> bool:
    candidate_hash = password_hash or _get_dummy_hash()
    try:
        return _get_password_hasher().verify(candidate_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False

@functools.lru_cache(maxsize=1)
def _get_dummy_hash() -> str:
    return _get_password_hasher().hash(secrets.token_urlsafe(32))

def password_needs_rehash(password_hash: str) -> bool:
    return _get_password_hasher().check_needs_rehash(password_hash)

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def issue_session(
    user_id: str,
    user_manager: UserManager,
) -> tuple[str, str, datetime]:
    raw_session = secrets.token_urlsafe(32)
    raw_csrf = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=SESSION_TTL)
    user_manager.create_web_session(
        user_id,
        _digest(raw_session),
        _digest(raw_csrf),
        expires_at,
    )
    return raw_session, raw_csrf, expires_at

async def require_session(request: Request) -> WebPrincipal:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(401, "Authentication required")
    session = get_user_manager().get_web_session(_digest(raw))
    if session is None or session.user is None or not session.user.is_active:
        raise HTTPException(401, "Authentication required")
    principal = WebPrincipal(
        user_id=session.user.user_id,
        user_name=session.user.user_name,
        role=session.user.role.value,
        session_id_hash=_digest(raw),
    )
    request.state.web_principal = principal
    return principal

async def require_csrf(
    request: Request,
    principal: WebPrincipal = Depends(require_session),
) -> WebPrincipal:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        cookie = request.cookies.get(CSRF_COOKIE)
        header = request.headers.get("X-CSRF-Token")
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise HTTPException(403, "CSRF validation failed")
        session = get_user_manager().get_web_session(principal.session_id_hash)
        if not session or not hmac.compare_digest(
            session.csrf_token_hash,
            _digest(header),
        ):
            raise HTTPException(403, "CSRF validation failed")
    return principal
~~~

实际代码通过 FastAPI dependency override 注入 UserManager，使测试传入临时 tmp_path/memos_users.db；WebPrincipal 为 frozen dataclass。PasswordHasher 必须由 _get_password_hasher() 延迟加载：缺少 argon2-cffi 时登录/设密返回清晰的安装 extra 错误，但现有 master/API key 调用 /admin/keys 不受影响，也不能降级到弱哈希。

### 4.2 API key/session 混合管理员依赖

~~~python
async def require_admin_principal(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    api_key: str | None = Security(API_KEY_HEADER),
) -> WebPrincipal | AuthContext:
    if session_cookie:
        principal = await require_session(request)
        if principal.role not in {"ROOT", "ADMIN"}:
            raise HTTPException(403, "Administrator role required")
        return principal
    if not api_key:
        raise HTTPException(401, "Authentication required")

    auth = await verify_api_key(request, api_key)
    if auth.get("is_internal"):
        raise HTTPException(403, "Administrator role required")
    if auth.get("is_master_key"):
        return {**auth, "role": "ROOT", "auth_type": "master"}

    user = get_user_manager().get_user_by_name(auth["user_name"])
    if user is None or not user.is_active:
        raise HTTPException(403, "Administrator role required")
    if user.role not in {UserRole.ROOT, UserRole.ADMIN}:
        raise HTTPException(403, "Administrator role required")
    if "admin" not in auth.get("scopes", []) and "all" not in auth.get("scopes", []):
        raise HTTPException(403, "Administrator role required")
    return {
        **auth,
        "user_id": user.user_id,
        "role": user.role.value,
        "auth_type": "api_key",
    }
~~~

require_admin_write(request) 先调用 require_admin_principal，再对 session principal 调 require_csrf；master/API key principal 直接通过。get_user_manager() 返回 server_api 与新 router 共享的 UserManager dependency；admin_db_connection() 复用 admin_router 当前的 PostgreSQL 连接配置；principal_name() 对 session 取 user_name、对 AuthContext 取 user_name。require_target_user() 对不存在 user 返回 404；require_target_user_by_name() 对不存在或 inactive user 返回 404，不能把数据库异常文本回给客户端。AUTH_ENABLED=false 时不会调用这些依赖，因为新 router 不挂载。

## 5. 后端 API 设计

### 5.1 Schema

src/memos/api/console_models.py：

~~~python
class LoginRequest(BaseModel):
    user_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

class PrincipalResponse(BaseModel):
    user_id: str
    user_name: str
    role: Literal["ROOT", "ADMIN", "USER", "GUEST"]

class LoginResponse(BaseModel):
    user: PrincipalResponse
    expires_at: datetime

class UserCreateRequest(BaseModel):
    user_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.@-]+$",
    )
    password: str = Field(..., min_length=12, max_length=128)
    role: Literal["ROOT", "ADMIN", "USER", "GUEST"] = "USER"
    cube_ids: list[str] = Field(default_factory=list, max_length=100)

class UserUpdateRequest(BaseModel):
    password: str | None = Field(None, min_length=12, max_length=128)
    role: Literal["ROOT", "ADMIN", "USER", "GUEST"] | None = None
    is_active: bool | None = None

class CubeAssignmentRequest(BaseModel):
    cube_ids: list[str] = Field(default_factory=list, max_length=100)

class UserResponse(BaseModel):
    user_id: str
    user_name: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    cube_ids: list[str]

class ProfileUpdateRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=12, max_length=128)

class CubeResponse(BaseModel):
    cube_id: str
    cube_name: str
    owner_id: str
    is_active: bool

class KeyCreateRequest(BaseModel):
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    description: str | None = Field(None, max_length=500)
    expires_in_days: int | None = Field(None, ge=1, le=365)
~~~

密码字段只出现在 request，任何 response 都不含 password_hash。KeyCreateRequest.scopes 透传既有 CreateKeyRequest.scopes，不新增枚举、不改变数据库约束。/me/keys 的 create response 复用现有 CreateKeyResponse shape，只一次返回 key。

### 5.2 /auth/login 与 /auth/logout

src/memos/api/routers/auth_router.py：

~~~python
@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    user_manager: UserManager = Depends(get_user_manager),
) -> LoginResponse:
    login_rate_limiter.enforce(request, payload.user_name)
    user = user_manager.get_user_by_name(payload.user_name)
    try:
        valid = verify_password(user.password_hash if user else None, payload.password)
    except ConfigurationError as exc:
        raise HTTPException(503, "Web console password support is not installed") from exc
    if user is None or not user.is_active or not valid:
        raise HTTPException(401, "Invalid username or password")
    if user.password_hash and password_needs_rehash(user.password_hash):
        user_manager.update_user(
            user.user_id,
            password_hash=hash_password(payload.password),
        )
    user_manager.mark_login(user.user_id, datetime.now())
    raw_session, raw_csrf, expires_at = issue_session(user.user_id, user_manager)
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_TTL,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        raw_csrf,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_TTL,
        path="/",
    )
    return LoginResponse(user=to_principal(user), expires_at=expires_at)

@router.post("/logout", status_code=204)
def logout(
    response: Response,
    principal: WebPrincipal = Depends(require_csrf),
    user_manager: UserManager = Depends(get_user_manager),
) -> Response:
    user_manager.revoke_web_session(principal.session_id_hash)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = 204
    return response
~~~

登录失败统一 401，不区分用户不存在、停用、未设密码或密码错误；不存在用户也验证由 _get_dummy_hash() 缓存的 Argon2id dummy hash，降低时序枚举。新增 /auth/login 专用 bucket（IP+规范化用户名，默认 10 次/分钟），复用 RateLimitMiddleware 的 Redis/内存滑动窗口；429 同样不泄露账号状态。

### 5.3 /admin/users 与 Cube 分配

所有端点使用 principal=Depends(require_admin_principal)，写操作使用 require_admin_write（内部调用 require_csrf_if_session）。权限服务 assert_can_manage(actor, target, requested_role) 遵循：

- master/ROOT：可管理数据库用户和分配 ROOT/ADMIN/USER/GUEST；不能删除或停用最后一个 ROOT。
- ADMIN：只能创建、修改、停用、删除 USER/GUEST；目标或请求角色为 ROOT/ADMIN 一律 403。
- user_name 创建后不可修改；任何角色不能通过 /me 自提权。

精确 endpoint diff：

~~~python
@router.get("/users", response_model=list[UserResponse])
def list_users(
    include_inactive: bool = True,
    principal=Depends(require_admin_principal),
):
    return [
        serialize_user(user)
        for user in user_manager.list_users(include_inactive=include_inactive)
    ]

@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(
    payload: UserCreateRequest,
    principal=Depends(require_admin_write),
):
    assert_can_manage(principal, target=None, requested_role=payload.role)
    user = user_manager.create_user_with_password(
        payload.user_name,
        hash_password(payload.password),
        UserRole(payload.role),
    )
    user_manager.set_user_cubes(user.user_id, payload.cube_ids)
    return serialize_user(user_manager.get_user(user.user_id))

@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(user_id: str, principal=Depends(require_admin_principal)):
    user = user_manager.get_user(user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return serialize_user(user)

@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    principal=Depends(require_admin_write),
):
    target = require_target_user(user_id)
    assert_can_manage(principal, target=target, requested_role=payload.role)
    updated = user_manager.update_user(
        user_id,
        role=UserRole(payload.role) if payload.role else None,
        is_active=payload.is_active,
        password_hash=hash_password(payload.password) if payload.password else None,
    )
    if payload.password or payload.is_active is False:
        user_manager.revoke_all_web_sessions(user_id)
    return serialize_user(updated)

@router.delete("/users/{user_id}", response_model=SimpleResponse)
def delete_user(user_id: str, principal=Depends(require_admin_write)):
    target = require_target_user(user_id)
    assert_can_manage(principal, target=target, requested_role=None)
    if not user_manager.delete_user(user_id):
        raise HTTPException(409, "User cannot be deleted")
    user_manager.revoke_all_web_sessions(user_id)
    return SimpleResponse(message="User deactivated")

@router.get("/cubes", response_model=list[CubeResponse])
def list_cubes(principal=Depends(require_admin_principal)):
    return [
        serialize_cube(cube)
        for cube in user_manager.list_cubes(include_inactive=False)
    ]

@router.put("/users/{user_id}/cubes", response_model=list[CubeResponse])
def assign_cubes(
    user_id: str,
    payload: CubeAssignmentRequest,
    principal=Depends(require_admin_write),
):
    target = require_target_user(user_id)
    assert_can_manage(principal, target=target, requested_role=None)
    return [
        serialize_cube(cube)
        for cube in user_manager.set_user_cubes(user_id, payload.cube_ids)
    ]
~~~

管理员查不存在用户返回 404；普通用户所有 /admin/* 在依赖层固定 403，不能通过 user_id 枚举。POST /admin/users 若用户名冲突返回 409；无效 Cube 返回 422；数据库失败统一 500 且响应不含底层异常。

### 5.4 现有 /admin/keys 的精确改造

保留 CreateKeyRequest、CreateKeyResponse、KeyListResponse、路径和字段不变，仅将 require_scope("admin") + verify_api_key 替换为混合管理员依赖：

~~~python
@router.post("/keys", response_model=CreateKeyResponse)
def create_key(
    request: CreateKeyRequest,
    principal=Depends(require_admin_write),
):
    require_target_user_by_name(request.user_name, principal)
    with admin_db_connection() as conn:
        api_key = create_api_key_in_db(
            conn=conn,
            user_name=request.user_name,
            scopes=request.scopes,
            description=request.description,
            expires_in_days=request.expires_in_days,
            created_by=principal_name(principal),
        )
    return CreateKeyResponse(
        message="API key created successfully. Store this key securely - it won't be shown again!",
        key=api_key.key,
        key_prefix=api_key.key_prefix,
        user_name=request.user_name,
        scopes=request.scopes,
    )

@router.get("/keys", response_model=KeyListResponse)
def list_keys(
    user_name: str | None = None,
    principal=Depends(require_admin_principal),
):
    if user_name:
        require_target_user_by_name(user_name, principal)
    with admin_db_connection() as conn:
        keys = list_api_keys(conn, user_name=user_name)
    return KeyListResponse(message=f"Found {len(keys)} key(s)", keys=keys)

@router.delete("/keys/{key_id}", response_model=SimpleResponse)
def revoke_key(
    key_id: str,
    principal=Depends(require_admin_write),
):
    with admin_db_connection() as conn:
        if not revoke_api_key(conn, key_id):
            raise HTTPException(404, "API key not found or already revoked")
    return SimpleResponse(message="API key revoked successfully")
~~~

/admin/generate-master-key 保留原路径，只允许 is_master_key=True；session ROOT/ADMIN 和 admin-scope key 均 403。日志只记录操作者 user id、key id 和结果，不记录完整 key、hash、session 或密码。

### 5.5 /me/profile 与 /me/keys

src/memos/api/routers/me_router.py：

~~~python
@router.get("/profile", response_model=UserResponse)
def profile(principal=Depends(require_session)):
    return serialize_user(user_manager.get_user(principal.user_id))

@router.patch("/profile", response_model=UserResponse)
def update_profile(
    payload: ProfileUpdateRequest,
    principal=Depends(require_csrf),
):
    user = user_manager.get_user(principal.user_id)
    if not verify_password(user.password_hash, payload.current_password):
        raise HTTPException(400, "Current password is incorrect")
    user_manager.update_user(
        principal.user_id,
        password_hash=hash_password(payload.new_password),
    )
    user_manager.revoke_all_web_sessions(principal.user_id)
    return serialize_user(user_manager.get_user(principal.user_id))

@router.post("/keys", response_model=CreateKeyResponse, status_code=201)
def create_self_key(
    payload: KeyCreateRequest,
    principal=Depends(require_csrf),
):
    with admin_db_connection() as conn:
        key = create_api_key_in_db(
            conn=conn,
            user_name=principal.user_name,
            scopes=payload.scopes,
            description=payload.description,
            expires_in_days=payload.expires_in_days,
            created_by=principal.user_name,
        )
    return CreateKeyResponse(
        message="API key created successfully. Store this key securely - it won't be shown again!",
        key=key.key,
        key_prefix=key.key_prefix,
        user_name=principal.user_name,
        scopes=payload.scopes,
    )

@router.get("/keys", response_model=KeyListResponse)
def list_self_keys(principal=Depends(require_session)):
    with admin_db_connection() as conn:
        keys = list_api_keys(conn, user_name=principal.user_name)
    return KeyListResponse(message=f"Found {len(keys)} key(s)", keys=keys)

@router.delete("/keys/{key_id}", response_model=SimpleResponse)
def revoke_self_key(
    key_id: str,
    principal=Depends(require_csrf),
):
    with admin_db_connection() as conn:
        if not revoke_api_key_for_user(conn, key_id, principal.user_name):
            raise HTTPException(403, "Insufficient key access")
    return SimpleResponse(message="API key revoked successfully")
~~~

revoke_api_key_for_user() 必须使用单条原子 SQL：

~~~sql
UPDATE api_keys SET is_active = FALSE
WHERE id = %s AND user_name = %s AND is_active = TRUE;
~~~

不存在、已吊销和他人 key 对 /me 统一 403，防止 key id 枚举；管理员路径保持 404 语义。改密成功后当前 session 也被撤销，前端收到响应后立即回登录页。

## 6. RBAC 访问矩阵

| 路由 | ROOT session | ADMIN session | USER/GUEST session | master key | admin-scope krlk_* | 普通 krlk_*/无凭证 |
|---|---:|---:|---:|---:|---:|---:|
| /auth/login | 公共（AUTH_ENABLED=true） | 同左 | 同左 | 同左 | 同左 | 同左 |
| /auth/logout | 204 当前 session | 204 当前 session | 204 当前 session | 401 | 401 | 401 |
| /admin/users* | 全部 | 仅 USER/GUEST 目标 | 403 | 全部 | 映射到 ROOT/ADMIN 且有 admin scope | 403 |
| /admin/cubes、/admin/users/*/cubes | 全部 | USER/GUEST 目标 | 403 | 全部 | ROOT/ADMIN + admin scope | 403 |
| /admin/keys | 全量、代签、吊销 | 全量、代签、吊销 | 403 | 全量、代签、吊销 | ROOT/ADMIN + admin scope | 403 |
| /admin/generate-master-key | 403 | 403 | 403 | 允许 | 403 | 403 |
| /me/profile | 仅自身 | 仅自身 | 仅自身 | 401 | 401 | 401 |
| /me/keys | 仅自身 | 仅自身 | 仅自身 | 401 | 401 | 401 |

资源规则：

- ROOT 是唯一可分配 ROOT/ADMIN 角色、修改或停用其他管理员的数据库角色；最后一个 ROOT 不可删除或停用。
- ADMIN 可完整管理 USER/GUEST，包括 CRUD、Cube 分配和代签 key，但任何 ROOT/ADMIN 目标写操作固定 403。
- USER/GUEST 只能访问自己的 /me/*；身份不从路径 user_id、query user_name 或请求体 owner 获取。
- /product/* 继续使用 CubeAccessControl；session 不被视为 privileged，不能绕过 Cube ACL。

## 7. Web 前端方案

### 7.1 目录与状态保持

~~~text
src/memos/api/static/console/
├── index.html
├── app.js
└── styles.css
~~~

GET /console/ 使用 StaticFiles(html=True)，仅 AUTH_ENABLED=true 挂载。页面不把 session、CSRF 或 key 放 localStorage/sessionStorage；请求统一 credentials: "include"。session 在 HttpOnly cookie，JS 只能读取单独的 CSRF cookie。

### 7.2 关键页面组件

- LoginView：用户名、密码、通用错误；成功后请求 /me/profile，按 role 进入 AdminShell 或 UserShell。
- AdminShell：当前用户与退出；Users、Cubes、API Keys 三个 tab。用户表支持 active 筛选、创建/编辑 dialog、软删除确认；Cube dialog 使用 /admin/cubes；key dialog 仅一次显示完整 key 和复制按钮，关闭后仅显示 prefix。
- UserShell：ProfilePanel（改密码）和 KeyTable（prefix/scopes/expiry/active）；支持创建、吊销和一次性 key dialog。
- apiFetch()：401 清理内存状态并回登录；403 显示固定无权限；所有写请求从 memos_csrf cookie 加 X-CSRF-Token；日志和错误 toast 不打印完整 response key。
- 交互质量：窄屏表格改为列表，modal 键盘可达，aria-live toast，按钮有稳定尺寸；不引入 CDN，保证离线自托管和 CSP 可控。

### 7.3 server_api.py 精确 diff

~~~python
from pathlib import Path

from memos.api.routers.auth_router import router as auth_router
from memos.api.routers.me_router import router as me_router

CONSOLE_DIR = Path(__file__).parent / "static" / "console"

app.include_router(
    server_router_module.router,
    dependencies=[Depends(verify_api_key)],
)
if AUTH_ENABLED:
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(admin_router)
    app.mount(
        "/console",
        StaticFiles(directory=CONSOLE_DIR, html=True),
        name="console",
    )
~~~

原 middleware 顺序和业务 router dependency 不变；admin_router 只 include 一次。静态资源纳入 Python package data，此项与 argon2-cffi 一起属于 pyproject.toml 审批范围。

## 8. 安全与运维

1. **密码**：12-128 字符，至少小写、大写、数字、符号各一；只存 Argon2id PHC；登录/admin 日志不记录密码、PHC 或原始 request body。
2. **限流**：全局 RateLimitMiddleware 保持；login bucket 默认 10 次/分钟/IP+用户名，复用 Redis/内存滑动窗口；429 不泄露账号是否存在。
3. **session**：绝对 TTL 默认 8 小时；每请求检查 active/revoked/expired；改密、停用、删除调用 revoke_all_web_sessions()；cookie 为 HttpOnly、SameSite=Lax、Secure（本地测试显式 WEB_COOKIE_SECURE=false）。
4. **CSRF**：所有 cookie 认证的 POST/PUT/PATCH/DELETE 使用双提交 token并对照 DB hash；登录不需要 CSRF；API key/master 调用不需要 CSRF。
5. **日志**：key 只记录 UUID 或 prefix 和操作结果，禁止完整 krlk_*、mk_*、session、CSRF、密码；异常响应不返回连接串、SQL 或 Python 异常文本。
6. **CORS/安全头**：沿用显式 CORS_ORIGINS、allow_credentials=True 和 SecurityHeadersMiddleware；console 推荐同源 HTTPS。
7. **停用**：Web session 每次回查 users.is_active；业务 API key 仍经现有 PostgreSQL 校验，Cube ACL 拒绝 inactive SQLite user。v1 不自动跨库撤销全部 key，管理员可通过 /admin/keys 吊销；若产品要求停用即撤销，需要单独定义跨库失败补偿，不能伪装为原子事务。
8. **首次启用**：历史 ROOT 的 password_hash 为 NULL，先用 master key 调 PATCH /admin/users/{root_id} 设置密码，再进行 Web 登录；无需把初始密码写入 env。其他历史用户由 ROOT/ADMIN 重置密码。

## 9. OpenAPI 与部署融合

- AUTH_ENABLED=true 时 OpenAPI 包含 /auth/*、/me/*、/admin/users*、/admin/cubes；/admin/keys 原模型不变。
- 完成路由后运行 make openapi，检查 docs/openapi.json 不出现 password_hash、session_id_hash、csrf_token_hash。
- AUTH_ENABLED=false 启动测试确认新路由和 /console/ 404，现有 /product/* bypass、RateLimit 和安全头不回归。
- pyproject.toml 新增 web-console = ["argon2-cffi (>=23.1,<26.0)"] 并加入 all；获批后运行 poetry lock --no-update。Docker 镜像安装 web-console extra。
- 不修改 api_keys SQL schema；/me/keys 复用现有 PostgreSQL 存储。SQLite migration 自动执行，发布前备份数据库。
- API 层新异常应翻译为 400/401/403/404/409/422，不把裸 Exception/RuntimeError 作为库级语义异常。

## 10. TDD 顺序与实现任务

顺序强制：每个任务先提交失败测试并确认失败，再写最小实现。测试用临时 SQLite、mock PostgreSQL cursor 和 TestClient，不访问真实 MEMOS_DIR 或凭据。

### Task 1: 旧 SQLite migration 与密码服务

**Files:** src/memos/mem_user/user_manager.py、src/memos/api/web_auth.py、tests/mem_user/test_user_schema_migration.py、tests/api/test_web_auth.py

- [ ] 写失败测试：旧 schema 启动后出现 password_hash/password_updated_at/last_login_at 和 web_sessions；重复启动无 duplicate column；hash 不等于明文；正确密码通过、错误密码失败、弱密码 422。
- [ ] 运行 poetry run pytest tests/mem_user/test_user_schema_migration.py tests/api/test_web_auth.py -q，预期 migration/hash helper 未实现而 FAIL。
- [ ] 实现 ORM 字段、_migrate_schema()、Argon2id helper 和 session CRUD；旧 create_user() 语义不改。
- [ ] 重跑同一命令，预期 PASS；运行 poetry run ruff check src/memos/mem_user/user_manager.py src/memos/api/web_auth.py tests/mem_user/test_user_schema_migration.py tests/api/test_web_auth.py。

### Task 2: 登录、登出、过期、CSRF 与限流

**Files:** src/memos/api/console_models.py、src/memos/api/routers/auth_router.py、src/memos/api/server_api.py、src/memos/api/middleware/rate_limit.py、tests/api/test_web_auth.py

- [ ] 先写失败测试：成功登录 200 并设置两个 cookie；错误、停用、未设密码均 401 且消息相同；logout 后 401；过期 session 401；写请求缺/错 CSRF 403；第 11 次失败登录 429。
- [ ] 运行 poetry run pytest tests/api/test_web_auth.py -q，预期路由不存在而 FAIL。
- [ ] 实现 router、cookie、session/CSRF dependency 和 login bucket；AUTH_ENABLED=false reload app 后 /auth/login 404。
- [ ] 运行 poetry run pytest tests/api/test_auth_router_mounts.py tests/api/test_web_auth.py -q，预期 PASS。

### Task 3: RBAC 与管理员用户/Cube CRUD

**Files:** src/memos/api/console_models.py、src/memos/api/routers/admin_router.py、src/memos/mem_user/user_manager.py、tests/api/test_admin_console.py

- [ ] 先写失败测试：未登录 /admin/users 401；USER session 403；ROOT CRUD USER；ADMIN 管 USER 但管理 ROOT/ADMIN 403；不能分配无效 Cube；最后 ROOT 不可停用或删除。
- [ ] 运行 poetry run pytest tests/api/test_admin_console.py -q，预期端点不存在而 FAIL。
- [ ] 实现 require_admin_principal、assert_can_manage、用户/Cube schemas 和端点；user_name immutable；response 无敏感字段。
- [ ] 运行 poetry run pytest tests/api/test_admin_console.py tests/api/test_cube_access_control.py tests/mem_user/test_mem_user.py -q，预期 PASS。

### Task 4: 管理员代签与用户自助 key

**Files:** src/memos/api/utils/api_keys.py、src/memos/api/routers/admin_router.py、src/memos/api/routers/me_router.py、tests/api/test_me_keys.py、tests/api/test_admin_console.py

- [ ] 先写失败测试：ROOT/ADMIN session 可代签、列出、吊销；master 仍可代签并生成 master；普通 key 无 admin scope 403；USER 只见自身 key；创建响应一次包含完整 key；列表无完整 key；吊销他人 key 固定 403。
- [ ] 运行 poetry run pytest tests/api/test_me_keys.py tests/api/test_admin_console.py -q，预期 owner-safe revoke 和新端点 FAIL。
- [ ] 实现 revoke_api_key_for_user() 单 SQL owner 条件；/me 强制 principal.user_name；现有 /admin/keys schema 不改。
- [ ] 运行上述测试及 poetry run pytest tests/api/test_auth.py tests/api/test_auth_router_mounts.py -q，预期 PASS。

### Task 5: 静态 console 与浏览器流程

**Files:** src/memos/api/static/console/index.html、app.js、styles.css、src/memos/api/server_api.py、tests/api/test_console_static.py

- [ ] 先写失败测试：AUTH_ENABLED=true 的 /console/ 返回 HTML；静态资源可加载；AUTH_ENABLED=false /console/ 404。
- [ ] 运行 poetry run pytest tests/api/test_console_static.py -q，预期目录/挂载不存在而 FAIL。
- [ ] 实现页面、角色导航、table/dialog、一次性 key、CSRF fetch client；不引入 CDN/localStorage。
- [ ] 运行 poetry run pytest tests/api/test_console_static.py tests/api/test_auth_router_mounts.py -q；再用 Playwright 或 Docker smoke 走完整浏览器流程。

### Task 6: OpenAPI、依赖和全量验证

**Files:** docs/openapi.json、pyproject.toml、poetry.lock、Docker 安装清单（需批准）

- [ ] 先增加 OpenAPI 失败断言，确认新路径未出现。
- [ ] 获批后加入 web-console extra、锁文件和 Docker 安装；运行 poetry lock --no-update。
- [ ] 运行 make openapi 并审查路径、安全 scheme、无敏感字段。
- [ ] 运行 make format、相关 pytest、make test 和 Docker smoke test；将真实输出附到 PR。

## 11. 验收清单

- [ ] AUTH_ENABLED=true 且安装 web-console extra 时，/console/、/auth/*、/admin/users*、/me/* 可用。
- [ ] ROOT 可 CRUD 用户、分配 Cube、代签/吊销任意 key；ADMIN 可管理 USER/GUEST，不能管理 ROOT/ADMIN。
- [ ] USER/GUEST 只能读自己的 profile、创建/列出/吊销自己的 key；访问他人数据固定 403。
- [ ] 未登录访问 /admin/* 和 /me/* 为 401；非管理员访问 /admin/* 为 403；不存在/他人 key 的 /me 吊销为 403。
- [ ] 旧 memos_users.db 备份后可幂等升级；旧 users/cubes/association 数据完整；PRAGMA user_version=2。
- [ ] 密码只以 Argon2id PHC 存储；session/CSRF/key 明文不落库、不进日志、不进 OpenAPI response。
- [ ] 登录限流 429；Redis 不可用时内存回退工作；Cookie 与 CSRF 属性通过测试。
- [ ] AUTH_ENABLED=false 时 Web 面和 /admin/* 404，业务 bypass、Cube ACL 兼容、限流和安全头不回归。
- [ ] make openapi、相关 pytest、make test、Docker 登录→建用户→用户自建 key→调用业务 smoke test 全绿。

## 12. 方案自审

- 已覆盖 7 个决策点、SQLite migration、指定 API、RBAC、前端、登录态、安全、现有 key 融合、OpenAPI、TDD 和验收。
- 文档无待定占位；每个代码步骤给出文件、函数、调用和测试命令。
- user_name、role、session hash、key owner 命名一致；session 不进入业务 privileged，master 仍是唯一可生成 master key 的 credential。
- 本次只新增本方案文档；实现前涉及 pyproject.toml/Docker 的依赖变更必须先获用户批准。
