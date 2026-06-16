# 会话状态模型 (`core/models/session.py`)

本文档详细说明会话状态模型中所有数据结构的字段含义、类型、生命周期（何时创建/销毁）、读写角色（谁创建、谁读取、谁修改）及设计考量。该模块是整个 ChaChaAgent 运行时的"记忆中枢"，负责记录 Agent 与用户交互的完整历史。

## 概述

会话状态模型遵循 **"不可变日志 + 可变状态"** 的设计范式：

- **不可变事件日志**：所有用户输入、Agent 回复、工具调用、工具结果等都被记录为不可变事件，一旦创建不可修改。这保证了审计追溯和历史回放的可靠性。
- **可变运行状态**：Agent 循环的运行时状态（如当前迭代次数、等待中的工具调用）是可变的，集中存放在 `AgentLoopState` 中。
- **会话元数据**：记录会话的统计信息（Token 消耗、成本、耗时等），便于成本控制和性能分析。

---

## 1. 基础类型：`Attachment`

```python
class Attachment(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: Literal["image", "audio", "file"] = "image"
    data: bytes
    mime_type: str = "application/octet-stream"
    filename: Optional[str] = None
```

**用途**：表示消息附件的原始二进制数据（如图片、音频文件），为多模态能力预留。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 用户上传文件或 Agent 从工具结果获取图片 | `MessageEvent` 构造者（`orchestrator` 或 `interface` 层） |
| 读取 | 展示给用户、传递给多模态 LLM | `interface/cli`、`interface/web`、`core/model/vision_client.py` |
| 修改 | 不允许修改（设计为不可变） | 无 |
| 销毁 | 会话结束时随 `ConversationState` 一起销毁 | GC 自动回收 |

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 附件唯一标识，自动生成 UUID |
| `type` | `"image" \| "audio" \| "file"` | 附件类型，用于区分不同的媒体格式 |
| `data` | `bytes` | 原始二进制数据（图片像素、音频波形等） |
| `mime_type` | `str` | MIME 类型，如 `image/png`、`audio/mpeg`，用于客户端正确解析 |
| `filename` | `str \| None` | 原始文件名（可选），便于用户识别 |

**设计考量**：目前此模型仅作预留，v1.5+ 将完整支持多模态输入。`data` 字段采用 `bytes` 而非 Base64 字符串，便于与 LLM API 的多模态接口直接对接。

---

## 2. 基础事件：`BaseEvent`

```python
class BaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    source: Literal["user", "agent", "tool", "system"]
```

**用途**：所有事件类型的基类，定义了事件的基本属性。`frozen=True` 保证事件一旦创建不可修改。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 各种事件的具体子类创建时，由对应的模块实例化 | `orchestrator`、`llm_invoker`、`tool_executor`、`context_manager` 等 |
| 读取 | 审计、历史回放、LLM 上下文组装、展示 | `checkpoint_manager`、`context_manager.get_messages_for_llm()`、`interface` 层 |
| 修改 | 不允许修改（`frozen=True`） | 无 |
| 销毁 | 会话结束时 | GC 自动回收 |

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 事件唯一标识，自动生成 UUID |
| `timestamp` | `datetime` | 事件发生时间（UTC 时区），用于排序和审计 |
| `source` | `"user" \| "agent" \| "tool" \| "system"` | 事件来源，区分是谁产生的 |

---

## 3. 具体事件类型

### 3.1 `MessageEvent` — 文本消息

```python
class MessageEvent(BaseEvent):
    role: Literal["user", "assistant", "system"]
    content: str
    attachments: List[Attachment] = Field(default_factory=list)
```

**用途**：记录用户、助手或系统发送的文本消息。`role` 与 LLM API 的 `role` 字段对应，便于直接转换为 LLM 输入。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 用户输入、Agent 流式回复结束、系统提示 | `orchestrator`（用户输入）、`llm_invoker`（助手回复）、`context_manager`（系统消息） |
| 读取 | 生成 LLM 上下文、界面展示、审计 | `context_manager.get_messages_for_llm()`、`interface` 层 |
| 修改 | 不可修改 | 无 |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `role` | `"user" \| "assistant" \| "system"` | 消息角色，与 LLM API 的 `role` 字段对应 |
| `content` | `str` | 消息文本内容 |
| `attachments` | `List[Attachment]` | 附件列表（预留），当前默认空列表 |

### 3.2 `ToolCallEvent` — 工具调用请求

```python
class ToolCallEvent(BaseEvent):
    tool_name: str
    arguments: Dict[str, Any]
    tool_use_id: str
    thought: Optional[str] = None
```

**用途**：记录 Agent 发起的一次工具调用请求，包含工具名称、参数和唯一标识。`tool_use_id` 用于与后续的 `ObservationEvent` 关联。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | LLM 返回 `tool_calls` 后，`llm_invoker` 解析并创建 | `llm_invoker` |
| 读取 | 工具调度器取出执行、LLM 上下文构造（用于后续对话） | `tool_executor`、`context_manager` |
| 修改 | 不可修改 | 无 |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_name` | `str` | 工具名称，如 `read_file`、`run_command` |
| `arguments` | `Dict[str, Any]` | 工具调用的参数（JSON 对象） |
| `tool_use_id` | `str` | 工具调用的唯一标识，用于匹配调用和结果 |
| `thought` | `str \| None` | Agent 调用工具前的思考过程（可选），用于可解释性 |

### 3.3 `ObservationEvent` — 工具执行结果

```python
class ObservationEvent(BaseEvent):
    tool_use_id: str
    content: str
    status: Literal["success", "error"]
    error: Optional[str] = None
    truncated: bool = False
    duration_ms: Optional[int] = None
```

**用途**：记录工具执行后的结果，包括输出内容、状态（成功/失败）、错误信息、是否截断、耗时等。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 工具执行完成后，`tool_executor` 创建 | `tool_executor` |
| 读取 | LLM 上下文构造（将工具结果作为 tool 消息返回）、审计、性能分析 | `context_manager`、`telemetry`、`checkpoint_manager` |
| 修改 | 不可修改 | 无 |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_use_id` | `str` | 对应 `ToolCallEvent` 的 `tool_use_id`，用于匹配 |
| `content` | `str` | 工具执行输出的文本内容 |
| `status` | `"success" \| "error"` | 执行状态 |
| `error` | `str \| None` | 错误信息（仅当 `status="error"` 时有效） |
| `truncated` | `bool` | 输出是否被截断（超过长度限制时标记为 `True`） |
| `duration_ms` | `int \| None` | 工具执行耗时（毫秒），用于性能分析 |

### 3.4 `PermissionRequestEvent` — 权限请求（人工审批）

```python
class PermissionRequestEvent(BaseEvent):
    request_id: str
    tool_name: str
    command_or_action: str
    reason: str
    approved: Optional[bool] = None
```

**用途**：记录需要人工审批的高危操作请求，包含工具名、具体操作内容、模型给出的理由。`approved` 字段初始为 `None`，用户审批后更新为 `True` 或 `False`。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | `policy_engine` 检测到高危操作后，`orchestrator` 创建 | `orchestrator`（或 `policy_engine`） |
| 读取 | 前端（CLI/Web）展示审批面板、用户进行审批 | `interface` 层 |
| 修改 | 用户审批后，`orchestrator` 更新 `approved` 字段 | `orchestrator` |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `request_id` | `str` | 审批请求的唯一标识 |
| `tool_name` | `str` | 请求权限的工具名称 |
| `command_or_action` | `str` | 具体的敏感操作内容，如 `rm -rf .git` |
| `reason` | `str` | 模型给出的调用理由，帮助用户决策 |
| `approved` | `bool \| None` | `None` 表示等待审批，`True`/`False` 表示用户决定 |

### 3.5 `CompactEvent` — 上下文压缩

```python
class CompactEvent(BaseEvent):
    before_token_count: int
    after_token_count: int
    summary: Optional[str] = None
```

**用途**：记录上下文压缩事件，用于追踪压缩前后的 Token 数量变化，以及压缩生成的摘要（可选）。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | `context_compressor` 执行压缩后创建 | `context_compressor` |
| 读取 | 审计、调试、观察压缩效果 | `telemetry`、`checkpoint_manager` |
| 修改 | 不可修改 | 无 |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `before_token_count` | `int` | 压缩前的 Token 数量 |
| `after_token_count` | `int` | 压缩后的 Token 数量 |
| `summary` | `str \| None` | 压缩生成的摘要内容（可选） |

### 3.6 `CheckpointEvent` — 检查点标记

```python
class CheckpointEvent(BaseEvent):
    checkpoint_id: str
    description: Optional[str] = None
```

**用途**：记录会话中创建检查点的事件，便于回溯和恢复。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | `checkpoint_manager` 保存检查点时创建 | `checkpoint_manager` |
| 读取 | 用户浏览检查点列表、恢复会话 | `interface` 层、`checkpoint_manager` |
| 修改 | 不可修改 | 无 |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `checkpoint_id` | `str` | 检查点唯一标识 |
| `description` | `str \| None` | 检查点描述（如"用户手动保存"） |

---

## 4. Agent 运行时状态：`AgentLoopState`

```python
class AgentLoopState(BaseModel):
    model_config = ConfigDict(frozen=False)
    iteration: int = 0
    pending_tool_calls: List[ToolCallEvent] = Field(default_factory=list)
    waiting_for: Optional[Literal["permission", "tool_result"]] = None
    waiting_for_id: Optional[str] = None
    tool_results_cache: Dict[str, ObservationEvent] = Field(default_factory=dict)
```

**用途**：存储 Agent 主循环的运行时状态，该部分数据**可变**，随 Agent 执行动态更新。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 会话初始化时由 `orchestrator` 创建 | `orchestrator` |
| 读取 | 检查当前迭代次数、待执行工具、等待状态 | `orchestrator`、`tool_executor` |
| 修改 | 每次循环迭代时更新 | `orchestrator`（更新 `iteration`、`pending_tool_calls`）、`tool_executor`（缓存结果） |
| 销毁 | 会话结束时 | GC |

| 字段 | 类型 | 说明 |
|------|------|------|
| `iteration` | `int` | 当前循环迭代次数（用于防止无限循环） |
| `pending_tool_calls` | `List[ToolCallEvent]` | 待执行的工具调用队列（支持并发工具调用） |
| `waiting_for` | `"permission" \| "tool_result" \| None` | 当前正在等待的事件类型 |
| `waiting_for_id` | `str \| None` | 对应的等待 ID（`request_id` 或 `tool_use_id`） |
| `tool_results_cache` | `Dict[str, ObservationEvent]` | 工具调用结果缓存，键为 `tool_use_id`，用于去重和快速访问 |

---

## 5. 会话元数据：`SessionMetadata`

```python
class SessionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    parent_session_id: Optional[str] = None
    project_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
```

**用途**：存储会话级别的统计信息和元数据。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 会话初始化时由 `orchestrator` 创建 | `orchestrator` |
| 读取 | 展示会话信息、成本统计、审计 | `interface` 层、`telemetry`、`checkpoint_manager` |
| 修改 | 会话更新时通过 `ConversationState.update_metadata()` 更新 | `orchestrator`、`usage_tracker` |
| 销毁 | 会话结束时归档或销毁 | `checkpoint_manager` |

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话唯一标识，自动生成 UUID |
| `parent_session_id` | `str \| None` | 父会话 ID（用于子 Agent 或会话分支场景） |
| `project_id` | `str` | 关联的项目 ID（与配置中的 `project.id` 对齐） |
| `created_at` | `datetime` | 会话创建时间（UTC 时区） |
| `updated_at` | `datetime` | 会话最后更新时间（UTC 时区） |
| `total_tokens` | `int` | 累计 Token 消耗（输入 + 输出） |
| `total_cost_usd` | `float` | 累计成本（美元） |
| `total_duration_ms` | `int` | 累计耗时（毫秒） |

---

## 6. 会话检查点：`SessionCheckpoint`

```python
class SessionCheckpoint(BaseModel):
    checkpoint_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    description: Optional[str] = None
    event_index: int
    metadata_snapshot: SessionMetadata
    loop_state_snapshot: AgentLoopState
```

**用途**：保存会话在某个时刻的快照，支持历史回溯和断点恢复。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 用户或系统触发保存检查点时，`checkpoint_manager` 创建 | `checkpoint_manager` |
| 读取 | 恢复会话时，`checkpoint_manager` 读取快照数据 | `checkpoint_manager` |
| 修改 | 不可修改 | 无 |
| 销毁 | 检查点过期或被手动删除时 | `checkpoint_manager` |

| 字段 | 类型 | 说明 |
|------|------|------|
| `checkpoint_id` | `str` | 检查点唯一标识 |
| `created_at` | `datetime` | 检查点创建时间 |
| `description` | `str \| None` | 检查点描述 |
| `event_index` | `int` | 对应事件日志的索引位置（从 0 开始） |
| `metadata_snapshot` | `SessionMetadata` | 会话元数据的快照 |
| `loop_state_snapshot` | `AgentLoopState` | Agent 运行时状态的快照 |

---

## 7. 完整会话状态：`ConversationState`

```python
class ConversationState(BaseModel):
    model_config = ConfigDict(frozen=False)
    metadata: SessionMetadata
    events: List[SessionEvent] = Field(default_factory=list)
    loop_state: AgentLoopState = Field(default_factory=AgentLoopState)
    checkpoints: List[SessionCheckpoint] = Field(default_factory=list)
```

**用途**：会话的**唯一可变实体**，集中管理所有会话数据。提供便捷方法操作事件日志和元数据。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 会话初始化时由 `orchestrator` 创建 | `orchestrator` |
| 读取 | 整个会话期间，所有模块通过 `orchestrator` 引用该对象 | `orchestrator`、`llm_invoker`、`tool_executor`、`context_manager`、`checkpoint_manager`、`telemetry`、`interface` 层 |
| 修改 | 通过 `add_event()`、`update_metadata()` 等方法修改 | `orchestrator`、`llm_invoker`、`tool_executor`、`context_manager` |
| 销毁 | 会话结束时 | GC 自动回收 |

| 字段 | 类型 | 说明 |
|------|------|------|
| `metadata` | `SessionMetadata` | 会话元数据 |
| `events` | `List[SessionEvent]` | 不可变事件日志（按时间顺序追加） |
| `loop_state` | `AgentLoopState` | Agent 循环运行时状态 |
| `checkpoints` | `List[SessionCheckpoint]` | 历史检查点列表 |

### 主要方法

| 方法 | 说明 |
|------|------|
| `add_event(event)` | 追加不可变事件到日志，并自动更新 `updated_at` 时间戳 |
| `update_metadata(**kwargs)` | 更新元数据字段（如 `total_tokens`、`total_cost_usd`），自动更新时间戳 |
| `get_messages_for_llm()` | 将事件日志转换为 LLM API（OpenAI/Anthropic 格式）可用的消息列表 |

---

## 8. 设计要点

1. **不可变事件 + 可变状态的分离**：所有事件（`BaseEvent` 及其子类）都是 `frozen=True`，创建后不可修改。这保证了审计日志的完整性和可追溯性。

2. **统一时区**：所有时间戳使用 `timezone.utc`，避免时区混淆问题。

3. **事件索引与检查点恢复**：`SessionCheckpoint.event_index` 指向事件日志中的位置，恢复时只需从该位置重新播放事件即可重建状态。

4. **多模态预留**：`Attachment` 模型为图片、音频等多媒体内容预留了存储空间，`get_messages_for_llm()` 目前仅处理文本，未来可扩展为支持多模态消息。

5. **工具调用并行支持**：`pending_tool_calls` 列表支持同时存放多个待执行工具，为并行工具调用场景提供支持。

6. **成本与性能追踪**：`total_tokens`、`total_cost_usd`、`total_duration_ms` 字段为成本控制和性能优化提供了数据基础。

7. **子 Agent 支持**：`parent_session_id` 字段支持会话继承关系，为子 Agent 任务隔离提供了基础。

## 9. 数据结构生命周期（运行时时间线）

以下描述一个典型会话从创建到结束的过程中，各个数据结构的作用时机。

### 9.1 会话初始化

- **创建 `SessionMetadata`**：在用户启动新会话时（或 CLI/Web 连接建立时），`orchestrator` 创建 `SessionMetadata`，分配 `session_id`、`project_id`，设置 `created_at` 和 `updated_at`。
- **创建 `AgentLoopState`**：同时初始化 `AgentLoopState`，`iteration=0`，`pending_tool_calls=[]`，`waiting_for=None`。
- **创建 `ConversationState`**：将上述实例组合成 `ConversationState`，作为该会话的主控对象，存储于 `orchestrator` 中。

### 9.2 用户输入与 Agent 处理

- **用户消息**：用户输入（CLI 或 Web）到达后，`orchestrator` 构造 `MessageEvent`（role="user"），通过 `add_event()` 追加到 `events` 列表，同时 `updated_at` 自动更新。
- **LLM 调用**：`llm_invoker` 通过 `get_messages_for_llm()` 读取 `events` 转换后的消息列表，作为上下文发送给 LLM。
- **流式输出**：LLM 返回的文本片段逐个通过 `MessageEvent`（role="assistant"）追加到 `events`（每个片段可合并为一个完整事件，或逐块存储，取决于实现粒度，当前设计建议以完整消息为单位）。
- **工具调用**：若 LLM 返回 `tool_calls`，`orchestrator` 解析后创建 `ToolCallEvent` 列表，并更新 `AgentLoopState.pending_tool_calls` 和 `iteration`。然后等待工具执行（或并行执行）。

### 9.3 工具执行

- **工具调用执行**：`tool_executor` 从 `pending_tool_calls` 中取出工具，执行，创建 `ObservationEvent`，通过 `add_event()` 追加。若结果需缓存，存入 `tool_results_cache`。
- **权限请求**：若工具需要审批，`orchestrator` 创建 `PermissionRequestEvent`，`waiting_for="permission"`，`waiting_for_id=request_id`。用户审批后，更新 `approved` 字段，恢复执行（或继续循环）。
- **循环**：`iteration` 递增，重复上述直到无 `pending_tool_calls` 或达到迭代上限。

### 9.4 上下文压缩

- **压缩触发**：当上下文 Token 数超出阈值（由 `context_manager` 监控），触发压缩。`context_compressor` 创建 `CompactEvent`，记录压缩前后的 Token 数，并将摘要存储到 `summary`（可选）。
- **记忆存储**：`memory_manager` 可定期读取 `events` 生成 MEMORY.md，或通过 `Auto Dream` 后台清洗。

### 9.5 检查点保存

- **保存**：用户或系统可手动/自动创建检查点。`checkpoint_manager` 读取当前 `ConversationState`，生成 `SessionCheckpoint`，包含当前 `event_index`（等于当前 `events` 长度），同时保存 `metadata` 和 `loop_state` 的快照。
- **恢复**：通过 `checkpoint_id` 加载检查点，恢复 `events` 到检查点索引（或截断后续事件），恢复 `metadata` 和 `loop_state` 快照，实现状态回滚。

### 9.6 会话结束

- **最终元数据更新**：会话结束时，`orchestrator` 调用 `update_metadata` 更新 `total_tokens`、`total_cost_usd`、`total_duration_ms`（通过 `usage_tracker` 和 `telemetry` 汇总）。
- **归档**：可选将 `ConversationState` 序列化（JSON）存储到 `.chacha_agent/checkpoints/` 作为持久化备份，用于后续审计或恢复。

### 9.7 持久化与恢复

- **序列化**：`ConversationState.model_dump_json()` 可保存为 JSON 文件。反序列化时使用 `ConversationState.model_validate_json()` 恢复内存对象，继续会话。
- **注意事项**：`Attachment` 的 `data` 字段为 `bytes`，JSON 序列化时需 Base64 编码（Pydantic 默认处理），或考虑单独存储。