# MemOS 多用户隔离与冲突风险调研（管理员视角）

> 调研人：Hermes Agent | 日期：2026-08-09
> 基于：feat/api-auth-hardening（6f37591f）真实源码逐行核实

## 一、核心结论（先看这个）

| 维度 | 结论 | 风险等级 |
|---|---|---|
| API key 层隔离 | ✅ 完整（每个 key 绑定 user_name + scopes） | 安全 |
| 数据存储隔离 | ✅ 按 cube_id 分桶存储（互不串数据） | 安全 |
| **Cube 访问权限校验（HTTP API 路径）** | ❌ **缺失** | 🔴 高危 |
| 全局组件共享（LLM/embedder/scheduler） | ⚠️ 单例共享，有状态竞争 | 🟡 中危 |
| 后台任务（dream/scheduler） | ⚠️ 全局单例 | 🟡 中危 |

**一句话**：你的鉴权加固解决了"谁能调 API"，但**没解决"谁的数据能被谁看到"**。
当前 HTTP API 路径上，任何持有合法 key 的用户，只要知道别人的 cube_id，就能读写别人的记忆。

---

## 二、✅ 已经安全的（鉴权层）

1. **Key 绑定用户**：`api_keys.user_name` 与业务用户关联（api_keys.py:115）
2. **Scope 控制**：read/write/admin 分层（auth.py:240 `require_scope`）
3. **数据分桶存储**：记忆按 `mem_cube_id` 隔离存取
   （memory_handler.py:56 `get_all(user_name=mem_cube_id)` —— cube_id 即存储命名空间）

## 三、🔴 高危：Cube 访问权限校验缺失

### 事实（源码核实）

- 权限校验函数 `validate_user_cube_access()` 存在于 user_manager.py:305
- **但只在 `mem_os/core.py`（MOS SDK 库层）被调用**（core.py:228, 512）
- **HTTP API 的 handlers 路径完全不走它**：
  - `search_handler._resolve_cube_ids()`（search_handler.py:1031）→ 直接返回请求里的 `readable_cube_ids`，**无任何归属校验**
  - `memory_handler` 直接用请求的 `mem_cube_id` 读写
  - `cube_handler.register_cube` 自己注释承认：*"full registration requires MOSCore integration (not yet available in API context)"*

### 攻击场景

```
1. 管理员给 alice/bob 各发一个 key（read scope）
2. alice 调用 /product/search，body 里带 bob 的 cube_id
3. → 200 OK，返回 bob 的全部记忆 ❌
```

只要知道 cube_id（且 cube_id 常是 user_id 或可猜测的 ID），隔离即失效。

### 修复方向（后续方案）

在 search/add/cube 各 handler 入口，用 key 关联的 user_name → 查 `validate_user_cube_access(user, cube)`，未授权返回 403。**这正是需要 Codex 出方案、Claude 执行的下一轮任务**。

---

## 四、🟡 中危：全局单例组件冲突

### 1. Scheduler（调度器）单例共享

- `init_server()` 全局只建**一个** scheduler（component_init.py:109）
- `_mem_cubes` 是所有 cube 的集合（base_scheduler.py:367），scheduler 在它们之间切换
- `current_mem_cube_id` 是**可变状态**（base_scheduler.py:166, 380）
- **冲突点**：用户 A 触发调度把 `current_mem_cube` 切到 A 的 cube，用户 B 的请求可能命中 A 的上下文 → 记忆串扰

### 2. LLM / Embedder / Reranker 全局共享

- 所有用户共用同一套模型配置与调用（component_init.py 构建一次）
- **冲突点**：无数据串扰，但**配额/速率共享**——一个用户打爆模型限流，全站受影响

### 3. Dream（记忆提炼）全局运行

- dream 是全局进程，处理所有 cube 的提炼（dream/contextualization.py）

### 4. Redis 队列（若启用）

- `MEMSCHEDULER_USE_REDIS_QUEUE=true` 时队列全局，stream key 需按 cube 区分
- 环境变量 `MEMSCHEDULER_STREAM_KEY_PREFIX` 存在（component_init.py:121），但需确认 per-cube 前缀是否真隔离

---

## 五、多用户部署建议（管理员实践）

### 短期（现在就做，零代码）
1. **先只开放信任用户**：key 只发给可信者，接受"数据不隔离"风险
2. **每个用户独立 cube**：create_cube 时 user 与 cube 1:1，靠"约定"隔离
3. **只给 read scope 给外部用户**：write scope 仅限自己
4. **开启 AUTH_ENABLED=true + 强 master key**（你的加固已就位）

### 中期（需要开发，建议按多引擎协议立项）
1. **补 Cube 访问校验**（高危，第一优先）——search/add/delete 入口加 `validate_user_cube_access`
2. **Scheduler per-cube 状态隔离**——或为每个用户请求重建上下文，避免 current_mem_cube 串扰
3. **Redis stream key 按 cube 加前缀**

### 长期
1. 自助注册 + 默认 cube 自动创建
2. 配额管理（每用户 key 数量、调用频率）
3. 审计日志（谁在何时访问了哪个 cube）

---

## 六、风险定级汇总

```
🔴 P0  Cube 访问权限缺失（数据泄露风险）  → 必须修复后才宜对外开放
🟡 P1  Scheduler 全局状态串扰           → 多用户并发时必须处理
🟡 P2  LLM 配额共享                     → 运营层面控制
🟢 P3  审计/配额/自助注册                → 产品化阶段
```

**部署红线建议**：在 P0 修复前，不要把系统开放给不可信第三方；仅限内部可信用户试用。

## 七、参考文件

- `src/memos/mem_user/user_manager.py` — validate_user_cube_access（305 行）、User/Cube 模型
- `src/memos/mem_os/core.py` — 唯一正确调用权限校验的地方（228/512 行）
- `src/memos/api/handlers/search_handler.py` — _resolve_cube_ids（1031 行）无校验
- `src/memos/api/handlers/memory_handler.py` — 直接用请求 cube_id 读写
- `src/memos/api/handlers/cube_handler.py` — register_cube 自认缺 MOSCore 集成
- `src/memos/api/handlers/component_init.py` — 全局单例初始化
- `src/memos/mem_scheduler/base_scheduler.py` — 单例 + current_mem_cube_id 可变状态
