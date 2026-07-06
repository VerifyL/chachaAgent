# RPC 消息模型 (`protocol/rpc_schema.py`)

本文档详细说明 ChachaAgent 通信协议中所有消息类型的字段含义、使用场景和数据流向。`rpc_schema.py` 定义了组件间的**唯一通信语言** —— 所有模块（CLI/Web 前端、Gateway、Orchestrator、LLMInvoker、ToolExecutor）均通过这套消息格式交互。

## 概述

设计遵循 **JSON-RPC 2.0 规范**，并在此基础上增加 Gateway 层的路由信息。

- **三层消息结构**：GatewayMessage（路由包装）→ RPCRequest/RPCResponse/RPCEvent（JSON-RPC 2.0）→ 具体事件子类
- **请求-响应模式**：客户端发 `RPCRequest`，服务端回 `RPCResponse`（通过 `id` 关联）
- **推送模式**：服务端发 `RPCEvent`（无 `id` 字段，不需要响应），如流式 token、工具状态
- **会话复用**：`GatewayMessage.seq` 自增保证顺序，`session_id` 关联同一会话

消息流向示意：
```
┌─CLI/Web─┐   RPCRequest ─→  ┌─Gateway─┐  ─→  ┌─Orchestrator─┐
│         │←── RPCEvent ──── │         │ ←─── │              │
└─────────┘                   └─────────┘       └──────────────┘
                                                       │
                                              TokenChunkEvent（流式）
                                              ToolStatusEvent（工具进度）
                                              PermissionRequestEvent（审批）
                                              AuditTrailEvent（审计）
```

---

## 1. 基础类型：`RPCError`

```python
class RPCError(BaseModel):
    code: int              # 错误码
    message: str           # 人类可读描述
    data: Optional[Any]    # 附加信息
```

**用途**：嵌入 `RPCResponse.error`，表示请求失败。

| 字段 | 说明 |
|------|------|
| `code` | JSON-RPC 2.0 标准错误码：-32700（解析错）、-32600（非法请求）、-32601（方法不存在）、-32602（参数错）、-32603（内部错） |
| `message` | 错误描述文本（如 "Invalid Request"） |
| `data` | 附加错误详情（如堆栈信息、参数名） |

---

## 2. 网关层包装：`GatewayMessage`

```python
class GatewayMessage(BaseModel):
    seq: int                        # 全局自增序列号
    project_id: Optional[str]       # 项目 ID
    session_id: Optional[str]       # 会话 ID
    payload: Union[RPCRequest, RPCResponse, RPCEvent]  # JSON-RPC 消息
```

**用途**：所有消息的外层包装。Gateway 根据 `seq` 保证顺序，按 `session_id` + `project_id` 路由到正确的会话。**Gateway 不解析 payload 的业务内容**，只做路由。

| 字段 | 说明 |
|------|------|
| `seq` | 全局自增序列号（≥0），Gateway 分配，保证消息有序投递 |
| `project_id` | 项目标识，多项目场景下隔离会话和记忆（可为空，表示系统级消息） |
| `session_id` | 会话标识，同一会话的所有消息共享，支持断线重连后恢复 |
| `payload` | JSON-RPC 2.0 消息体，具体类型由 Gateway/MCP 客户端按 `method` 字段路由 |

> 🔮 **多模态预留（后续版本）**：`payload` 的联合类型可扩展为 `Union[..., ImageChunk, AudioChunk]`，在不改动现有模型的前提下支持图片/音频消息。

---

## 3. JSON-RPC 2.0 消息基类

### 3.1 `RPCRequest` — 请求

```python
class RPCRequest(BaseModel):
    jsonrpc: str = "2.0"           # 固定
    id: str                        # UUID4，与响应关联
    method: str                    # 方法名（如 user/message、tool/execute）
    params: Optional[Dict] = {}    # 参数
```

**用途**：客户端→服务端的调用请求。`id` 用于匹配 `RPCResponse`。

| 字段 | 说明 |
|------|------|
| `jsonrpc` | 固定为 `"2.0"`，frozen 不可改 |
| `id` | 自动生成 UUID4，服务端响应时原样返回 |
| `method` | 方法名，格式为 `模块/操作`（如 `user/message`、`tool/execute`、`config/reload`） |
| `params` | 方法参数，key-value 字典 |

### 3.2 `RPCResponse` — 响应

```python
class RPCResponse(BaseModel):
    jsonrpc: str = "2.0"           # 固定
    id: str                        # 对应请求的 id
    result: Optional[Any]           # 成功结果
    error: Optional[RPCError]       # 错误信息
```

**用途**：服务端→客户端的调用结果。`result` 和 `error` 互斥。

| 字段 | 说明 |
|------|------|
| `id` | 对应 `RPCRequest.id`，客户端据此匹配请求 |
| `result` | 成功时返回的数据（任意 JSON 可序列化类型） |
| `error` | 失败时返回的 `RPCError` 对象 |

### 3.3 `RPCEvent` — 推送事件

```python
class RPCEvent(BaseModel):
    jsonrpc: str = "2.0"           # 固定
    method: str                    # 事件方法名
    params: Optional[Dict] = {}    # 事件参数
```

**用途**：服务端→客户端的**单向推送**。无 `id` 字段，客户端不需要响应。`extra=forbid` 保证结构严格。

**与 `RPCRequest` 的区别**：

| | RPCRequest | RPCEvent |
|---|---|---|
| 方向 | 客户端 → 服务端 | 服务端 → 客户端 |
| 有 id 吗 | ✅ 有 | ❌ 无 |
| 需要响应吗 | ✅ 需要 | ❌ 不需要 |
| 示例 | "帮我执行 read_file" | "流式输出: 你 好" |

---

## 4. 具体事件类型

每个事件类型都有固定的 `method` 值，通过 `model_validator` 校验。这保证了 `GatewayPayload` 联合类型的正确反序列化。

### 4.1 `TokenChunkEvent` — 流式文本输出

```python
method = "stream/token"
params = {delta, finish_reason, tool_call_delta}
```

**用途**：LLMInvoker 每收到一个 token 就推送，前端累积渲染。

| 参数 | 说明 |
|------|------|
| `delta` | 当前 token 的文本片段（如 "你"、"好"） |
| `finish_reason` | 结束原因：`"stop"`=自然结束，`"tool_calls"`=模型要调用工具，`"length"`=超长截断 |
| `tool_call_delta` | 当 `finish_reason="tool_calls"` 时，携带 `ToolCallDelta` 增量（工具名、参数片段） |

**便捷方法**：
- `set_delta(text)` → 设置文本片段
- `set_finish(reason)` → 设置结束标志
- `set_tool_call_delta(delta)` → 设置工具调用增量

**`ToolCallDelta` 子结构**：

| 字段 | 说明 |
|------|------|
| `index` | 在 tool_calls 列表中的索引，支持多工具并行调用 |
| `id` | tool_call 的唯一 ID（首次出现时填充） |
| `function_name` | 工具名（首次出现时填充） |
| `arguments_delta` | 参数的增量 JSON 片段（如 `{"pa` → `th":` → `"/tmp"}`） |

### 4.2 `ToolStatusEvent` — 工具执行状态

```python
method = "tool/status"
params = {tool_use_id, tool_name, status, progress, duration_ms, output_summary}
```

**用途**：Orchestrator/ToolExecutor 在工具执行过程中推送状态变更，前端展示进度条或状态标签。

| 参数 | 说明 |
|------|------|
| `tool_use_id` | 工具调用唯一 ID，关联 `TokenChunkEvent.tool_call_delta.id` |
| `tool_name` | 工具名称（如 `read_file`、`pytest`） |
| `status` | 当前状态：`pending`→`running`→`done` 或 `error` |
| `progress` | 进度描述（如 "Reading line 42/100..."），仅 `running` 时有效 |
| `duration_ms` | 执行耗时（毫秒），仅 `done`/`error` 时有效 |
| `output_summary` | 输出摘要（如 "3 tests passed"），仅 `done` 时有效 |

### 4.3 `PermissionRequestEvent` — 权限请求

```python
method = "permission/request"
params = {request_id, tool_name, command_or_action, reason}
```

**用途**：当工具执行需要审批时，Orchestrator 推送此事件。前端展示审批弹窗，用户点击允许/拒绝。

| 参数 | 说明 |
|------|------|
| `request_id` | 审批请求唯一 ID，与 `PermissionResponse.request_id` 关联 |
| `tool_name` | 请求权限的工具名称（如 `shell`） |
| `command_or_action` | 具体的危险操作内容（如 `rm -rf /tmp/test`） |
| `reason` | 模型给出的调用理由，帮助用户决策（如 "用户要求清理临时文件"） |

**关联响应 `PermissionResponse`**：

```python
class PermissionResponse(BaseModel):
    request_id: str    # 关联 PermissionRequestEvent
    approved: bool     # 审批结果：True=允许，False=拒绝
```

> `PermissionResponse` 嵌入 `RPCResponse.result` 中返回，不是独立事件。

### 4.4 `AuditTrailEvent` — 审计事件

```python
method = "audit/trail"
audit: AuditRecord    # 直接引用 core/models/audit.py
```

**用途**：复用 `AuditRecord` 联合类型（`ToolCallAuditEvent` / `CostAuditEvent` / `MemoryChangeAuditEvent` 等），将审计记录通过 RPC 推送。前端或日志系统接收后写入 `audit.jsonl`。

| 字段 | 说明 |
|------|------|
| `audit` | 完整的审计记录，类型由 `AuditEventCategory` 区分 |

**序列化行为**：`model_dump()` 会将 `audit` 展开到 `params` 中，避免嵌套层级过深，方便前端解析。

### 4.5 `SessionLifecycleEvent` — 会话生命周期

```python
method = "session/lifecycle"
params = {event, session_id, project_id, parent_session_id, checkpoint_id, total_tokens, total_cost_usd}
```

**用途**：会话启停、检查点创建/恢复时推送。Gateway 据此管理会话路由表。

| 参数 | 说明 |
|------|------|
| `event` | 事件类型：`started`、`ended`、`checkpoint_created`、`checkpoint_restored`、`resumed` |
| `session_id` | 会话 ID |
| `parent_session_id` | 父会话 ID（子 Agent 场景） |
| `checkpoint_id` | 检查点 ID（`checkpoint_created`/`restored` 时） |
| `total_tokens` | 会话累计 token（`ended`/`checkpoint` 时） |
| `total_cost_usd` | 会话累计成本（`ended`/`checkpoint` 时） |

### 4.6 `SystemNotificationEvent` — 系统通知

```python
method = "system/notification"
params = {level, message, source_module, details}
```

**用途**：向客户端推送非致命错误或状态提示（不含对话流中的消息）。

| 参数 | 说明 |
|------|------|
| `level` | 通知级别：`info`（信息）、`warning`（警告）、`error`（错误） |
| `message` | 通知文本（如 "环境校验未通过：缺少 Git"） |
| `source_module` | 来源模块（如 `core.config_manager`），帮助定位 |
| `details` | 附加详情（如完整的错误堆栈） |

---

## 5. 联合类型

```python
GatewayPayload = Union[TokenChunkEvent, ToolStatusEvent, PermissionRequestEvent,
                       AuditTrailEvent, SessionLifecycleEvent, SystemNotificationEvent,
                       RPCRequest, RPCResponse]

RPCMessage = Union[TokenChunkEvent, ToolStatusEvent, PermissionRequestEvent,
                   AuditTrailEvent, SessionLifecycleEvent, SystemNotificationEvent,
                   RPCRequest, RPCResponse]
```

**反序列化规则**：Pydantic 按 Union 中声明的顺序依次尝试匹配。每个事件类通过 `@model_validator` 校验 `method` 字段，确保 `TokenChunkEvent` 不会误匹配 `method="tool/status"` 的消息。

> ⚠️ **顺序约束**：具体事件类型必须在 `RPCRequest`/`RPCResponse` 之前声明，否则会被泛化的请求类型误匹配。

---

## 6. 与已有模型的关联

| 已有模型 | rpc_schema.py 的关系 |
|----------|---------------------|
| `core/models/session.py` | 会话的 `PermissionRequestEvent` 是**存储层**事件（存入 `ConversationState.events`），RPC 的 `PermissionRequestEvent` 是**传输层**消息（发给用户看）。两者 `request_id` 关联 |
| `core/models/audit.py` | `AuditTrailEvent` 直接引用 `AuditRecord` 联合类型，不重复定义字段 |
| `core/models/hook.py` | 钩子返回值不经过 RPC（钩子是内部责任链，不走网络） |

---

## 7. 典型消息流

```
用户发送 "帮我读 main.py"
  →  CLI 构造 GatewayMessage(payload=RPCRequest(method="user/message", params={content: "帮我读..."}))
  →  Gateway 路由到 Orchestrator
  →  Orchestrator 调 LLMInvoker

LLMInvoker 流式输出
  →  GatewayMessage(payload=TokenChunkEvent→set_delta("正在"))
  →  GatewayMessage(payload=TokenChunkEvent→set_delta("读取"))
  →  GatewayMessage(payload=TokenChunkEvent→set_finish("tool_calls")→set_tool_call_delta(...))

ToolExecutor 执行 read_file
  →  GatewayMessage(payload=ToolStatusEvent(status="running", progress="Reading line 42/100..."))
  →  GatewayMessage(payload=ToolStatusEvent(status="done", output_summary="文件共 100 行"))

Orchestrator 推送结果
  →  GatewayMessage(payload=RPCResponse(id="req_1", result={content: "文件内容：..."}))
```

---

## 8. 设计要点

1. **Gateway 不解析业务内容**：`payload` 是黑盒，Gateway 只看 `GatewayMessage.seq`/`session_id`/`project_id` 做路由。

2. **具体事件优先反序列化**：Union 类型中具体事件排在泛化类型前面，配合 `method` 校验保证正确匹配。

3. **审计复用不重复**：`AuditTrailEvent` 直接引用 `core/models/audit.py` 的 `AuditRecord`，单一定义源。

4. **请求-响应 id 关联**：`RPCRequest.id` 与 `RPCResponse.id` 一一对应，客户端可并发送多个请求。

5. **事件不等待响应**：`RPCEvent` 无 `id`，服务端推送不需要客户端确认，适合高频流式场景。

6. **多模态预留扩展**：`GatewayMessage.payload` 的 union 类型可直接添加 `ImageChunk`/`AudioChunk`（后续版本），不破坏已有消息格式。
