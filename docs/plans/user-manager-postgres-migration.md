# MemOS UserManager SQLite -> PostgreSQL 实施方案

> 状态：待实施  
> 唯一事实来源：`docs/plans/user-manager-postgres-audit.md`  
> 范围：用户、Cube 归属关系和 Web session 的持久化后端迁移；本文只定义方案，不包含代码实施。

## 1. 目标与边界

目标是让生产路径不再创建或读写 `.memos/memos_users.db`：用户、Cube、用户与 Cube 的关联以及 Web session 全部进入现有 `memos` PostgreSQL 数据库。新后端必须保持 SQLite `UserManager` 的 26 个方法、返回值和软删除语义，并补齐 `password_hash`；现有 SQLite/MySQL 实现保留，不做公共 API 改名。

PostgreSQL 连接统一复用审计事实 8 中已经由 compose 注入的 `POSTGRES_HOST`、`POSTGRES_PORT`、`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`。仅 UserManager/WebSessionStore 通过 SQLAlchemy 建立新连接池；API key 代码现有的 psycopg2 直连不进入本次重构。

## 2. 裁决

### D1：用户和 Web session 一起迁移 PostgreSQL

**裁决：选择方案 A，`UserManager` 与 `WebSessionStore` 同批迁移，切换后生产路径不再依赖 SQLite。** 理由：审计事实 1 和事实 3 证明两者当前共同写入 `memos_users.db`；只迁用户仍会保留存储分裂和会话丢失点，而事实 9 已确认现有 PostgreSQL 有持久卷可承载两类数据。

### D2：新增独立 PostgreSQL UserManager

**裁决：选择方案 A，新增 `postgres_user_manager.py`，逐方法移植 SQLite 版完整契约，不把 PostgreSQL 分支塞入现有 SQLite 类。** 理由：审计事实 4 明确 SQLite 版有完整 26 方法，事实 6 证明 MySQL 副本缺 8 个方法、`password_hash` 和 `SQLEnum`，不能作为实现基线；独立类也能隔离事实 1 中的 SQLite URL 和事实 4 中的 PRAGMA 迁移逻辑。

### D3：通过工厂完成三处实例化切换，默认 PostgreSQL

**裁决：新增 `USER_DB_BACKEND`，默认值为 `postgres`，并让三处生产实例化统一调用工厂运行时入口。** 理由：审计事实 2 已锁定 `server_api.py:64`、`server_router.py:81`、`cube_handler.py:34` 三处直接 `UserManager()`，事实 7 又证明现有 factory/config 尚无 postgres；集中入口可以避免第四处硬编码并保留显式 `sqlite` 回退能力。

### D4：不开发 SQLite 数据搬迁脚本，按空库重建

**裁决：不迁移现有 SQLite 行数据，由 PostgreSQL 后端幂等建表并初始化 root，切换时主动使旧 Web session 失效。** 理由：审计 D4 已核实 SQLite 当前只剩 root、历史用户已丢且数据量几乎为零；为这批数据引入一次性 ETL、冲突处理和密码/session 搬迁的成本高于价值。旧 SQLite 文件只作为短期回退备份，不由应用导入。

### D5：与 `api_keys` 共库、分表，使用幂等 schema bootstrap

**裁决：在现有 `memos` 数据库默认 schema 中创建 `users`、`cubes`、`user_cube_association`、`web_sessions` 和命名原生枚举 `user_role`，只执行 `create_all(checkfirst=True)`，不触碰 `api_keys`。** 理由：审计事实 8、9 表明 `api_keys` 已在该库运行，审计 D5 已核实 `users` 当前不存在；UserManager 自有 Metadata 只管理自己的表，可以与 `api_keys` 安全共存。

## 3. 目标结构与关键契约

```text
POSTGRES_* env
    |
    +-- postgres_connection.py -- SQLAlchemy URL/Engine
    |       +-- PostgresUserManager -- users/cubes/user_cube_association/user_role
    |       +-- WebSessionStore ----- web_sessions
    |
USER_DB_BACKEND=postgres
    +-- mem_user.factory.create_runtime_user_manager()
            +-- server_api.py
            +-- server_router.py
            +-- cube_handler.py

me_router.py/admin_router.py -- 原 psycopg2 api_keys 连接（完全不改）
```

数据库对象约束：

| 对象 | 约束 |
|---|---|
| `user_role` | PostgreSQL 原生 ENUM，且仅含 `ROOT`、`ADMIN`、`USER`、`GUEST` |
| `users` | 保持事实 5 全部字段；`user_name` 唯一且非空，`role` 使用 `SQLEnum(UserRole)`，`password_hash` 可空 |
| `cubes` | 保持事实 5 字段与 `owner_id -> users.user_id` 外键 |
| `user_cube_association` | `(user_id, cube_id)` 联合主键及两个外键，保持 `created_at` |
| `web_sessions` | 字段、索引、token hash、过期和撤销语义与当前 SQLite 表完全一致，仅替换方言和连接实现 |

`role` 的模型定义固定为：

```python
role = Column(
    SQLEnum(
        UserRole,
        name="user_role",
        native_enum=True,
        validate_strings=True,
        values_callable=lambda enum_cls: [item.value for item in enum_cls],
    ),
    default=UserRole.USER,
    nullable=False,
)
```

显式 `values_callable` 保证数据库标签取枚举字符串值而非依赖 SQLAlchemy 隐式规则；按事实 5，这四个值均为大写。

## 4. 编号实施任务

### T1. 先建立 PostgreSQL 契约测试（依赖：无；改动 4 个文件）

遵循 TDD，先提交会失败的测试，再开始生产实现。测试不得连接默认 `memos` 生产库。

#### 新建 `tests/mem_user/conftest.py`

关键内容必须完整覆盖以下 fixture 行为：

```python
@pytest.fixture
def postgres_test_schema():
    """只接受 MEMOS_TEST_POSTGRES_*；创建唯一 schema，结束后 DROP SCHEMA ... CASCADE。"""
```

- 只从 `MEMOS_TEST_POSTGRES_HOST/PORT/USER/PASSWORD/DB` 取值；缺失时按仓库集成测试约定 skip，绝不回退到生产 `POSTGRES_*`。
- schema 名使用 `memos_test_<uuid hex>`，并限制为 `[a-z0-9_]+`。
- 用 psycopg2 的 `sql.Identifier` 创建/删除 schema，禁止字符串拼接标识符。
- fixture 输出测试 URL 和 schema；teardown 先 `dispose()` 所有 engine，再删除 schema。

#### 新建 `tests/mem_user/test_postgres_user_manager.py`

测试按下列矩阵逐项锁定 SQLite 契约，不能只做“方法存在”断言：

| 组 | 必测方法与行为 |
|---|---|
| 初始化 | `__init__`、`_migrate_schema`、`_get_session`、`_init_root_user`；重复初始化不重复 root，不破坏既有表 |
| 用户读取 | `create_user`、`get_user`、`get_user_by_name`、`validate_user`、`list_users`；重复用户名遵循 SQLite 版现有异常/返回语义 |
| 密码与管理 | `set_user_password`、`count_users`、`search_users`、`update_user`；覆盖 role/is_active 过滤、offset/limit 和 Argon2id hash 原样往返 |
| Cube 查询 | `get_owned_cube_ids`、`get_default_cube_id`、`list_active_cubes`、`get_cube`、`get_user_cubes`、`validate_user_cube_access` |
| Cube 写入 | `create_cube`、`add_user_to_cube`、`remove_user_from_cube`、`set_user_cubes`；`set_user_cubes` 的替换必须单事务完成 |
| 删除与关闭 | `delete_user`、`delete_cube` 只软删除；`close` 释放连接池且可重复调用 |

另用 PostgreSQL catalog 断言：`users.password_hash` 存在且可空；`users.role` 的底层类型是 `user_role`；`pg_enum` 中标签严格等于四个大写值；三张业务表位于测试 schema。

#### 新建 `tests/api/test_postgres_web_session_store.py`

- 复用现有 WebSessionStore 测试的所有公开操作和边界，不改变方法名。
- 对创建、读取、access/refresh 轮换、撤销、过期清理逐项验证现有行为。
- 直接查询测试 schema，确认只保存 access/refresh token hash，不保存明文 token。
- 两个 Store 实例连接同一 schema，验证跨实例可见，证明状态不再依赖本地文件。
- `close()` 后连接池释放；重复 `_ensure_schema()` 幂等。

#### 新建 `tests/api/test_postgres_user_manager_wiring.py`

- 在导入三个目标模块前设置 `USER_DB_BACKEND=postgres` 并 monkeypatch `create_runtime_user_manager`。
- 分别验证 `server_api.py` lifespan、`server_router.py` 模块级单例、`cube_handler.py` 都从工厂获得 manager，而不是直接构造 SQLite `UserManager`。
- 验证 `WebSessionStore()` 默认获得 PostgreSQL engine，临时目录内未生成 `memos_users.db`。

**完成标准：** 四个测试文件已提交；首次运行因 PostgreSQL 类/注册/实现尚不存在而产生预期失败，失败点与 T2-T5 一一对应，且测试 fixture 不可能误连生产库。

### T2. 实现 PostgreSQL 连接层和完整 UserManager（依赖：T1；改动 2 个文件）

#### 新建 `src/memos/mem_user/postgres_connection.py`

关键代码结构如下；实现时应保持该文件无业务查询：

```python
import os
import re

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL

_SCHEMA_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def build_postgres_url() -> URL:
    return URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER", "memos"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "memos"),
    )


def create_postgres_engine(
    database_url: str | URL | None = None,
    schema: str | None = None,
) -> Engine:
    connect_args: dict[str, str] = {}
    if schema is not None:
        if _SCHEMA_PATTERN.fullmatch(schema) is None:
            raise ValueError("Invalid PostgreSQL schema name")
        connect_args["options"] = f"-csearch_path={schema}"
    return create_engine(
        database_url or build_postgres_url(),
        echo=False,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
```

逐行要求：

- 必须使用 `URL.create`，避免密码中的 `@`、`:` 等字符被错误拼接或写入日志。
- 不新增连接环境变量，不记录 URL/密码；默认值与事实 8 保持一致。
- `schema` 仅服务测试隔离，生产默认使用连接的默认 schema。
- 若仓库锁定的 SQLAlchemy 版本不接受上述类型导入或 union 对象，按现有版本调整类型写法，不改变函数契约；不得为此修改依赖版本。

#### 新建 `src/memos/mem_user/postgres_user_manager.py`

文件结构必须是独立 `Base`、`User`、`Cube`、`user_cube_association` 和 `PostgresUserManager`。模型字段逐项复制事实 5 的 SQLite 权威模型；唯一允许的方言化变化是上述命名原生 ENUM。不得从 MySQL 模型复制 `String(20)` role，也不得遗漏 `password_hash`。

构造与 schema 关键代码：

```python
class PostgresUserManager:
    def __init__(
        self,
        database_url: str | URL | None = None,
        user_id: str = "root",
        schema: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.engine = create_postgres_engine(database_url, schema)
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )
        self._migrate_schema()
        self._init_root_user()

    def _migrate_schema(self) -> None:
        Base.metadata.create_all(self.engine, checkfirst=True)

    def _get_session(self) -> Session:
        return self._session_factory()

    def close(self) -> None:
        self.engine.dispose()
```

必须实现且仅以 SQLite 同名方法为语义基线的完整方法清单：

```text
__init__, _migrate_schema, _get_session, _init_root_user,
create_user, get_user, get_user_by_name, set_user_password,
validate_user, list_users, count_users, search_users, update_user,
get_owned_cube_ids, get_default_cube_id, list_active_cubes,
create_cube, get_cube, validate_user_cube_access, get_user_cubes,
add_user_to_cube, remove_user_from_cube, set_user_cubes,
delete_user, delete_cube, close
```

逐方法移植规则：

1. 方法签名、默认参数、返回类型、查询过滤、排序、offset/limit、提交时机以及“不存在时”的返回值全部与 SQLite 版一致；不得以 MySQL 版缺失方法作为理由改变契约。
2. 每个写方法使用 `with session.begin()` 或等价的单事务结构；异常路径回滚后按 SQLite 版现有语义返回或抛出项目语义异常，禁止裸 `Exception`/`RuntimeError`。
3. `_init_root_user` 依赖 `user_id`/`user_name` 唯一约束做到幂等；并发首次启动发生唯一键竞争时，回滚、重新读取已存在 root，而不是让第二个进程启动失败。
4. `set_user_password` 只保存调用方提供的 hash，不在存储层重复哈希，也不记录 hash。
5. `delete_user`、`delete_cube` 继续设置 `is_active=False`，不发出物理 DELETE；关联增删及 `set_user_cubes` 保持 SQLite 版权限与原子性。
6. 所有日志使用 `memos.log.get_logger(__name__)` 和参数化格式，不输出连接串、用户原始数据或 token/hash。
7. `_migrate_schema` 在 PostgreSQL 中表示“幂等创建本版本对象”，禁止 PRAGMA，也禁止 DROP/ALTER 已有 `api_keys` 或其他 metadata 中的表。

**完成标准：** `test_postgres_user_manager.py` 全部通过；26 个方法逐项存在并通过行为断言；真实 PG catalog 中枚举为四个大写标签，`password_hash` 存在；重复/并发初始化不产生第二个 root。

### T3. 注册 postgres 配置并增加运行时工厂入口（依赖：T2；改动 2 个文件）

#### 修改 `src/memos/configs/mem_user.py`

在 SQLite/MySQL 配置同级增加 `PostgresUserManagerConfig`：继承与现有数据库 manager 配置相同的基类、保持相同 Pydantic v2 约束，只将 backend 固定为字面量 `postgres`。连接秘密不复制进配置对象，仍由 `POSTGRES_*` 在连接层读取。

注册表精确变化：

```diff
 backend_to_class = {
     "sqlite": SQLiteUserManagerConfig,
     "mysql": MySQLUserManagerConfig,
+    "postgres": PostgresUserManagerConfig,
     "redis": RedisUserManagerConfig,
 }
```

#### 修改 `src/memos/mem_user/factory.py`

注册和运行时入口精确变化：

```diff
+import os
+from memos.mem_user.postgres_user_manager import PostgresUserManager

-backend_to_class = {"sqlite": UserManager, "mysql": MySQLUserManager}
+backend_to_class = {
+    "sqlite": UserManager,
+    "mysql": MySQLUserManager,
+    "postgres": PostgresUserManager,
+}
+
+def create_runtime_user_manager():
+    backend = os.getenv("USER_DB_BACKEND", "postgres").strip().lower()
+    if backend not in {"postgres", "sqlite"}:
+        # 复用本 factory 现有的 unsupported-backend 语义异常。
+        raise <existing unsupported-backend exception>(backend)
+    return backend_to_class[backend]()
```

约束：

- `create_runtime_user_manager` 是三处生产 wiring 的唯一入口，默认 postgres。
- 运行时入口只承诺零配置的 `postgres` 和现有 `sqlite` 回退；MySQL 继续走原有显式配置 factory，避免臆造 MySQL 环境参数。
- `<existing unsupported-backend exception>` 实施时替换为 factory 已使用的项目语义异常，绝不能原样留在代码，也不能新增裸 `RuntimeError`。
- 不修改现有 backend 名称或已有配置序列化格式。

**完成标准：** config factory 能解析 `backend=postgres`；implementation factory 能创建 `PostgresUserManager`；未设置环境变量时选择 postgres，显式 `sqlite` 时仍创建原 SQLite manager，无效值走现有语义错误路径。

### T4. 将 WebSessionStore 的持久化实现迁到 PostgreSQL（依赖：T1、T2；改动 1 个文件）

#### 修改 `src/memos/api/web_auth.py`

保留 `WebSessionStore` 类名和全部公开方法签名，只替换存储内部：

1. 删除 `sqlite3` 连接依赖以及 `settings.MEMOS_DIR / "memos_users.db"` 默认路径；构造函数改为可注入 `database_url`、`schema`，默认调用 `create_postgres_engine()`。
2. `_connect()` 改为返回 SQLAlchemy `Connection`/事务上下文，或删除该 helper 并在各方法中统一使用 `with self.engine.begin() as connection:`；不得向调用者暴露 psycopg2 connection。
3. `_ensure_schema()` 用独立 Metadata/Table 表达当前 `web_sessions` 的全部原有列、非空约束、唯一约束和索引，并执行 `metadata.create_all(engine, checkfirst=True)`。字段含义和 hash/expiry/revocation 语义不变，不能借迁移改表契约。
4. 把 SQLite `?` 占位符全部替换为 SQLAlchemy 绑定参数；把现有 insert/update/delete/select 逐条改为 SQLAlchemy Core。若当前语义包含 upsert，使用 `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update/do_nothing`，禁止“先查后写”的竞争窗口。
5. SQLite 返回行访问改为 `.mappings()`；datetime 的写入、比较和过期清理统一由 SQLAlchemy 参数传递，不在 SQL 字符串中拼接。
6. 每个 session 创建、轮换、撤销操作在一个 `engine.begin()` 事务完成；任何异常自动回滚。token 仍只以 hash 形式持久化，日志不包含 token 或 hash。
7. `close()` 调用 `engine.dispose()` 并保持幂等。生产默认不再创建 SQLite 文件。

该文件不得顺带修改 password hash 算法、cookie、JWT/token 格式、有效期、认证响应模型或路由行为。本任务只是存储适配。

**完成标准：** `test_postgres_web_session_store.py` 全部通过；两个应用实例可共享 session；创建、轮换、撤销和清理具备事务性；仓库运行测试时不生成 `memos_users.db`；`web_auth.py` 不再导入/调用 sqlite3。

### T5. 切换三处生产实例化并验证 API 内部 wiring（依赖：T3、T4；改动 3 个文件）

三处只做同一种机械替换，不修改路由、请求/响应模型或依赖注入公开契约。

#### 修改 `src/memos/api/server_api.py:64`

```diff
-from memos.mem_user.user_manager import UserManager
+from memos.mem_user.factory import create_runtime_user_manager
 ...
-user_manager = UserManager()
+user_manager = create_runtime_user_manager()
 store = WebSessionStore()
```

`WebSessionStore()` 调用形式可保持不变，因为 T4 已把其默认后端改为 PostgreSQL；lifespan 关闭阶段必须继续分别调用 manager/store 的 `close()`。

#### 修改 `src/memos/api/routers/server_router.py:81`

```diff
-from memos.mem_user.user_manager import UserManager
+from memos.mem_user.factory import create_runtime_user_manager
 ...
-<module_user_manager> = UserManager()
+<module_user_manager> = create_runtime_user_manager()
```

只替换事实 2 指出的模块级单例表达式；变量名及下游引用保持原样，环境变量在模块导入时读取。

#### 修改 `src/memos/api/handlers/cube_handler.py:34`

```diff
-from memos.mem_user.user_manager import UserManager
+from memos.mem_user.factory import create_runtime_user_manager
 ...
-<handler_user_manager> = UserManager()
+<handler_user_manager> = create_runtime_user_manager()
```

只替换事实 2 指出的实例化表达式；不改变 cube handler 的调用顺序、返回值和错误映射。

`<module_user_manager>`/`<handler_user_manager>` 表示文件中现有变量名，实施时原名保留，不应把尖括号文本写进代码。

**完成标准：** `test_postgres_user_manager_wiring.py` 全部通过；全仓生产代码搜索只剩 SQLite 后端实现/测试中的 `UserManager()`，三处指定位置均使用工厂；默认启动连接 PG；`USER_DB_BACKEND=sqlite` 可作为代码级回退；OpenAPI 生成结果无差异。

### T6. 集成验收、空库切换与回退演练（依赖：T1-T5；改动 0 个文件）

上线顺序固定为：

1. 保留现有 `.memos/memos_users.db` 的只读备份，仅用于回滚旧应用版本；不执行数据导入。
2. 在测试 PostgreSQL 实例运行 T1 全套测试，确认 schema 创建权限、ENUM 和事务行为。
3. 部署新应用；首次启动由 `PostgresUserManager._migrate_schema()` 和 `WebSessionStore._ensure_schema()` 幂等创建对象，并由 `_init_root_user()` 创建 root。
4. 确认 root 可通过现有流程设置/更新密码；旧 SQLite Web session 按 D4 全部失效，用户重新登录。
5. 执行用户 CRUD、Cube 分配、登录、refresh、logout 冒烟测试，再检查 `api_keys` 的创建/查询仍正常。
6. 回退演练使用旧应用版本和保留的 SQLite 文件；不得通过删除 PG 表实现回退。确认回退后再恢复新版本。

建议实施阶段实际执行并保存原始输出：

```powershell
poetry run pytest tests/mem_user/test_postgres_user_manager.py -q
poetry run pytest tests/api/test_postgres_web_session_store.py tests/api/test_postgres_user_manager_wiring.py -q
poetry run pytest tests/mem_user/ tests/api/ -q
make openapi
git diff --exit-code -- docs/openapi.json
```

禁止运行 `ruff --fix`、自动格式化或任何可能批量改写无关文件的命令。若 `make openapi` 产生差异，必须停止上线并审查；本方案没有授权修改 OpenAPI 契约。

**完成标准：** 上述 pytest 均有真实通过输出；OpenAPI 无差异；PG 中四张新表和一个枚举存在，`api_keys` 数据与连接路径未改变；应用目录不产生新的 `memos_users.db`；新旧版本回退演练均完成。

## 5. 不做清单

- 不修改 `src/memos/api/routers/me_router.py`、`admin_router.py` 中 API keys 的 `psycopg2.connect(...)`、环境变量、SQL 或连接生命周期。
- 不迁移、不改表、不重命名、不清理现有 PostgreSQL `api_keys` 表及其 `docker/postgres/init/001_api_keys.sql`。
- 不修改 Neo4j、Qdrant 或任何 textual/tree/preference/skill/KV cache/LoRA 记忆存储和调度逻辑。
- 不修改前端页面、筛选值、登录 UI 或管理控制台；后端继续提供大写 role 值。
- 不修改公开 API 路由、请求/响应模型、cookie/token 格式、密码哈希算法或授权策略。
- 不补齐 MySQL 版缺少的 8 个方法，不改 MySQL role 字段，也不合并三套 ORM 模型。
- 不删除 SQLite `UserManager`、SQLite 表或旧数据库文件；它们保留作显式回退，但不再是默认生产路径。
- 不新增 SQLite -> PostgreSQL 数据迁移脚本，不导入旧用户、密码 hash 或旧 Web session。
- 不修改 `pyproject.toml` 或升级 SQLAlchemy/psycopg2；审计事实 8 已证明 psycopg2 是现有运行依赖。
- 不删除 `docker/docker-compose.yml` 的 `memos_data` 卷。本轮先消除运行时 SQLite 读写；`.memos` 中日志/其他文件及卷清理由独立运维变更处理。
- 不运行自动格式化、自动修复，不借机整理无关 imports 或重构 API 模块。

## 6. 风险表

| 风险 | 触发条件/影响 | 预防与处置 | 核验 |
|---|---|---|---|
| PG `SQLEnum` 原生类型生命周期 | 未命名枚举、大小写漂移或残留同名异构类型会导致建表/写入失败 | 固定类型名 `user_role`，显式 `native_enum=True` 和 `values_callable`；只允许四个大写值；`create_all(checkfirst=True)`，不自动 DROP/ALTER | 查询 `pg_type`/`pg_enum`，精确比较四个标签 |
| ENUM 与测试 schema 的可见性 | 并行测试共享 `public.user_role` 会互相污染或 teardown 误删 | 每次测试使用唯一 `search_path` schema；engine dispose 后只 DROP 本次 schema；禁止使用生产 env | 并行运行两组 PG 测试，schema/枚举互不干扰 |
| SQLAlchemy 版本兼容 | 当前锁定版本对 `URL` 导入、类型注解、Enum 参数或 Result API 支持不同 | 不升级依赖；按已锁版本调整 import/typing，保持 `URL.create`、`.mappings()` 与事务契约；先用最小连接测试验证 | 在项目锁文件环境运行指定 pytest，不以本机全局包代替 |
| 测试隔离不足 | 测试误连 `memos`、并行删表或残留 root，可能污染开发/生产数据 | 仅接受 `MEMOS_TEST_POSTGRES_*`；唯一 schema；安全 Identifier；teardown 精确删除；fixture 禁止回退生产变量 | 缺测试变量时 skip/明确失败；测试前后列举目标 schema |
| 并发首次启动 | 多 worker 同时建表/创建 root，唯一键冲突使一个 worker 退出 | schema bootstrap 幂等；root insert 捕获唯一冲突后回滚并重新读取 | 并发构造两个 manager，最终恰好一个 root |
| 自动建表权限不足 | 生产数据库账号只有 DML 权限，首次启动无法创建表/枚举 | 发布前在同权限测试库演练；确认账号具备目标 schema CREATE/USAGE；失败则在上线窗口由 DBA 执行从 Metadata 生成并评审的等价 DDL | 使用生产同权限账号执行 bootstrap 冒烟 |
| Web session SQL 方言差异 | SQLite upsert、占位符、row 访问或 datetime 比较直接照搬会在 PG 失败 | 全部使用 SQLAlchemy Core 绑定参数；PG upsert 原语；统一事务和 `.mappings()` | 覆盖轮换、撤销、过期清理、并发冲突测试 |
| 切换导致全员登出 | D4 不迁旧 `web_sessions`，现有 access/refresh token 无法继续使用 | 把全员重新登录作为计划内切换效果；切换窗口验证 root 密码设置路径 | 旧 token 失败，新登录/refresh/logout 成功 |
| 三处 manager 产生独立连接池 | 模块级单例加 lifespan/handler 共三个 pool，连接数上升 | `pool_pre_ping=True`；每个生命周期调用 `close()`；部署前核对 pool 默认值与 PG 连接上限，不擅自扩大 pool | 启停后检查 `pg_stat_activity` 无泄漏 |
| ORM 类型身份差异 | 下游若对 SQLite `User`/`Cube` 做 `isinstance`，独立 PG 模型可能暴露兼容问题 | 契约测试比较字段、返回值和 router 行为；禁止依赖具体 ORM 类身份，若确有依赖则在实施前作为阻断项处理，不扩大本次公共 API | 跑完整 `tests/mem_user/` 与 `tests/api/` |
| API keys 被误改 | 共库容易诱发“统一连接层”重构，破坏已运行 psycopg2 路径 | 文件和 metadata 隔离；me/admin API key 代码列入不做清单；上线前做 api_keys 冒烟 | `git diff` 确认两处直连代码零改动，API key CRUD 通过 |

## 7. 总体验收清单

- [ ] T1：四个 PostgreSQL 契约/隔离测试文件先于生产实现落地，并记录预期失败。
- [ ] T2：`PostgresUserManager` 实现全部 26 个方法，字段和行为对齐 SQLite；`password_hash` 可空且可读写。
- [ ] T2：`users.role` 是命名 PG 原生 ENUM，值严格为 `ROOT/ADMIN/USER/GUEST`。
- [ ] T2：用户、Cube、关联的写操作均有事务；删除保持 soft delete；root 初始化幂等且可并发。
- [ ] T3：config/factory 都注册 `postgres`；`USER_DB_BACKEND` 默认 postgres，显式 sqlite 可回退。
- [ ] T4：WebSessionStore 全部操作落 PG，不保存明文 token，不再导入 sqlite3 或创建 `memos_users.db`。
- [ ] T5：`server_api.py:64`、`server_router.py:81`、`cube_handler.py:34` 三处全部通过同一工厂入口实例化。
- [ ] T5：API 路由、请求/响应模型与生成的 `docs/openapi.json` 无变化。
- [ ] T6：相关 `tests/mem_user/`、`tests/api/` 在真实隔离 PostgreSQL 上有通过输出。
- [ ] T6：现有 `api_keys` 表和 me/admin psycopg2 连接未修改，API key 冒烟通过。
- [ ] T6：Neo4j/Qdrant 记忆写入与查询回归通过，确认本次共库变更没有跨存储影响。
- [ ] T6：旧 session 失效、新登录/refresh/logout 正常，root 密码可通过现有流程设置。
- [ ] T6：回退演练不删除 PG 数据；自动格式化/自动修复命令未运行。

## 8. 任务依赖与文件计数

| 任务 | 依赖 | 改动文件数 |
|---|---|---:|
| T1 PostgreSQL 契约测试 | 无 | 4 |
| T2 连接层与 PostgreSQL UserManager | T1 | 2 |
| T3 config/factory 注册 | T2 | 2 |
| T4 WebSessionStore 迁移 | T1、T2 | 1 |
| T5 三处生产 wiring | T3、T4 | 3 |
| T6 集成验收与切换 | T1-T5 | 0 |

合计 6 个任务，涉及 12 个未来实施文件（按唯一文件计数也是 12 个；T6 仅执行验证与发布步骤）。
