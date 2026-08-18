# MemOS 记忆使用率统计面板（Memory Usage Telemetry）

> 输入文档 · Hermes 实测核实 · 目标：给 MemOS Web 控制台加"记忆相关"使用率监测（非 LLM token 成本）

---

## 一、需求

用户要监测**记忆相关**的使用率：记忆库规模、类型构成、写入趋势、状态/置信度分布。
明确**不监测** LLM token 消耗 / 请求级成本。

## 二、现状（已实测核实）

### 2.1 数据源
- 记忆存储在 Neo4j（`naive_mem_cube.text_mem`，TreeTextMemory）
- 记忆节点 metadata 可用字段（实测 jxpro 库，10169 条）：
  - `memory_type`: UserMemory/LongTermMemory/WorkingMemory/OuterMemory
  - `created_at`: ISO 时间戳（可用于时间序列）
  - `status`: 当前全为 `activated`
  - `confidence`: 0~1 浮点
  - `tags`: 数组
  - `source_type`: 来源类型（chat/web/…）
  - `background`, `key` 等
- **`agent_id` 全为 None**——同步写入时未记录来源 Agent（无法按来源统计）

### 2.2 现有能力
- `/me/memories` 已支持 page/page_size/memory_type，`stats.by_type` 用轻量 Neo4j GROUP BY
- 前端用量页（UsageView.vue）已展示记忆总数 + 类型分布条形图

### 2.3 关键缺口
- **无记忆写入趋势**（按 created_at 天聚合）
- **无状态/置信度分布**
- **无来源（Agent）分布**（agent_id 缺失）

## 三、目标功能

### 3.1 后端：新增 `GET /me/memory-stats`（Web session 认证）
返回 JSON（全部用 Neo4j GROUP BY 轻量聚合，不导出全量节点）：
```json
{
  "total": 10169,
  "by_type": {"UserMemory": 3154, "LongTermMemory": 7015, ...},
  "by_status": {"activated": 10169},
  "by_confidence": {"high": 8000, "medium": 1500, "low": 669},
  "trend_30d": [
    {"date": "2026-07-20", "count": 12},
    ...
  ]
}
```
- `by_confidence` 分段：high ≥0.8 / medium 0.5~0.8 / low <0.5
- `trend_30d`：按 `created_at` 取日期（UTC 或本地均可，保持一致）GROUP BY，近 30 天
- 复用 `/me/memories` 的 cube 解析 + `get_grouped_counts` 模式（已在 me_router.py 实现）

### 3.2 前端：用量页增强
- 记忆趋势：近 30 天柱状图（Nuxt UI Chart 或简单 CSS bar）
- 状态/置信度：简单展示
- 保留现有类型分布

### 3.3（可选）来源补齐
- 改 Memmy→MemOS 同步脚本，写入 `/product/add` 时把来源 Agent 记为 agent_id
- 需清理/重新同步才有来源分布（数据量大，可选做）

## 四、技术要点
- Neo4j GROUP BY：`naive_mem_cube.text_mem.graph_store.get_grouped_counts(...)`（已在 me_router.py 验证用法）
- 时间聚合：created_at 按天 truncate；`get_grouped_counts` 支持 `group_fields=["created_at"]` 但需截断为日期字符串，可能需在 Cypher 里 `substring(toString(n.created_at),0,10)` 或拆字段
- 来源分布暂返回空（agent_id 缺失）

## 五、不做清单
- 不做 LLM token 用量/成本统计
- 不改记忆写入逻辑（除可选来源补齐）
- 不引入新依赖（用 Nuxt UI 现有组件）

## 六、验收清单
- [ ] `/me/memory-stats` 带 Web token 返回 total/by_type/by_status/by_confidence/trend_30d
- [ ] 无 token 401
- [ ] 前端用量页显示趋势图 + 状态/置信度（真实数据）
- [ ] typecheck/build/测试通过
