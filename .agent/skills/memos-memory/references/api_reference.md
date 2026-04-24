# MemOS API 完整参考

> 基于实际 OpenAPI 规范 (`/openapi.json`)，版本 1.0.1

## 目录
1. [添加记忆 (add)](#1-添加记忆)
2. [搜索记忆 (search)](#2-搜索记忆)
3. [对话 (chat/complete)](#3-对话)
4. [流式对话 (chat/stream)](#4-流式对话)
5. [获取记忆 (get_memory)](#5-获取记忆)
6. [获取全部记忆 (get_all)](#6-获取全部记忆)
7. [删除记忆 (delete_memory)](#7-删除记忆)
8. [记忆反馈 (feedback)](#8-记忆反馈)
9. [调度器状态](#9-调度器状态)
10. [认证](#10-认证)

---

## 1. 添加记忆

**端点**: `POST /product/add`
**Schema**: `APIADDRequest`

```json
{
  "user_id": "string (必填)",
  "messages": "string | MessageList | InputItemList | null",
  "writable_cube_ids": ["string (可选)"],
  "session_id": "string (可选)",
  "task_id": "string (可选，用于异步任务监控)",
  "async_mode": "async | sync (默认 async)",
  "mode": "fast | fine (仅 sync 模式生效)",
  "custom_tags": ["string (可选)"],
  "is_feedback": false,
  "info": {
    "agent_id": "string",
    "app_id": "string",
    "source_type": "string",
    "source_url": "string"
  },
  "chat_history": "MessageList | null (可选)"
}
```

### messages 格式

**纯文本：**
```json
"messages": "用户喜欢深色主题"
```

**对话消息列表 (MessageList)：**
```json
"messages": [
  {"role": "system", "content": "You are a helpful assistant."},
  {"role": "user", "content": "我喜欢 Python", "chat_time": "2025-01-01T10:00:00Z"},
  {"role": "assistant", "content": "记住了。", "chat_time": "2025-01-01T10:00:05Z"}
]
```

**带工具调用：**
```json
"messages": [
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [{
      "id": "call_1",
      "type": "function",
      "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"}
    }]
  },
  {
    "role": "tool",
    "content": "北京 25°C 晴",
    "tool_call_id": "call_1"
  }
]
```

**多模态消息（文本 + 图片）：**
```json
"messages": [{
  "role": "user",
  "content": [
    {"type": "text", "text": "看看这张图"},
    {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg", "detail": "high"}}
  ]
}]
```

**文件输入 (InputItemList)：**
```json
"messages": [
  {"type": "file", "file": {"file_data": "base64_content", "filename": "doc.pdf"}},
  {"type": "file", "file": {"file_id": "uploaded_file_id"}}
]
```

---

## 2. 搜索记忆

**端点**: `POST /product/search`
**Schema**: `APISearchRequest`

```json
{
  "query": "string (必填)",
  "user_id": "string (必填)",
  "readable_cube_ids": ["string (可选)"],
  "mode": "fast | fine | mixture (默认 fast)",
  "top_k": 10,
  "relativity": 0.45,
  "dedup": "no | sim | mmr (默认 mmr)",
  "include_preference": true,
  "pref_top_k": 6,
  "search_tool_memory": true,
  "tool_mem_top_k": 6,
  "include_skill_memory": true,
  "skill_mem_top_k": 3,
  "session_id": "string (可选，软权重信号)",
  "internet_search": false,
  "search_memory_type": "All | WorkingMemory | LongTermMemory | UserMemory | OuterMemory | ToolSchemaMemory | ToolTrajectoryMemory | RawFileMemory | AllSummaryMemory | SkillMemory",
  "neighbor_discovery": false,
  "filter": {"and": [{"created_at": {"gt": "2024-01-01"}}]},
  "chat_history": "MessageList | null"
}
```

**响应 (`SearchResponse`)：**
```json
{
  "code": 200,
  "message": "success",
  "data": {
    "text_mem": [{"memory": "...", "score": 0.92}],
    "pref_mem": [...],
    "tool_mem": [...]
  }
}
```

---

## 3. 对话

**端点**: `POST /product/chat/complete`
**Schema**: `APIChatCompleteRequest`

```json
{
  "user_id": "string (必填)",
  "query": "string (必填)",
  "readable_cube_ids": ["string"],
  "writable_cube_ids": ["string"],
  "history": "MessageList | null",
  "mode": "fast | fine | mixture (默认 fast)",
  "system_prompt": "string (可选)",
  "top_k": 10,
  "session_id": "string (可选)",
  "include_preference": true,
  "pref_top_k": 6,
  "model_name_or_path": "string (可选)",
  "max_tokens": "int (可选)",
  "temperature": "float (可选)",
  "top_p": "float (可选)",
  "add_message_on_answer": true,
  "relativity": 0.45,
  "internet_search": false,
  "threshold": 0.5,
  "filter": {}
}
```

---

## 4. 流式对话

**端点**: `POST /product/chat/stream`
**Schema**: `ChatRequest`

参数与 `chat/complete` 基本相同，返回 SSE 流。

---

## 5. 获取记忆

**端点**: `POST /product/get_memory`
**按 ID 获取**: `GET /product/get_memory/{memory_id}`
**批量获取**: `POST /product/get_memory_by_ids`（body: `["id1", "id2"]`）

```json
{
  "mem_cube_id": "string (必填)",
  "user_id": "string (可选)",
  "include_preference": true,
  "include_tool_memory": true,
  "include_skill_memory": true,
  "filter": {},
  "page": 1,
  "page_size": 20
}
```

---

## 6. 获取全部记忆

**端点**: `POST /product/get_all`
**Schema**: `GetMemoryPlaygroundRequest`

```json
{
  "user_id": "string (必填)",
  "memory_type": "text_mem | act_mem | param_mem | para_mem (必填)",
  "mem_cube_ids": ["string (可选)"],
  "search_query": "string (可选，模糊搜索)",
  "search_type": "embedding | fulltext (默认 fulltext)"
}
```

---

## 7. 删除记忆

**端点**: `POST /product/delete_memory`
**Schema**: `DeleteMemoryRequest`

```json
{
  "writable_cube_ids": ["string (必填)"],
  "memory_ids": ["string (可选)"],
  "file_ids": ["string (可选)"],
  "filter": {}
}
```

---

## 8. 记忆反馈

**端点**: `POST /product/feedback`
**Schema**: `APIFeedbackRequest`

```json
{
  "user_id": "string (必填)",
  "history": "MessageList (必填)",
  "feedback_content": "string (必填)",
  "session_id": "string (默认 default_session)",
  "retrieved_memory_ids": ["string (可选)"],
  "feedback_time": "string (可选)",
  "writable_cube_ids": ["string"],
  "async_mode": "sync | async (默认 async)",
  "corrected_answer": false
}
```

---

## 9. 调度器状态

- **全部状态**: `GET /product/scheduler/allstatus`
- **用户任务状态**: `GET /product/scheduler/status?user_id=xxx&task_id=xxx`
- **队列状态**: `GET /product/scheduler/task_queue_status?user_id=xxx`
- **等待完成**: `POST /product/scheduler/wait?user_name=xxx&timeout_seconds=120`

---

## 10. 认证

认证方式: `APIKeyHeader`（header 名: `Authorization`）

所有 `/product/*` 和 `/admin/*` 端点都需要认证。

```bash
curl -H "Authorization: ${MEMOS_API_KEY}" ...
```

### Admin 端点
- `POST /admin/keys` — 创建 API Key
- `GET /admin/keys` — 列出所有 Key
- `DELETE /admin/keys/{key_id}` — 撤销 Key
- `POST /admin/generate-master-key` — 生成 Master Key
- `GET /admin/health` — Admin 健康检查（无需认证）
- `GET /health` — 服务健康检查（无需认证）
