# 钩子上下文与结果模型 (`core/models/hook.py`)

本文档详细说明钩子系统中所有数据结构的字段含义、类型、读写角色、生命周期及设计考量。该模块定义了钩子责任链的数据契约 —— `HookContext` 是不可变上下文在链中传递，`HookResult` 是每个钩子的纯返回值，禁止副作用。

hook 理念：钩子是纯函数，接收上下文、返回决策、不改全局状态。

## 概述

钩子模型的设计范式：**"不可变上下文 + 纯返回值 + 匹配器筛选"**

- **不可变上下文**：`HookContext` 为 `frozen=True`，钩子链中只读传递，保证并发安全。
- **纯返回值**：`HookResult` 不含任何副作用，`additional_context` 通过编排层注入对话。
- **匹配器筛选**：`HookMatcher` 决定哪些钩子在哪些事件上触发，支持工具名/命令/组合匹配。
- **面向规则引擎**：数据模型与执行逻辑分离，由后续 `hook_orchestrator.py` + `rule_engine.py` 驱动。

---

## 1. 钩子挂载点：`HookPoint`

```python
class HookPoint(str, Enum):
    PRE_TOOL_EXECUTION = "pre_tool_execution"
    POST_TOOL_EXECUTION = "post_tool_execution"
    PRE_LLM_CALL = "pre_llm_call"
    POST_LLM_CALL = "post_llm_call"
    PRE_CONTEXT_ASSEMBLY = "pre_context_assembly"
    POST_CONTEXT_ASSEMBLY = "post_context_assembly"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_END = "on_session_end"
    ON_ERROR = "on_error"
```

**用途**：标记钩子在编排生命周期中的触发位置。`HookOrchestrator` 在每个节点遍历已注册的钩子，匹配后执行。

| 挂载点 | 触发时机 | 典型用途 |
|--------|----------|----------|
| `PRE_TOOL_EXECUTION` | 工具执行之前 | 安全检查、参数修正、审批拦截 |
| `POST_TOOL_EXECUTION` | 工具执行之后 | 结果校验、输出脱敏、审计追加 |
| `PRE_LLM_CALL` | LLM 请求发送前 | Token 预算检查、提示词注入、成本预估 |
| `POST_LLM_CALL` | LLM 响应收到后 | 响应内容过滤、tool_calls 解析校验 |
| `PRE_CONTEXT_ASSEMBLY` | 上下文组装前 | 记忆注入、系统提示调整 |
| `POST_CONTEXT_ASSEMBLY` | 上下文组装后 | 上下文大小验证、压缩触发判断 |
| `ON_SESSION_START` | 会话启动时 | 环境初始化、欢迎语注入 |
| `ON_SESSION_END` | 会话结束时 | 统计汇总、记忆归档 |
| `ON_ERROR` | 异常发生时 | 错误恢复、通知推送、降级处理 |

---

## 2. 钩子决策动作：`HookAction`

```python
class HookAction(str, Enum):
    CONTINUE = "continue"  # 传递到下一个钩子
    STOP = "stop"          # 停止链，继续原始操作
    BLOCK = "block"        # 拒绝当前操作
    MODIFY = "modify"      # 修改数据后继续传递
```

| 动作 | 链行为 | 原始操作是否执行 | `modified_tool_args` 是否有效 |
|------|--------|-----------------|------------------------------|
| `CONTINUE` | 传递到下一个钩子 | 是（待定） | 否 |
| `STOP` | 停止链，不执行后续钩子 | 是 | 否 |
| `BLOCK` | 停止链，拒绝操作 | **否**（被拦截） | 否 |
| `MODIFY` | 修改参数后传递到下一个钩子 | 是（使用修改后的参数） | **是** |

**决策优先级**：`BLOCK` > `MODIFY` > `STOP` > `CONTINUE`。链中第一个返回 `BLOCK` 的钩子终止整个操作。

---

## 3. 钩子匹配器：`HookMatcher`

```python
class HookMatcher(BaseModel):
    type: str                              # always | tool_name | command | composite
    pattern: Optional[str]                 # 正则表达式
    invert: bool                           # 是否取反
    composite_op: Optional[str]            # and | or（仅 composite 时有效）
    children: Optional[List["HookMatcher"]]  # 子匹配器（仅 composite 时有效）
```

**用途**：决定哪些钩子在哪些事件上触发。由 `HookOrchestrator` 在每个挂载点筛选已注册钩子时调用 `matcher.matches()`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `str` | 匹配类型：`always`=始终，`tool_name`=按工具名，`command`=按命令内容，`composite`=组合条件 |
| `pattern` | `str \| None` | 正则表达式，`tool_name` 时匹配工具名，`command` 时匹配命令字符串 |
| `invert` | `bool` | `True` 时取反：匹配 → 不触发，不匹配 → 触发 |
| `composite_op` | `"and" \| "or" \| None` | 组合操作符，`type=composite` 时必填 |
| `children` | `List[HookMatcher] \| None` | 子匹配器列表，`type=composite` 时必填 |

**匹配示例**：

```python
# 始终触发
HookMatcher(type="always")

# 只匹配 read_file 和 write_file 工具
HookMatcher(type="tool_name", pattern="read_file|write_file")

# 除 shell 工具外的所有工具
HookMatcher(type="tool_name", pattern="shell", invert=True)

# 匹配包含 "git push" 的命令
HookMatcher(type="command", pattern="git\\s+push")

# 复合条件：工具名为 shell 且命令包含 pip
HookMatcher(
    type="composite",
    composite_op="and",
    children=[
        HookMatcher(type="tool_name", pattern="shell"),
        HookMatcher(type="command", pattern="pip"),
    ],
)
```

**方法**：
- `matches(tool_name=None, command=None)` → `bool`：判断当前工具/命令是否匹配。无效正则会静默降级为 `False`。

---

## 4. 子上下文结构

为减少 `HookContext` 中的可选嵌套字段，将不同场景的上下文拆分为独立的子结构。

### 4.1 `ToolCallContext` — 工具调用上下文

```python
class ToolCallContext(BaseModel):
    tool_name: str                    # 工具名称
    tool_use_id: str                  # 工具调用唯一 ID
    arguments: Dict[str, Any]         # 参数（可能已脱敏）
    command_or_action: Optional[str]  # 执行的命令字符串
```

**生命周期**：在 `PRE_TOOL_EXECUTION` / `POST_TOOL_EXECUTION` 时由 `HookOrchestrator` 从对应的 `ToolCallEvent` 构建。

| 字段 | 说明 |
|------|------|
| `tool_name` | 工具名称（如 `shell`、`read_file`） |
| `tool_use_id` | 关联 `ToolCallEvent` 的唯一 ID |
| `arguments` | 工具参数，安全钩子可能已对敏感字段脱敏 |
| `command_or_action` | 要执行的命令（shell 工具时），或操作描述（其他工具），供 `command` 类型匹配器使用 |

### 4.2 `LLMRequestContext` — LLM 请求上下文

```python
class LLMRequestContext(BaseModel):
    model_name: str                   # 模型名称
    provider: str                     # 提供商
    messages_count: int               # 消息数量
    estimated_input_tokens: int       # 预估输入 token
```

**生命周期**：在 `PRE_LLM_CALL` / `POST_LLM_CALL` 时由 `HookOrchestrator` 从 `ContextManager` 获取当前状态构建。

| 字段 | 说明 |
|------|------|
| `model_name` | 模型名称（如 `gpt-4`） |
| `provider` | 提供商（`openai`、`anthropic`、`ollama`） |
| `messages_count` | 本次请求携带的消息数量（含历史+工具结果） |
| `estimated_input_tokens` | 预估输入 token 数，用于触发成本/预算检查 |

### 4.3 `ErrorContext` — 错误上下文

```python
class ErrorContext(BaseModel):
    exception_type: str               # 异常类型名
    message: str                      # 异常消息
    source_module: Optional[str]      # 异常来源模块
    recoverable: bool                 # 是否可恢复
```

**生命周期**：在 `ON_ERROR` 时由 `HookOrchestrator` 捕获异常后构建。

| 字段 | 说明 |
|------|------|
| `exception_type` | 异常类型（如 `TimeoutError`、`ValueError`） |
| `message` | 异常消息文本 |
| `source_module` | 异常来源模块（如 `core.llm_invoker`），便于定位 |
| `recoverable` | `True` 表示可尝试恢复（如超时重试），`False` 表示不可恢复（如配置错误） |

---

## 5. 钩子上下文：`HookContext`

```python
class HookContext(BaseModel):
    model_config = ConfigDict(frozen=True, use_enum_values=True)

    id: str                                     # UUID4 唯一标识
    hook_point: HookPoint                       # 当前挂载点
    session_id: Optional[str]                   # 会话 ID
    project_id: Optional[str]                   # 项目 ID
    tool_call: Optional[ToolCallContext]         # 工具上下文
    llm_request: Optional[LLMRequestContext]     # LLM 上下文
    error: Optional[ErrorContext]               # 错误上下文
    matched_by: Optional[HookMatcher]            # 触发匹配器
    metadata: Dict[str, Any]                    # 扩展元数据（只读）
```

**用途**：不可变上下文，在钩子责任链中只读传递。钩子仅能读取字段值，不能修改（`frozen=True`）。

| 字段 | 说明 |
|------|------|
| `id` | 上下文唯一 ID，用于日志关联 |
| `hook_point` | 当前触发位置，钩子据此决定处理逻辑 |
| `session_id` | 当前会话 ID |
| `project_id` | 当前项目 ID |
| `tool_call` | 工具调用信息，`PRE/POST_TOOL_EXECUTION` 时填充 |
| `llm_request` | LLM 请求信息，`PRE/POST_LLM_CALL` 时填充 |
| `error` | 异常信息，`ON_ERROR` 时填充 |
| `matched_by` | 触发此钩子的匹配器，钩子可根据匹配器信息做更精细的判断 |
| `metadata` | 扩展元数据字典，frozen，用于跨钩子传递共享信息（如标记位） |

**字段填充规则**：

| hook_point | tool_call | llm_request | error |
|------------|-----------|-------------|-------|
| `PRE/POST_TOOL_EXECUTION` | ✅ | — | — |
| `PRE/POST_LLM_CALL` | — | ✅ | — |
| `ON_ERROR` | — | — | ✅ |
| 其他 | — | — | — |

---

## 6. 钩子结果：`HookResult`

```python
class HookResult(BaseModel):
    model_config = ConfigDict(frozen=True, use_enum_values=True)

    action: HookAction                          # 决策动作
    message: Optional[str]                      # 决策说明（日志）
    modified_tool_args: Optional[Dict]          # 修改后的参数
    additional_context: Optional[str]           # 注入对话的消息
    metadata: Dict[str, Any]                    # 透传数据
```

**用途**：钩子的纯返回值，不含副作用。`HookOrchestrator` 根据 `action` 决定是否继续执行、是否注入 `additional_context`。

| 字段 | 说明 |
|------|------|
| `action` | 钩子决策（`CONTINUE`/`STOP`/`BLOCK`/`MODIFY`） |
| `message` | 给**开发者/调试**看的决策说明（写入日志，不影响 LLM） |
| `modified_tool_args` | 修改后的工具参数（仅 `MODIFY` 时有效），替换原始 `arguments` |
| `additional_context` | 注入**LLM 对话**的消息文本（如警告、系统提示、上下文补充） |
| `metadata` | 钩子自定义透传数据，后续钩子可读取 |

**`message` vs `additional_context` 的区别**：

| 维度 | `message` | `additional_context` |
|------|-----------|----------------------|
| 受众 | 开发者/调试器 | LLM 模型 |
| 写入位置 | 日志文件 | `ConversationState.events`（MessageEvent） |
| 对 LLM 可见 | ❌ | ✅ |

**便捷方法**：

| 方法 | 返回 | 说明 |
|------|------|------|
| `is_continue()` | `bool` | `action == CONTINUE` |
| `is_blocked()` | `bool` | `action == BLOCK` |
| `is_modified()` | `bool` | `action == MODIFY` |
| `is_stopped()` | `bool` | `action == STOP` |

**工厂方法**：

| 工厂 | 作用 | 示例 |
|------|------|------|
| `continue_(message, additional_context)` | 创建 CONTINUE 结果 | `HookResult.continue_(message="校验通过")` |
| `block(message, additional_context)` | 创建 BLOCK 结果 | `HookResult.block(message="命中黑名单", additional_context="⚠️ 已阻止")` |
| `modify(modified_tool_args, message, additional_context)` | 创建 MODIFY 结果 | `HookResult.modify({"path": "/safe"}, message="路径已修正")` |
| `stop(message)` | 创建 STOP 结果 | `HookResult.stop(message="无需继续检查")` |

---

## 7. 典型使用流程

```
┌──────────────────────────────────────────────────────────┐
│  Orchestrator 准备执行工具 "shell"                          │
│    ↓                                                       │
│  HookOrchestrator.run(PRE_TOOL_EXECUTION, ...)            │
│    │                                                       │
│    ├─ 1. 构建 HookContext                                  │
│    │     hook_point = PRE_TOOL_EXECUTION                   │
│    │     tool_call = ToolCallContext(                      │
│    │       tool_name="shell",                              │
│    │       command_or_action="rm -rf /tmp",                │
│    │     )                                                 │
│    │                                                       │
│    ├─ 2. 筛选匹配的钩子（通过 HookMatcher）                  │
│    │     - 安全钩子: matcher(type="command", pattern="rm")  │
│    │     - 成本钩子: matcher(type="always")                │
│    │                                                       │
│    ├─ 3. 按优先级串行执行                                   │
│    │   安全钩子收到 ctx → 检测到 "rm"                       │
│    │     → 返回 HookResult.block(                          │
│    │         message="命中危险命令",                        │
│    │         additional_context="⚠️ 命令被安全策略拦截"      │
│    │       )                                               │
│    │                                                       │
│    ├─ 4. 处理 BLOCK                                        │
│    │     - 停止链（不执行后续钩子）                          │
│    │     - 将 additional_context 注入 ConversationState    │
│    │     - 插入 PermissionAuditEvent（审计）               │
│    │     - 通知 UI 展示拦截信息                             │
│    │                                                       │
│    └─ 5. Orchestrator 收到 BLOCK → 跳过工具执行              │
└──────────────────────────────────────────────────────────┘
```

---

## 8. 设计要点

1. **纯函数无副作用**：钩子不直接修改 `ConversationState`、不写文件、不发网络请求。所有"影响外部"的操作通过 `HookResult` 的字段声明意图，由 `HookOrchestrator` 统一执行。

2. **匹配器与执行分离**：`HookMatcher` 只负责"是否触发"，具体逻辑在钩子函数中。这让匹配规则可以独立测试和复用。

3. **上下文不可变**：`frozen=True` 保证钩子不会意外修改上下文。如需跨钩子共享信息，使用 `metadata` 字典中的标记位。

4. **降级安全**：无效正则不会抛出异常，`matches()` 静默返回 `False`（不触发），保证不因配置错误阻断主流程。

5. **审批集成**：`additional_context` 在审批场景中可同时注入对话和 UI 层，用户看到警告 → 确认 → LLM 也感知到用户已知风险。

6. **后续扩展点**：`Rule Engine`（`core/rule_engine.py`）将使用 YAML 配置 + `HookMatcher` 实现声明式钩子注册，非开发人员可直接编辑 YAML 文件添加钩子规则。
