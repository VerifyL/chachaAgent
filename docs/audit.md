# 审计日志模型 (`core/models/audit.py`)

本文档详细说明审计日志模型中所有数据结构的字段含义、类型、读写角色、生命周期（何时创建/消费）及设计考量。该模块负责安全合规追踪，与会话事件模型（`session.py`）分离 —— 会话事件驱动对话状态，审计事件专注安全审计。

## 概述

审计模型遵循 **"不可变事件 + JSONL 序列化"** 的设计范式：

- **不可变事件**：所有审计记录均为 `frozen=True`，创建后不可修改，保证审计链的完整性。
- **JSONL 输出**：每条事件通过 `to_jsonl()` 序列化为一行 JSON，直接对接到 `.chacha_agent/logs/audit.jsonl`。
- **敏感信息脱敏**：`SensitiveString` 包装器标记敏感字段，序列化时自动脱敏，原始值仅在内存中存在。
- **事件分类覆盖**：工具调用、记忆变更、成本、权限审批、会话生命周期、模型调用、配置变更、系统事件。

---

## 1. 敏感信息处理：`SensitiveString`

```python
class SensitiveString(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: str                     # 原始敏感值（内存中）
```

**用途**：包装敏感字符串（如 API Key、密码），序列化时自动脱敏，防止泄露到日志文件。

**脱敏规则**：
- ≤4 字符：全部替换为 `[REDACTED]`
- >4 字符：保留首尾各 2 字符，中间替换为 `*`
- 例：`"sk-1234567890abcd"` → `"sk**************cd"`

| 字段 | 类型 | 说明 |
|------|------|------|
| `value` | `str` | 原始敏感值 |
| `masked` | `str` (计算属性) | 脱敏后的值，序列化/打印时自动返回 |

**使用场景**：API Key、Token、Password 等需要写入审计日志但不可明文存储的字段。

---

## 2. 事件分类：`AuditEventCategory`

```python
class AuditEventCategory(str, Enum):
    TOOL_CALL = "tool_call"          # 工具调用
    MEMORY_CHANGE = "memory_change"  # 记忆变更
    COST = "cost"                    # 成本记录
    PERMISSION = "permission"        # 权限审批
    SESSION = "session"              # 会话生命周期
    MODEL_CALL = "model_call"        # 模型调用
    CONFIG_CHANGE = "config_change"  # 配置变更
    SYSTEM = "system"                # 系统事件
```

| 枚举值 | 对应事件类 | 触发时机 |
|--------|-----------|----------|
| `tool_call` | `ToolCallAuditEvent` | 工具执行完成/被拦截 |
| `memory_change` | `MemoryChangeAuditEvent` | 读写删剪枝 MEMORY.md |
| `cost` | `CostAuditEvent` | 每次 LLM 调用后 |
| `permission` | `PermissionAuditEvent` | 审批请求/结果 |
| `session` | `SessionAuditEvent` | 会话启停/检查点 |
| `model_call` | `ModelCallAuditEvent` | 每次 LLM 请求 |
| `config_change` | `AuditEvent` (基类) | 配置热重载 |
| `system` | `AuditEvent` (基类) | 启动/关闭/环境校验 |

---

## 3. 审计事件基类：`AuditEvent`

```python
class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, use_enum_values=True)
    id: str                           # UUID4 唯一标识
    timestamp: datetime               # UTC 时间戳
    category: AuditEventCategory      # 事件分类
    session_id: Optional[str]         # 所属会话
    project_id: Optional[str]         # 所属项目
```

**用途**：所有审计事件的基类，定义公共字段。`frozen=True` 保证不可变性，`use_enum_values=True` 使 `category` 序列化为字符串。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 事件唯一标识，自动生成 UUID4 |
| `timestamp` | `datetime` | 事件发生时间（UTC 时区），用于排序和审计追溯 |
| `category` | `AuditEventCategory` | 事件分类，与具体子类一一对应 |
| `session_id` | `str \| None` | 所属会话 ID（系统级事件可为空） |
| `project_id` | `str \| None` | 所属项目 ID（系统级事件可为空） |

**方法**：
- `to_jsonl()` → `str`：将事件序列化为一行 JSON，供日志写入。

---

## 4. 具体审计事件类型

### 4.1 `ToolCallAuditEvent` — 工具调用审计

```python
class ToolCallAuditEvent(AuditEvent):
    category: "tool_call"            # 固定值
    tool_name: str                   # 工具名称
    tool_use_id: str                 # 工具调用唯一 ID
    arguments_summary: dict          # 参数摘要（已脱敏）
    status: "success"|"error"|"blocked"  # 执行结果
    error_message: str|None          # 错误信息
    duration_ms: int|None            # 耗时（毫秒）
    output_truncated: bool           # 输出是否被截断
    blocked_by_policy: str|None      # 被哪个策略拦截
```

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 工具执行完成或被策略拦截后 | `tool_executor` |
| 读取 | 安全审计、成本分析 | `telemetry`、运维人员 |
| 销毁 | 随日志归档 | 日志轮转 |

| 字段 | 说明 |
|------|------|
| `tool_name` | 工具名称（如 `shell`、`read_file`） |
| `tool_use_id` | 工具调用唯一 ID，关联 `ToolCallEvent`（会话模型） |
| `arguments_summary` | 参数摘要，敏感字段已脱敏（通过 `sanitize_arguments()` 处理） |
| `status` | `success`=成功，`error`=执行失败，`blocked`=被策略拦截 |
| `blocked_by_policy` | 拦截策略名称（如 `command_blacklist`），仅 `blocked` 时有效 |

**静态方法**：
- `sanitize_arguments(args)` → `Dict`：对工具参数脱敏，将 `api_key`/`password`/`token` 等键替换为 `[REDACTED]`，超过 200 字符的值截断。

---

### 4.2 `CostAuditEvent` — 成本审计

```python
class CostAuditEvent(AuditEvent):
    category: "cost"                 # 固定值
    model_name: str                  # 模型名称
    provider: str                    # 提供商
    input_tokens: int                # 输入 token
    output_tokens: int               # 输出 token
    cost_usd: float                  # 本次调用成本
    cumulative_cost_usd: float       # 会话累计成本
    cost_limit_usd: float|None       # 配置上限
    circuit_breaker_triggered: bool  # 是否触发熔断
```

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 每次 LLM 调用返回后 | `usage_tracker` |
| 读取 | 成本监控、熔断判断 | `policy_engine`、仪表盘 |

| 字段 | 说明 |
|------|------|
| `model_name` | 模型名称（如 `gpt-4`、`claude-3-opus`） |
| `input_tokens` / `output_tokens` | 输入/输出 token 数（≥0） |
| `cost_usd` | 本次调用的成本（美元，≥0） |
| `cumulative_cost_usd` | 当前会话累计成本，用于判断是否触发熔断 |
| `cost_limit_usd` | 配置的成本上限（来自 `[policy]` 配置段） |
| `circuit_breaker_triggered` | `True` 表示本次调用导致累计成本超过上限，后续调用将被阻断 |

---

### 4.3 `MemoryChangeAuditEvent` — 记忆变更审计

```python
class MemoryChangeAuditEvent(AuditEvent):
    category: "memory_change"                          # 固定值
    operation: "read"|"write"|"delete"|"prune"|"auto_clean"  # 操作类型
    file_path: str                                     # 变动文件路径
    change_summary: str|None                           # 变更摘要
    lines_before: int|None                             # 变更前行数
    lines_after: int|None                              # 变更后行数
```

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 记忆读写删、Auto Dream 清理时 | `memory_manager` |
| 读取 | 记忆变更追溯 | 运维人员 |

| 字段 | 说明 |
|------|------|
| `operation` | `read`=读取，`write`=写入，`delete`=删除，`prune`=手动剪枝，`auto_clean`=Auto Dream 自动清理 |
| `file_path` | 变动的记忆文件路径（相对于 `.chacha_agent/memory`） |
| `change_summary` | 变更内容摘要，最大 500 字符 |
| `lines_before` / `lines_after` | 变更前后的行数，用于判断是否触发剪枝 |

---

### 4.4 `PermissionAuditEvent` — 权限审批审计

```python
class PermissionAuditEvent(AuditEvent):
    category: "permission"           # 固定值
    request_id: str                  # 审批请求 ID
    tool_name: str                   # 工具名称
    command_or_action: str           # 请求的命令或操作
    reason: str                      # 发起审批的原因
    approved: bool|None              # 审批结果
    cache_hit: bool                  # 是否命中缓存
    cache_ttl_seconds: int|None      # 缓存有效期
```

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 工具执行前触发审批流程时 | `policy_engine` |
| 更新 | 用户审批后 | `orchestrator` → 创建新事件或更新字段 |
| 读取 | 审批行为审计 | 运维人员、合规审查 |

| 字段 | 说明 |
|------|------|
| `request_id` | 审批请求唯一 ID，关联 `PermissionRequestEvent`（会话模型） |
| `command_or_action` | 具体的操作内容（如 `rm -rf /tmp`） |
| `approved` | `None`=待审批，`True`=批准，`False`=拒绝 |
| `cache_hit` | `True` 表示命中审批缓存，无需重复询问用户 |
| `cache_ttl_seconds` | 缓存有效期（秒），仅 `cache_hit=True` 时有效 |

---

### 4.5 `SessionAuditEvent` — 会话审计

```python
class SessionAuditEvent(AuditEvent):
    category: "session"              # 固定值
    event: "started"|"ended"|"checkpoint_created"|"checkpoint_restored"|"resumed"
    parent_session_id: str|None      # 父会话 ID
    checkpoint_id: str|None          # 检查点 ID
    total_tokens_at_event: int|None  # 事件时的累计 token
    total_cost_at_event: float|None  # 事件时的累计成本
    duration_ms_at_event: int|None   # 事件时的累计耗时
```

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 会话生命周期节点 | `orchestrator` |
| 读取 | 会话统计、使用分析 | `telemetry` |

| 字段 | 说明 |
|------|------|
| `event` | `started`=会话开始，`ended`=结束，`checkpoint_created`=创建检查点，`checkpoint_restored`=恢复检查点，`resumed`=恢复会话 |
| `parent_session_id` | 父会话 ID（子 Agent 场景） |
| `total_tokens_at_event` | 事件发生时的累计 token 消耗 |
| `total_cost_at_event` | 事件发生时的累计成本 |
| `duration_ms_at_event` | 事件发生时的累计耗时 |

---

### 4.6 `ModelCallAuditEvent` — 模型调用审计

```python
class ModelCallAuditEvent(AuditEvent):
    category: "model_call"           # 固定值
    model_name: str                  # 模型名称
    provider: str                    # 提供商
    prompt_tokens: int               # prompt token
    completion_tokens: int           # completion token
    latency_ms: int                  # 调用延迟
    retry_count: int                 # 重试次数
    status: "success"|"error"|"retry"|"rate_limited"
```

> **注意**：此事件仅记录调用元数据（token 消耗、延迟、状态），**不记录完整 prompt 和 response**，避免审计日志膨胀和敏感信息泄露。

**生命周期与读写角色**：

| 阶段 | 动作 | 角色 |
|------|------|------|
| 创建 | 每次 LLM 调用完成（含重试）后 | `llm_invoker` |
| 读取 | 调用性能分析、重试率统计、API 可用性监控 | `telemetry` |

| 字段 | 说明 |
|------|------|
| `prompt_tokens` / `completion_tokens` | 本次调用的输入/输出 token 数（不含重试累计） |
| `latency_ms` | 从发送请求到收到完整响应的延迟（毫秒） |
| `retry_count` | 本次调用经历的重试次数（0 表示首次成功） |
| `status` | `success`=成功，`error`=失败，`retry`=正在重试中，`rate_limited`=触发限流 |

---

## 5. 联合类型：`AuditRecord`

```python
AuditRecord = Union[
    ToolCallAuditEvent,
    CostAuditEvent,
    MemoryChangeAuditEvent,
    PermissionAuditEvent,
    SessionAuditEvent,
    ModelCallAuditEvent,
    AuditEvent,  # 含 SYSTEM / CONFIG_CHANGE
]
```

**用途**：统一的审计事件联合类型，用于类型注解和联合反序列化。`TypeAdapter(AuditRecord).validate_python(data)` 会根据 `category` 字段自动匹配正确的子类。

---

## 6. 工厂函数：`audit_factory()`

```python
def audit_factory(
    category: AuditEventCategory,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    **event_fields,
) -> AuditRecord:
```

**用途**：根据 `category` 自动创建对应类型的审计事件，避免手动判断类型。

**使用示例**：
```python
# 无需关心具体类型，工厂自动匹配
evt = audit_factory(
    AuditEventCategory.TOOL_CALL,
    session_id="s1",
    tool_name="read_file",
    tool_use_id="call_1",
    status="success",
)
# → ToolCallAuditEvent
```

---

## 7. JSONL 格式说明

每条审计事件都是一个独立的 JSON 对象，通过 `to_jsonl()` 序列化为一行：

```jsonl
{"id":"uuid-1","timestamp":"2025-01-01T00:00:00Z","category":"session","session_id":"s1","project_id":"p1","event":"started",...}
{"id":"uuid-2","timestamp":"2025-01-01T00:01:00Z","category":"tool_call","session_id":"s1","tool_name":"read_file","status":"success",...}
{"id":"uuid-3","timestamp":"2025-01-01T00:01:05Z","category":"cost","session_id":"s1","model_name":"gpt-4","cost_usd":0.003,...}
```

**JSONL 特点**：
- 每行可独立解析，支持追加写入
- 可使用 `grep`、`jq` 等标准工具进行离线分析
- 与 Prometheus + Loki 等日志系统兼容

---

## 8. 设计要点

1. **与会话事件分离**：`session.py` 的 `SessionEvent` 驱动对话状态，`audit.py` 的 `AuditRecord` 驱动安全审计。两者独立创建、独立存储，互不依赖。

2. **不可变 + UTC**：所有审计事件 `frozen=True`，所有时间戳使用 `timezone.utc`，保证跨时区审计一致性。

3. **脱敏前置**：敏感信息（API Key、密码）在写入审计日志前即完成脱敏，不依赖下游处理。

4. **不记录完整对话**：`ModelCallAuditEvent` 仅记录元数据，不记录 prompt 和 response 原文，避免日志膨胀和隐私泄露。

5. **熔断可见**：`CostAuditEvent.circuit_breaker_triggered` 和 `CostAuditEvent.cumulative_cost_usd` 为成本控制提供了可审计的数据基础。

6. **审批追溯**：`PermissionAuditEvent` 记录每次审批请求的全生命周期（发起→审批→缓存命中），满足合规审查要求。

---

## 9. 典型审计流水线

```
[启动]  → AuditEvent(category=system)
  ↓
[新会话] → SessionAuditEvent(event=started)
  ↓
[LLM 调用] → ModelCallAuditEvent(status=success)
  ↓           CostAuditEvent(cumulative_cost_usd=0.003)
[工具调用] → ToolCallAuditEvent(status=success)
  ↓           (若被拦截 → status=blocked)
[记忆写入] → MemoryChangeAuditEvent(operation=write)
  ↓
[审批]    → PermissionAuditEvent(approved=True, cache_hit=True)
  ↓
... (重复) ...
  ↓
[会话结束] → SessionAuditEvent(event=ended, total_tokens_at_event=5000)
  ↓
[关闭]    → AuditEvent(category=system)
```

所有事件按时间戳追加写入 `audit.jsonl`，可随时通过 `grep` 过滤或 `jq` 分析。
