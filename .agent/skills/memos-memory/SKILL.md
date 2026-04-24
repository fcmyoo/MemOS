---
name: memos-memory
description: >
  通过 HTTP API 调用 MemOS 记忆系统，实现记忆的存储、检索和基于记忆的对话。
  适用于任何支持 HTTP 请求的 AI 开发工具。当用户提到记忆管理、长期记忆、
  上下文持久化、用户偏好存储、多轮对话记忆、知识库管理，或需要在对话间保持
  上下文连续性时，请使用此 Skill。即使用户没有明确提到 "MemOS"，只要涉及
  记忆、上下文、偏好存储等概念，也应考虑触发此 Skill。
---

# MemOS Memory API

通过 HTTP API 与已部署的 MemOS 服务交互，为 AI Agent 提供长期记忆能力。

## 环境变量

使用前必须在环境变量或 `.env` 文件中配置：

```
MEMOS_API_BASE_URL=https://your-memos-host/product
MEMOS_API_KEY=your_api_key_here
```

所有请求必须携带 API Key：
```
Authorization: ${MEMOS_API_KEY}
```

## 核心端点

### 1. 添加记忆 — POST /product/add

将对话或文本写入记忆库。

**简单文本：**
```bash
curl -X POST "${MEMOS_API_BASE_URL}/add" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "user_id": "my_agent",
    "messages": "用户偏好暗色主题，喜欢 TypeScript",
    "async_mode": "sync"
  }'
```

**结构化对话：**
```bash
curl -X POST "${MEMOS_API_BASE_URL}/add" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "user_id": "my_agent",
    "writable_cube_ids": ["<cube_id>"],
    "messages": [
      {"role": "user", "content": "我喜欢用 Next.js"},
      {"role": "assistant", "content": "好的，后续优先使用 Next.js。"}
    ],
    "async_mode": "sync",
    "custom_tags": ["preference", "tech_stack"]
  }'
```

关键参数：
- `user_id`（必填）: 用户唯一标识
- `messages`: 字符串或消息列表
- `writable_cube_ids`: 写入的记忆库 ID 列表
- `async_mode`: `"sync"` 同步 | `"async"` 异步（默认）
- `custom_tags`: 自定义标签，搜索时可用于过滤

### 2. 搜索记忆 — POST /product/search

```bash
curl -X POST "${MEMOS_API_BASE_URL}/search" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "user_id": "my_agent",
    "query": "用户喜欢什么前端框架？",
    "top_k": 10,
    "mode": "fast",
    "include_preference": true
  }'
```

关键参数：
- `query`（必填）: 搜索文本
- `user_id`（必填）: 用户 ID
- `readable_cube_ids`: 搜索的记忆库列表
- `mode`: `"fast"` | `"fine"` | `"mixture"`
- `top_k`: 返回结果数（默认 10）
- `relativity`: 相关性阈值（默认 0.45，0 表示不过滤）
- `include_preference`: 是否包含偏好记忆（默认 true）
- `search_tool_memory`: 是否包含工具记忆（默认 true）

### 3. 带记忆的对话 — POST /product/chat/complete

```bash
curl -X POST "${MEMOS_API_BASE_URL}/chat/complete" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "user_id": "my_agent",
    "query": "帮我推荐一个前端框架",
    "mode": "fast",
    "top_k": 10,
    "add_message_on_answer": true,
    "session_id": "session_001"
  }'
```

关键参数：
- `query`（必填）: 用户问题
- `user_id`（必填）: 用户 ID
- `add_message_on_answer`: 自动保存本轮对话为记忆（默认 true）
- `session_id`: 会话 ID
- `system_prompt`: 自定义系统提示词
- `readable_cube_ids` / `writable_cube_ids`: 读写的记忆库

### 4. 获取记忆 — POST /product/get_memory

```bash
curl -X POST "${MEMOS_API_BASE_URL}/get_memory" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "mem_cube_id": "<cube_id>",
    "include_preference": true,
    "include_tool_memory": true
  }'
```

### 5. 删除记忆 — POST /product/delete_memory

```bash
curl -X POST "${MEMOS_API_BASE_URL}/delete_memory" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "writable_cube_ids": ["<cube_id>"],
    "memory_ids": ["<memory_id_1>", "<memory_id_2>"]
  }'
```

### 6. 记忆反馈 — POST /product/feedback

对记忆进行纠正或补充：

```bash
curl -X POST "${MEMOS_API_BASE_URL}/feedback" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${MEMOS_API_KEY}" \
  -d '{
    "user_id": "my_agent",
    "history": [
      {"role": "user", "content": "我喜欢 React"},
      {"role": "assistant", "content": "好的，记住了。"}
    ],
    "feedback_content": "其实我更喜欢 Vue.js，请更新记忆",
    "writable_cube_ids": ["<cube_id>"],
    "async_mode": "sync"
  }'
```

## 使用模式

### 开始新任务前 → 搜索记忆
`POST /product/search` 查找相关上下文，注入当前对话。

### 完成任务后 → 保存记忆
提取关键信息，`POST /product/add` 保存。

### 持续对话 → 自动记忆
`POST /product/chat/complete`（`add_message_on_answer: true`）自动检索 + 记忆。

### 纠正错误记忆 → 反馈
`POST /product/feedback` 纠正或补充已有记忆。

## 详细参考

完整的请求体 Schema、响应格式和高级参数见 `references/api_reference.md`。
