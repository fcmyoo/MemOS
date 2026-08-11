# MemOS 管理后台 + 用户自助门户 — 审计清单（方案输入）

> 审计人：Hermes Agent | 日期：2026-08-10
> 分支：feat/api-auth-hardening（含统一入口 + Cube ACL + 生产加固）

## 一、目标

实现类 MemOS Cloud 的**管理后台 + 用户自助门户**：

1. **管理员**：Web 登录 → 管理所有用户（CRUD：建/查/改/删用户、角色、启停用、cube 分配）
2. **普通用户**：Web 登录 → 只能管理自己 → 自助创建/吊销自己的 API token（Bearer）
3. 权限边界：**管理员管全部，用户只管自己**（严格数据隔离）

## 二、现状盘点（后端已有能力）

### ✅ 已具备（可直接复用）
| 能力 | 位置 | 说明 |
|---|---|---|
| User 模型（user_id/user_name/role/is_active） | `mem_user/user_manager.py:57` | 角色 ROOT/ADMIN/USER/GUEST |
| UserManager CRUD | 同上 `create_user/get_user/list_users/delete_user` | 完整 |
| Cube 租户隔离 | 同上 `create_cube/add_user_to_cube/validate_user_cube_access` | 完整 |
| API key 签发/吊销/列表 | `api/routers/admin_router.py` `POST/GET/DELETE /admin/keys` | **已有！** 受 master key 保护 |
| 统一入口 server_api | 合并 admin_router + 限流 + 安全头 | 单端口 |
| master key 管理 | `generate_master_key()` | 管理员凭证 |

### ❌ 缺失（需开发）
| 缺失 | 说明 |
|---|---|
| **密码与会话** | User 模型**无密码字段**；无登录接口、无 session/token 会话机制 |
| **用户 CRUD API** | admin_router 只有 key 管理，**无用户管理端点**（create_user 等是 Python API 无 HTTP 暴露） |
| **用户自助 API** | 用户无法登录后自建 key（现在只能管理员代签） |
| **Web 前端** | 无任何页面（纯 API） |
| **RBAC 路由** | admin 端点无 ROOT/ADMIN 角色校验（现在靠 master key 一把钥匙） |

## 三、功能矩阵（目标 vs 现状）

| 功能 | 管理员 | 普通用户 | 现状 |
|---|---|---|---|
| 登录（密码） | ✅ 需开发 | ✅ 需开发 | ❌ 无 |
| 查看所有用户 | ✅ 需开发 | ❌ 只看到自己 | ❌ 无 API |
| 创建用户 | ✅ 需开发 | ❌ | ❌ 无 API |
| 编辑用户（角色/启停） | ✅ 需开发 | ❌ | ❌ 无 API |
| 删除用户 | ✅ 需开发 | ❌ | ❌ 无 API |
| 创建自己的 API key | ✅ | ✅ 需开发 | ⚠️ 仅管理员可代签 |
| 吊销自己的 key | ✅ | ✅ 需开发 | ⚠️ 仅管理员可代签 |
| 查看自己的 key 列表 | ✅ | ✅ 需开发 | ⚠️ 仅管理员可查全部 |
| 分配 cube 给用户 | ✅ 需开发 | ❌ | ❌ 无 API |

## 四、架构方向（初步）

```
浏览器 ── 管理后台页(React/静态) ──▶ /admin/*   (ROOT/ADMIN 角色)
浏览器 ── 用户门户页(React/静态) ──▶ /me/*     (登录用户，仅自己)
浏览器 ── 登录页 ──▶ /auth/login (密码 → 签发会话 token)
```

**三层设计**：
1. **认证层**：密码登录 → 会话 token（JWT 或服务端 session）——新开发
2. **用户管理 API**：`/admin/users` CRUD（ROOT/ADMIN 角色校验）——新开发
3. **自助 API**：`/me/keys` 创建/吊销/列表（仅自己）——新开发，复用 api_keys 存储

**关键决策点（需 Codex 方案定）**：
1. 密码存储：bcrypt/argon2？（User 表加 password_hash 列，需 SQLite migration）
2. 会话机制：JWT（无状态）vs 服务端 session（可吊销）？
3. Web 前端形态：独立 React SPA（构建产物挂 /download 或静态目录）vs 轻量 HTML+JS（无构建）？
4. 现有 admin_router 的 `/admin/keys` 与新的 `/me/keys` 关系：管理员可代签（保留）+ 用户自助（新增）？
5. master key 与新 ROOT/ADMIN 角色密码登录的关系：并存还是统一？
6. AUTH_ENABLED=false 时 Web 登录是否可用？
7. 会话 token 与 API key（krlk_）体系是否统一？

## 五、约束

1. 不改 25 业务端点 contract；Cube ACL（403 统一）不回归
2. 不改 `CreateKeyRequest.scopes` 枚举、不改 api_keys 表 schema（除非方案明确论证）
3. 现有 240 测试全绿；新增测试覆盖：登录/会话/CRUD/自助/越权（用户管自己）
4. 用户只能管理自己：任何 /me/* 越权访问他人数据必须 403（防枚举）
5. 管理员角色校验：非 ROOT/ADMIN 调 /admin/* 必须 403
6. 生产加固不回归：日志不泄 key、限流、非 root、CORS
7. 密码绝不明文存储/日志；Web 页面不引入重型构建链（除非必要）

## 六、验收标准

1. 管理员登录后：CRUD 用户、分配 cube、代签 key、吊销 key 全可用
2. 普通用户登录后：看到自己信息、自建/吊销自己的 key；访问他人数据 403
3. 未登录访问 /admin/* /me/* → 401
4. 非管理员访问 /admin/* → 403
5. 密码哈希存储、日志零泄露
6. 全量测试通过 + Docker 实测（登录 → 建用户 → 用户自建 key → 调用业务）
