# MemOS 生产部署手册（自托管 · 鉴权加固版）

> 分支：feat/api-auth-hardening | 适用：统一入口 server_api（单端口 8000）
> 最后更新：2026-08-10

## 一、架构总览

```
                        ┌─────────────────────────────┐
  客户端 ── HTTPS ──▶ Caddy/nginx ──▶ :8000 (server_api)
                        │        ┌────────────────────┤
                        │        │ 25 业务端点 + 鉴权   │
                        │        │ /admin/* key 管理    │
                        │        │ 限流 + 安全头 + CORS │
                        └────────┴────────────────────┘
                              │ 容器内网
        ┌───────────────┬─────┴──────┬───────────────┐
   PostgreSQL(api_keys)  Neo4j(图记忆)  Qdrant(向量)  (LLM 走百炼/硅基)
```

## 二、前置条件

- 服务器：Docker Engine + Docker Compose v2（或 Docker Desktop）
- 域名 + 反向代理（Caddy/nginx，可选但推荐）
- 百炼 API Key（模型服务，见第四节）

## 三、首次部署

### 1. 拉取代码到私有分支
```bash
git clone https://github.com/fcmyoo/MemOS.git
cd MemOS
git checkout feat/api-auth-hardening
```

### 2. 生成密钥并配置 .env
```bash
# 生成 master key（保存输出，只显示一次）
PYTHONPATH=src python -c "
from memos.api.utils.api_keys import generate_master_key
k, h = generate_master_key()
print('ONE_TIME_MASTER_KEY =', k)
print('MASTER_KEY_HASH    =', h)
"

# 生成内部 secret 和数据库密码
python -c "import secrets; print('INTERNAL_SERVICE_SECRET =', secrets.token_urlsafe(48))"
python -c "import secrets; print('POSTGRES_PASSWORD =', secrets.token_urlsafe(24))"
python -c "import secrets; print('NEO4J_PASSWORD =', secrets.token_urlsafe(24))"

cp docker/.env.example .env
# 编辑 .env：填入上面生成的 4 个值 + 百炼 key + 你的 CORS 域名
```

### 3. 启动
```bash
cd docker
docker compose --env-file ../.env up -d --build
# 验证
curl http://127.0.0.1:8000/health   # {"status":"healthy"}
```

### 4. 配置反向代理（Caddy 示例）
```caddyfile
memos.your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```
> 若走反代，把 .env 里 `TRUST_PROXY_HEADERS=true`，并把 `MEMOS_BIND_ADDRESS=127.0.0.1`（默认已锁定本机，反代同机即可）

## 四、模型配置（百炼专属实例）

⚠️ **关键坑**：百炼**专属实例（maas）**的 key 只能配专属 host，配公共端点会报 `401 Incorrect API key`。

```env
# 专属实例地址（替换成你的实例名）
OPENAI_API_BASE=https://llm-<your-instance>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
QWEN_API_BASE=https://llm-<your-instance>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
MEMRADER_API_BASE=https://llm-<your-instance>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
MOS_EMBEDDER_API_BASE=https://llm-<your-instance>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1

# 三个 key 可用同一个
OPENAI_API_KEY=sk-xxxx
MEMRADER_API_KEY=sk-xxxx
MOS_EMBEDDER_API_KEY=sk-xxxx

# 模型
MOS_CHAT_MODEL=qwen3-max
MEMRADER_MODEL=qwen3-max
MOS_EMBEDDER_MODEL=text-embedding-v4
EMBEDDING_DIMENSION=1024
```

## 五、多用户开通流程

1. **创建用户**（SQLite 用户库，容器内）：
```bash
docker exec memos-api-docker python -c "
from memos.mem_user.user_manager import UserManager
mgr = UserManager()
uid = mgr.create_user('alice', user_id='alice-id')
mgr.create_cube('alice-cube', owner_id=uid, cube_id='alice-cube')
print('created:', uid)
"
```

2. **签发 API key**（master key 调 admin 接口）：
```bash
curl -X POST http://127.0.0.1:8000/admin/keys \
  -H "Authorization: Bearer mk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"user_name":"alice","scopes":["read","write"],"description":"alice prod"}'
# 返回的 key 只显示一次，务必保存
```

3. **客户端调用**：
```bash
curl -X POST http://127.0.0.1:8000/product/search \
  -H "Authorization: Bearer krlk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"query":"xxx","user_id":"alice-id","readable_cube_ids":["alice-cube"]}'
```

## 六、运维

### 备份
```bash
# 手动备份（PostgreSQL + Neo4j + Qdrant 数据卷）
bash scripts/backup.sh
# 或定时（crontab 示例：每天凌晨 3 点）
0 3 * * * cd /path/to/MemOS && BACKUP_DIR=/mnt/backup bash scripts/backup.sh
```

### 日志
- 日志在容器内 `/home/memos/.memos/logs/memos.log`（按天轮转，默认保留 14 天，`LOG_BACKUP_DAYS` 可调）
- 查看：`docker logs -f memos-api-docker` 或 `docker exec memos-api-docker tail -f /home/memos/.memos/logs/memos.log`

### 更新（上游同步）
```bash
# main 拉上游 → 合并到私有分支（保留鉴权逻辑）
bash scripts/sync-upstream.sh
```

### 常见问题
| 症状 | 原因 | 解决 |
|---|---|---|
| 401 Incorrect API key | key 配了公共端点 | 改成专属 host（见第四节） |
| /admin/keys 404 | AUTH_ENABLED=false | 设为 true 并重启 |
| 429 Too Many Requests | 触发限流 | 调大 RATE_LIMIT 或窗口 |
| 启动卡在 HF 下载 | hf-mirror 超时 | 保持 HF_HUB_OFFLINE=1 |
| 客户端 403 越权 | cube 未授权 | 给用户 add_user_to_cube 或建自己的 cube |

## 七、安全清单（发布前确认）

- [ ] .env 不进 git（已在 .gitignore ✅）
- [ ] master key 明文只保存一次，库里只有 SHA-256
- [ ] 日志不泄露 key（已修复 ✅）
- [ ] 限流开启（RATE_LIMIT_ENABLED=true）
- [ ] 非 root 运行（Dockerfile USER memos ✅）
- [ ] CORS_ORIGINS 配了你的域名（不能空着对外）
- [ ] 端口只绑 127.0.0.1，外网走反代
