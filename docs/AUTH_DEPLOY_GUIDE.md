# MemOS 鉴权部署指南

## 前置条件
- 已安装 Docker 与 Docker Compose
- 已有可运行的 MemOS 部署环境（含 PostgreSQL）

## 快速开始

### 步骤 1: 生成 Master Key
```bash
python -m memos.api.utils.generate_master_key
```

执行后会输出：
- `MASTER_KEY: <明文>`（仅展示一次，请安全保存）
- `MASTER_KEY_HASH=<hash>`（用于写入 `.env`）

如需自动追加到指定 `.env`：
```bash
python -m memos.api.utils.generate_master_key --output-env docker/.env
```

### 步骤 2: 配置 `.env`
至少需要如下环境变量：

```env
AUTH_ENABLED=true
MASTER_KEY_HASH=<步骤1生成的哈希>
DOCS_PUBLIC=true

# PostgreSQL 连接（按实际部署填写）
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=memos
POSTGRES_PASSWORD=<你的数据库密码>
POSTGRES_DB=memos
```

可选（内部服务绕过鉴权）：
```env
INTERNAL_SERVICE_SECRET=<仅内网服务共享的密钥>
```

### 步骤 3: 启动服务
```bash
docker compose up -d
```

### 步骤 4: 创建业务 API Key
以下示例使用 Master Key 调用管理接口创建业务 key。

```bash
MASTER_KEY="mk_xxx"
BASE_URL="http://localhost:8001"

# 创建 read key
curl -X POST "${BASE_URL}/admin/keys" \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_name": "app_reader",
    "scopes": ["read"],
    "description": "read-only key"
  }'

# 创建 write key
curl -X POST "${BASE_URL}/admin/keys" \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_name": "app_writer",
    "scopes": ["read", "write"],
    "description": "read-write key"
  }'
```

### 步骤 5: 验证
假设：
- `READ_KEY=<read key>`
- `WRITE_KEY=<write key>`

```bash
BASE_URL="http://localhost:8001"

# 1) 无 key 访问 -> 401
curl -i -X POST "${BASE_URL}/product/search" \
  -H "Content-Type: application/json" \
  -d '{"query":"hello","user_id":"u1"}'

# 2) read key 查询 -> 200
curl -i -X POST "${BASE_URL}/product/search" \
  -H "Authorization: Bearer ${READ_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"query":"hello","user_id":"u1"}'

# 3) read key 写入 -> 403
curl -i -X POST "${BASE_URL}/product/add" \
  -H "Authorization: Bearer ${READ_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","memory_content":"new memory"}'

# 4) write key 写入 -> 200
curl -i -X POST "${BASE_URL}/product/add" \
  -H "Authorization: Bearer ${WRITE_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","memory_content":"new memory"}'
```

吊销后访问验证：

```bash
# 先查询 key 列表，找到要吊销 key 的 id
curl -X GET "${BASE_URL}/admin/keys" \
  -H "Authorization: Bearer ${MASTER_KEY}"

# 吊销 key
KEY_ID="<待吊销key_id>"
curl -i -X DELETE "${BASE_URL}/admin/keys/${KEY_ID}" \
  -H "Authorization: Bearer ${MASTER_KEY}"

# 吊销后再次访问 -> 401
curl -i -X POST "${BASE_URL}/product/search" \
  -H "Authorization: Bearer ${READ_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"query":"hello","user_id":"u1"}'
```

## 多客户端 Key 管理示例

```bash
MASTER_KEY="mk_xxx"
BASE_URL="http://localhost:8001"

# openclaw
curl -X POST "${BASE_URL}/admin/keys" \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_name":"openclaw","scopes":["read","write"],"description":"openclaw client"}'

# claude_code
curl -X POST "${BASE_URL}/admin/keys" \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_name":"claude_code","scopes":["read"],"description":"claude code client"}'

# codex
curl -X POST "${BASE_URL}/admin/keys" \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_name":"codex","scopes":["read","write"],"description":"codex client"}'
```

## 回滚方案
紧急回滚只需在 `.env` 设置：

```env
AUTH_ENABLED=false
```

重启服务后将立即关闭鉴权校验。
