# Auth 接入执行计划（整理版）

> 本文档为用户需求原文的标准化整理稿，按阶段描述目标与实施项。

## Phase 1: 接入鉴权（挂载 admin_router + product 路由鉴权）

### 目标
将已有的 auth 模块真正接入 API 执行链路。

### 任务
1. 修改 `src/memos/api/server_api.py`
- 导入 `admin_router`：`from memos.api.routers.admin_router import router as admin_router`
- 在 `app.include_router(server_router)` 后新增：`app.include_router(admin_router)`
- 不修改其他内容

2. 修改 `src/memos/api/routers/server_router.py`
- 导入：
  - `from fastapi import Depends`
  - `from memos.api.middleware.auth import verify_api_key, require_scope`
- 为端点添加 `dependencies`

READ scope（`dependencies=[Depends(require_scope("read"))]`）：
- `POST /product/search`
- `GET /product/scheduler/allstatus`
- `GET /product/scheduler/status`
- `GET /product/scheduler/task_queue_status`
- `POST /product/scheduler/wait`
- `GET /product/scheduler/wait/stream`
- `POST /product/get_all`
- `POST /product/get_memory`
- `GET /product/get_memory/{memory_id}`
- `POST /product/get_memory_by_ids`
- `POST /product/get_memory_dashboard`
- `POST /product/suggestions`
- `POST /product/chat/complete`
- `POST /product/chat/stream`
- `POST /product/chat/stream/playground`

WRITE scope（`dependencies=[Depends(require_scope("write"))]`）：
- `POST /product/add`
- `POST /product/feedback`
- `POST /product/delete_memory`
- `POST /product/delete_memory_by_record_id`
- `POST /product/recover_memory_by_record_id`

内部接口（`dependencies=[Depends(verify_api_key)]`）：
- `POST /product/get_user_names_by_memory_ids`
- `POST /product/exist_mem_cube_id`
- `POST /product/chat/stream/business_user`

### 注意
- 保持原装饰器其他参数不变（如 `summary`、`response_model`）
- 不改函数签名与函数体

## Phase 2: 修复数据类型（psycopg2 JSON 适配 + 时间戳对齐）

### 目标
修复 scopes 的 JSON 序列化与 `expires_at` 时间比对问题。

### 任务
1. 修改 `src/memos/api/utils/api_keys.py`
- 导入 `from psycopg2.extras import Json`
- `create_api_key_in_db` 中将 `scopes or ["read"]` 改为 `Json(scopes or ["read"])`
- `expires_at` 使用时区感知时间：
  - `from datetime import datetime, timedelta, timezone`
  - `expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)`

2. 修改 `src/memos/api/middleware/auth.py`
- `lookup_api_key` 中 `expires_at` 比对：
  - 若为 `datetime`，与 `datetime.now(timezone.utc)` 比较
  - 若为数字（epoch），与 `time.time()` 比较
- 确保导入 `datetime` 与 `timezone`
- scopes 从 JSONB 读取时加防御性处理：若为字符串则尝试 `json.loads(scopes)`

### 约束
- 不修改其他文件
- 保证向后兼容

## Phase 3: Docs 公开策略 + 健康检查端点

### 目标
OpenAPI/Docs 可配置公开，并添加无鉴权健康检查。

### 任务
1. 修改 `src/memos/api/server_api.py`
- 添加环境变量：
  - `DOCS_PUBLIC = os.getenv("DOCS_PUBLIC", "true").lower() == "true"`
- 根据 `DOCS_PUBLIC` 控制：
  - `docs_url`、`redoc_url`
  - `openapi_url`（确保 `/openapi.json` 同策略）
- 添加路由：
  - `@app.get("/health", tags=["Health"])`
  - 返回 `{"status": "ok", "version": "1.0.1", "auth_enabled": AUTH_ENABLED}`

2. 更新 `docker/.env.example`
- 追加 `DOCS_PUBLIC=true`

### 约束
- 不修改路由文件和鉴权文件

## Phase 4: 初始化脚本（Master Key 生成工具）

### 目标
提供命令行工具用于首次部署生成 master key。

### 任务
1. 新建 `src/memos/api/utils/generate_master_key.py`
- 可通过 `python -m` 执行
- 调用 `api_keys.generate_master_key()` 生成 key/hash
- 打印明文 master key（提示仅展示一次）
- 打印 `MASTER_KEY_HASH=<hash>` 供写入 `.env`
- 支持可选 `--output-env` 直接追加到指定 `.env`
- 使用 `argparse` + `if __name__ == "__main__"`

2. 新建 `scripts/init_auth.sh`
- 检查 `POSTGRES_PASSWORD`，未设置则随机生成
- 调用 `python -m memos.api.utils.generate_master_key`
- 输出需添加到 `.env` 的配置行
- 交互确认后写入

### 约束
- 不修改已有文件

## Phase 5: 单元测试 + 集成测试

### 目标
覆盖鉴权核心路径的自动化测试。

### 任务
1. 新建 `tests/test_auth_middleware.py`
- 使用 `pytest + unittest.mock`，不依赖真实数据库
- 用例：
  - `test_auth_disabled_bypasses_check`
  - `test_missing_api_key_returns_401`
  - `test_master_key_authentication`
  - `test_invalid_key_format_returns_401`
  - `test_valid_key_lookup_success`
  - `test_expired_key_returns_none`
  - `test_require_scope_read_allows_read`
  - `test_require_scope_write_denies_read_only`
  - `test_scope_all_grants_everything`
  - `test_internal_request_bypass`

2. 新建 `tests/test_admin_router.py`
- 使用 `pytest + FastAPI TestClient + mock`
- 用例：
  - `test_create_key_requires_admin_scope`
  - `test_create_key_success`
  - `test_list_keys_returns_no_plaintext`
  - `test_revoke_key_success`
  - `test_health_endpoint_no_auth`
- 每个测试函数包含清晰 docstring
- 使用 `@pytest.fixture` 管理 mock 对象

## Phase 6: 部署文档 + 最终验证

### 目标
形成部署指南与验收清单，支持逐条验收。

### 任务
1. 新建 `docs/AUTH_DEPLOY_GUIDE.md`
- 包含：
  - 前置条件
  - 快速开始（5 步）
  - 多客户端 key 管理示例（openclaw、claude_code、codex）
  - 回滚方案（`AUTH_ENABLED=false`）

2. 新建 `docs/AUTH_PLAN.md`
- 将用户需求原文整理为标准 Markdown 格式

### 执行要求
- 每阶段完成后提交一次 commit
