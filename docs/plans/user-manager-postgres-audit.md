# MemOS 用户存储后端：SQLite → PostgreSQL 迁移审计

> 诊断日期：2026-08-13 · Hermes 实测核实 · 本文档是 Codex 规划的唯一事实来源
> 仓库：`D:\code\invest\MemOS`（分支 `feat/api-auth-hardening`）
> 目标：新写 PostgreSQL 用户管理后端，替代硬编码 SQLite

---

## 一、问题陈述

MemOS 的存储后端是**分裂的**：

| 数据 | 当前存储 | 问题 |
|---|---|---|
| 用户账号 | SQLite（`MEMOS_DIR/memos_users.db`） | 容器重建即丢（无卷挂载） |
| Web 会话 token | SQLite（同一个 `memos_users.db`） | 同上 |
| API keys | PostgreSQL（`api_keys` 表） | 正常，有命名卷 `postgres_data` |
| 记忆数据 | Neo4j + Qdrant | 正常，有命名卷 |

用户诉求：**用户存储也应该用 PostgreSQL**，统一后端，消除 SQLite 分裂。

---

## 二、已核实事实（文件级证据）

### 事实 1：UserManager 硬编码 SQLite

`src/memos/mem_user/user_manager.py:125`：
```python
self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
```
- `db_path` 默认 `settings.MEMOS_DIR / "memos_users.db"`
- `MEMOS_DIR = Path(os.getenv("MEMOS_BASE_PATH", Path.cwd())) / ".memos"`（`src/memos/settings.py:6`）

### 事实 2：实例化点（3 处，都是直接 `UserManager()`）

| 文件:行 | 用途 |
|---|---|
| `src/memos/api/server_api.py:64` | Web console lifespan 启动（auth services） |
| `src/memos/api/routers/server_router.py:81` | product API 模块级单例 |
| `src/memos/api/handlers/cube_handler.py:34` | cube handler |

### 事实 3：WebSessionStore 也是 SQLite

`src/memos/api/server_api.py:65`：`store = WebSessionStore()`
`src/memos/api/web_auth.py:383`：`db_path = str(settings.MEMOS_DIR / "memos_users.db")`
- WebSessionStore 存 `web_sessions` 表（access/refresh token hash）

### 事实 4：SQLite 版 UserManager 完整接口（726 行，26 个方法）

`src/memos/mem_user/user_manager.py` 的 public 方法：
```
__init__(db_path=None, user_id="root")
_migrate_schema()          # SQLite 特有：PRAGMA table_info/user_version 加 password_hash 列
_get_session()
_init_root_user()
create_user(user_name, role=USER, user_id=None) -> str
get_user(user_id) -> User|None
get_user_by_name(user_name) -> User|None
set_user_password(user_id, password_hash) -> bool   # ← MySQL 版没有
validate_user(user_id) -> bool
list_users() -> list[User]
count_users(role=None, is_active=None) -> int        # ← MySQL 版没有
search_users(role=None, is_active=None, offset=0, limit=50) -> (total, rows)  # ← MySQL 版没有
update_user(user_id, role=None, is_active=None) -> bool  # ← MySQL 版没有
get_owned_cube_ids(user_id) -> list[str]             # ← MySQL 版没有
get_default_cube_id(user_id) -> str|None             # ← MySQL 版没有
list_active_cubes() -> list[dict]                    # ← MySQL 版没有
create_cube(cube_name, owner_id, cube_path=None, cube_id=None) -> str
get_cube(cube_id) -> Cube|None
validate_user_cube_access(user_id, cube_id) -> bool
get_user_cubes(user_id) -> list[Cube]
add_user_to_cube(user_id, cube_id) -> bool
remove_user_from_cube(user_id, cube_id) -> bool
set_user_cubes(user_id, cube_ids) -> bool            # ← MySQL 版没有
delete_user(user_id) -> bool  (soft delete)
delete_cube(cube_id) -> bool  (soft delete)
close()
```

### 事实 5：SQLite 版数据模型（schema）

`src/memos/mem_user/user_manager.py:62-104`：
```python
class User(Base):
    __tablename__ = "users"
    user_id = Column(String, primary_key=True, default=uuid4)
    user_name = Column(String, unique=True, nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.USER, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    password_hash = Column(String, nullable=True)  # ← Web console Argon2id，MySQL 版没有

class Cube(Base):
    __tablename__ = "cubes"
    cube_id = Column(String, primary_key=True, default=uuid4)
    cube_name = Column(String, nullable=False)
    cube_path = Column(String, nullable=True)
    owner_id = Column(String, ForeignKey("users.user_id"), nullable=False)
    created_at / updated_at / is_active

user_cube_association = Table(
    "user_cube_association",
    Column("user_id", FK users.user_id, primary_key=True),
    Column("cube_id", FK cubes.cube_id, primary_key=True),
    Column("created_at", DateTime, default=now),
)
```

`UserRole` 枚举（`user_manager.py:43-49`）：`ROOT / ADMIN / USER / GUEST`（字符串值大写）

### 事实 6：MySQL 版参考实现（502 行，缺 8 个方法）

`src/memos/mem_user/mysql_user_manager.py`：
- 连接：`mysql+pymysql://user:pass@host:port/db?charset=utf8mb4`（`pool_pre_ping=True`）
- 模型定义**复制了一份**（`Base`/`User`/`Cube`/`user_cube_association` 各自独立定义）
- 差异：
  - role 用 `String(20)` 而非 `SQLEnum(UserRole)`
  - **没有 `password_hash` 字段**
  - **缺 8 个方法**（见事实 4 标注）
  - 无 schema 迁移机制

### 事实 7：factory 支持的后端（上游只支持 sqlite/mysql）

`src/memos/mem_user/factory.py:12-13`：
```python
backend_to_class = {"sqlite": UserManager, "mysql": MySQLUserManager}
```
`src/memos/configs/mem_user.py:54-56`：
```python
backend_to_class = {"sqlite": SQLiteUserManagerConfig, "mysql": MySQLUserManagerConfig, "redis": RedisUserManagerConfig}
```
**没有 postgres**。但注意：生产代码（server_api/server_router/cube_handler）**根本没用 factory**，是直接 `UserManager()` 硬编码 SQLite。

### 事实 8：API keys 的 PG 连接方式（可复用）

`src/memos/api/routers/me_router.py:31-41` 和 `admin_router.py:142`：
```python
import psycopg2
psycopg2.connect(
    host=os.getenv("POSTGRES_HOST", "postgres"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    user=os.getenv("POSTGRES_USER", "memos"),
    password=os.getenv("POSTGRES_PASSWORD", ""),
    dbname=os.getenv("POSTGRES_DB", "memos"),
)
```
- env 变量：`POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB`
- compose 已在 `memos` 服务里注入这些 env（`docker/docker-compose.yml:32-36`）

### 事实 9：compose 现状

- 已有 `memos-postgres`（`postgres:16-alpine`）服务，env `POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD`
- `memos` 服务 env 注入 `POSTGRES_HOST=postgres` 等（compose:32-36）
- `api_keys` 表由 `docker/postgres/init/001_api_keys.sql` 初始化
- 新增的 `memos_data` 卷（本轮刚加）挂载 `/app/.memos` 持久化 SQLite——如果切 PG，这个卷对用户数据就多余了（但 web session 若也迁 PG，则整个 .memos 都不需要）

---

## 三、迁移方案（需 Codex 裁决的决策点）

### 决策点 D1：迁移范围——只迁用户数据，还是用户 + Web session 一起迁？

- **方案 A（推荐）**：用户（UserManager）+ Web session（WebSessionStore）**都迁 PG**
  - 彻底消除 SQLite，`.memos` 目录只剩 logs（或也迁走）
  - WebSessionStore 的 `_connect()`/`_ensure_schema()` 全是 sqlite3 原生调用（`web_auth.py:372-400`），要改成 SQLAlchemy 或 psycopg2
  - 改动量大（涉及 web_auth.py 大量重写）
- **方案 B**：只迁用户数据（UserManager），Web session 继续 SQLite
  - 改动小，但存储仍分裂（用户/PG + session/SQLite）
  - session 数据仍可能丢（除非 memos_data 卷保留）

### 决策点 D2：新 PG UserManager 的实现方式

- **方案 A**：新写 `postgres_user_manager.py`，完整实现 SQLite 版 26 个方法 + `password_hash` + `SQLEnum(UserRole)`
  - 参考 SQLite 版逻辑（逐方法移植）+ MySQL 版的 `pool_pre_ping` + PG 连接串
  - 用 SQLAlchemy（`postgresql+psycopg2://`）
- **方案 B**：改造现有 SQLite 版 UserManager，加 backend 参数支持 sqlite/postgres
  - 复用同一份模型定义，减少重复
  - 但 SQLite 特有的 `_migrate_schema`（PRAGMA）要隔离

### 决策点 D3：实例化切换

- 3 处 `UserManager()` 改为从 factory/配置读 backend，默认 postgres
- 需要环境变量（如 `USER_DB_BACKEND=postgres` 或复用 POSTGRES_* env）
- server_router.py:81 是模块级单例，启动时读 env

### 决策点 D4：数据迁移（现有 SQLite 用户数据 → PG）

- 当前 SQLite 里只剩 root（之前事故已丢 jxpro），数据量几乎为零
- 是否需要一个一次性迁移脚本？还是直接重置（反正数据几乎空了）？

### 决策点 D5：schema 冲突

- PG 里已有 `api_keys` 表（在 `memos` 数据库里）
- 新 PG UserManager 会创建 `users`/`cubes`/`user_cube_association` 表
- 表名是否与现有冲突？（`users` 表 PG 里目前不存在，已核实）

---

## 四、硬约束

1. **接口必须完整**：新 PG 版必须实现 SQLite 版全部 26 个方法 + `password_hash` 字段，因为 Web console 的 auth/admin 代码依赖它们（`auth_router.py` 调 `set_user_password`/`get_user_by_name`，`admin_router.py` 调 `search_users`/`update_user`，`me_router.py` 调 `get_user_cubes`/`create_user`）
2. **role 用 SQLEnum(UserRole)**，值大写 `ROOT/ADMIN/USER/GUEST`（前端筛选已对齐大写）
3. **不要破坏现有 API keys 的 PG 连接**（`me_router`/`admin_router` 的 psycopg2 直接连接保持不变）
4. **测试**：MemOS 有大量 pytest，涉及 UserManager 的测试要能跑通（或用 dependency_overrides 注入 mock）
5. **不运行 ruff --fix / 自动格式化**（污染无关文件）

---

## 五、建议核读的源码路径（本地，web 工具可能被拦）

- `src/memos/mem_user/user_manager.py`（SQLite 版，726 行，接口 + schema 权威）
- `src/memos/mem_user/mysql_user_manager.py`（MySQL 版参考，502 行）
- `src/memos/mem_user/factory.py` + `configs/mem_user.py`（factory 模式）
- `src/memos/api/server_api.py`（lifespan，UserManager + WebSessionStore 实例化）
- `src/memos/api/routers/auth_router.py`（get_services，依赖 user_manager）
- `src/memos/api/routers/me_router.py` + `admin_router.py`（API keys 的 psycopg2 PG 连接参考 + user_manager 方法调用）
- `src/memos/api/web_auth.py`（WebSessionStore，若迁 session 则要改这里）

---

## 六、交付格式要求

Codex 输出方案文档到 `docs/plans/user-manager-postgres-migration.md`（MemOS 仓库），必须包含：

1. **裁决**（D1~D5 逐条：一句话 + 理由，引用本文档事实）
2. **编号任务**（按依赖排序），每个任务给**文件级精确改动**：
   - 新文件给完整内容/关键代码
   - 改文件给精确 diff 或逐行改动说明
3. **不做清单**（明确不改什么：如 API keys 连接、memory 存储、Neo4j/Qdrant）
4. **风险表**（含：SQLEnum 在 PG 的枚举类型处理、SQLAlchemy 版本兼容、测试隔离）
5. **验收清单**（每任务可核验完成标准）

**硬规则**：只出方案，绝对不要修改任何源码文件；绝对禁止运行任何格式化/自动修复命令。
