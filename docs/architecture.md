# ChachaAgent 架构设计文档

> **文档定位**：本文档提供架构全景概述，各模块的详细设计见对应文档（`session.md` / `config.md` / `audit.md` / `hook.md` / `context.md` / `configuration.md`）。
> 
> **当前版本范围**：本文档覆盖阶段 0 已完成的**数据模型层 + RPC 消息模型**。后续阶段（编排引擎、能力插件、协议网关等）的章节在对应模块实现时补充，已在文中标注占位。

---

## 1. 架构概览

```
表现层 (双前端)
  CLI (Textual TUI)  |  Web (FastAPI + React)

网关层
  ChaChaAsyncGateway (JSON-RPC 2.0)

核心编排层 (微内核)
  Orchestrator  →  LLMInvoker  →  ToolExecutor
  ContextManager  →  HookOrchestrator  →  PolicyEngine  →  Telemetry

模型管理层
  OpenAI / Anthropic / Ollama 适配  →  ModelRouter  →  UsageTracker

记忆与上下文子系统
  ├─ StaticRuleLoader           分层加载CHACHA.md（支持@import）
  ├─ MemoryManager              读写MEMORY.md / CHACHA_MEMORY.md / Topics
  ├─ DreamPipeline              项目级记忆整合（每N轮或定时）
  ├─ GlobalDream                用户级跨项目永久记忆整合
  ├─ ContextAssembler           优先级排序组装
  └─ ContextCompressor          混合压缩（修剪 + LLM摘要）

能力与插件层
  内置技能  |  OpenClaw  |  MCP 客户端  |  Code-RAG  |  沙箱执行器
```

---

## 2. 数据模型层（阶段 0 已完成）

> 所有模型均遵循「不可变优先」原则，核心数据结构先行定义，后续模块通过类型注解耦合。

### 2.1 模型关系图

```
ChaChaConfig (config.py)
  │  全局配置：模型、上下文、策略、沙箱、多模态
  │
  ├──→ HookContext → HookResult (hook.py)
  │     钩子系统：不可变上下文 + 纯返回值，责任链模式
  │
  ├──→ AssembledContext (context.py)
  │     上下文组装结果：9 种来源 + 渐进式压缩 + 动态边界
  │
  ├──→ ConversationState (session.py)
  │     会话运行态：不可变事件日志 + AgentLoopState + 检查点
  │
  └──→ AuditRecord (audit.py)
        审计日志：6 种事件类型 + 敏感信息脱敏，JSONL 输出
```

### 2.2 各模型职责

| 模型 | 文件 | 核心职责 | 详细文档 |
|------|------|----------|----------|
| **ChaChaConfig** | `config.py` | 全局配置聚合，TOML 解析 + Pydantic 校验 + 环境变量覆盖 | `docs/configuration.md` |
| **ConversationState** | `session.py` | 会话唯一可变实体，不可变事件日志 + AgentLoopState + 检查点 | `docs/session.md` |
| **AuditRecord** | `audit.py` | 安全/合规审计，6 种事件分类，SensitiveString 脱敏，JSONL 序列化 | `docs/audit.md` |
| **HookContext / HookResult** | `hook.py` | 钩子责任链数据契约，9 个挂载点 + 4 种决策 + HookMatcher 匹配器 | `docs/hook.md` |
| **AssembledContext** | `context.py` | 上下文组装结果，9 种 BlockSource + 5 级渐进压缩 + DYNAMIC_BOUNDARY | `docs/context.md` |
| **RPC 消息模型** | `rpc_schema.py` | JSON-RPC 2.0 通信协议，GatewayMessage 路由包装 + 6 种事件类型 | `docs/rpc_schema.md` |

### 2.3 模型不可变设计

| 模型 | 可变性 | 原因 |
|------|--------|------|
| `ChaChaConfig` | 可变（validate_assignment） | 支持热重载 |
| `ConversationState` | 可变 | 会话运行时的唯一可变聚集点 |
| `BaseEvent` 及子类 | 不可变（frozen=True） | 审计追溯、历史回放 |
| `AuditEvent` 及子类 | 不可变（frozen=True） | 安全合规，不可篡改 |
| `HookContext` | 不可变（frozen=True） | 钩子链中只读传递 |
| `HookResult` | 不可变（frozen=True） | 纯返回值，禁止副作用 |
| `ContextBlock` | 不可变（frozen=True） | 压缩追踪一致性 |
| `AssembledContext` | 不可变（frozen=True） | 组装结果不可篡改 |

---

## 3. 协议与网关层

### 3.1 RPC 消息模型（✅ 已实现）

> 详细文档见 `docs/rpc_schema.md`

ChachaAgent 采用 **JSON-RPC 2.0 规范**作为组件间的统一通信协议。所有消息经 `GatewayMessage` 包装，携带 `seq`（全局自增序列号）、`session_id`、`project_id` 路由信息。

**三层消息结构**：

```
GatewayMessage（路由包装）
  ├─ RPCRequest          —— 客户端→服务端请求（id + method + params）
  ├─ RPCResponse         —— 服务端→客户端响应（id + result/error）
  └─ RPCEvent            —— 服务端单向推送（无 id，不需响应）
       ├─ TokenChunkEvent          stream/token（流式文本 + tool_call 增量）
       ├─ ToolStatusEvent          tool/status（pending→running→done→error）
       ├─ PermissionRequestEvent   permission/request（审批弹窗）
       ├─ AuditTrailEvent          audit/trail（复用 AuditRecord）
       ├─ SessionLifecycleEvent    session/lifecycle
       └─ SystemNotificationEvent  system/notification

PermissionResponse   —— 嵌入 RPCResponse.result
```

**事件与存储模型的区分**：

| 传输层（rpc_schema.py） | 存储层（session.py / audit.py） | 关系 |
|--------------------------|-------------------------------|------|
| `PermissionRequestEvent`（RPC） | `PermissionRequestEvent`（会话） | `request_id` 关联，前者发给用户看，后者存入 `ConversationState.events` |
| `AuditTrailEvent`（RPC） | `AuditRecord`（审计） | 直接引用 `AuditRecord`，不重复定义 |

**多模态预留**：`GatewayMessage.payload` 的 union 类型留有 `ImageChunk` / `AudioChunk` 扩展点（v1.5+）。

### 3.2 异步网关（✅ 已实现）

```python
ChaChaAsyncGateway(max_queue_size=10000, max_history=500, publish_timeout=10.0)
```

| 职责 | 说明 |
|------|------|
| **会话队列** | 每个会话独立 `asyncio.Queue`，慢消费者不阻塞其他会话 |
| **全局 seq** | `asyncio.Lock` 保护自增，保证跨会话有序 |
| **全局监听者** | `on_event(handler)` 注册，Telemetry/Audit 可监听所有事件；handler 以 `create_task` 异步执行，崩溃不扩散 |
| **背压** | 队列满时 `put()` 阻塞等待，`publish_timeout` 超时返回 `False`；`get_backpressure()` 查询 0~1 压力值 |
| **事件历史** | `deque` 保留最近 N 条完整消息，可调上限，`get_event_history()` 查询 |
| **优雅关闭** | `stop()` 向所有队列发送 `None` 哨兵，等待排空后清理 |

**关键方法**：

| 方法 | 说明 |
|------|------|
| `register(sid)` / `unregister(sid)` | 会话生命周期 |
| `publish(payload, session_id)` | 分配 seq → 包装 → 入队 + 全局通知，返回是否成功 |
| `subscribe(sid)` | 返回 `AsyncIterator`，逐条消费；收到 `None` 哨兵时结束 |
| `on_event(handler)` | 注册全局监听者 |
| `get_backpressure(sid?)` | 查询背压比率，不传 sid 返回所有会话最高值 |
| `get_event_history(limit?)` | 查询最近 N 条事件 |
| `list_sessions()` | 列出所有会话及背压状态 |

---

## 4. 钩子系统（阶段 2 部分完成）

> 详细文档见 `docs/hook_orchestrator.md` 和 `docs/hook.md`

### 4.1 数据契约（✅ 已实现）

`core/models/hook.py` 定义了钩子系统的数据模型：
- 9 个 `HookPoint` 挂载点（PRE/POST_TOOL_EXECUTION、PRE/POST_LLM_CALL 等）
- 4 种 `HookAction` 决策（CONTINUE/STOP/BLOCK/MODIFY）
- `HookMatcher` 匹配器（always/tool_name/command/composite）
- 不可变 `HookContext` + 纯 `HookResult`

### 4.2 执行引擎（✅ 已实现）

`core/hook_orchestrator.py` 驱动责任链：

| 特性 | 说明 |
|------|------|
| 责任链顺序 | PRE 正序（高 priority 先），POST 倒序（洋葱语义） |
| BLOCK 短路 | 第一个 BLOCK 终止整个操作 |
| MODIFY 链式覆盖 | 多个钩子修改参数时合并 |
| additional_context 累积 | 所有钩子的上下文消息拼接 |
| 双模式 handler | Python callable + ShellCommand 外部进程（Claude Code 风格） |
| 安全优先容错 | 可能返回 BLOCK 的钩子超时→拒绝，日志钩子→继续 |
| 匹配器筛选 | 工具名/命令/组合条件 |

<!-- TODO: 补充内置钩子实现（security_check / cost_check / compression_hook / path_sanitizer） -->

### 4.3 输出治理（✅ 已实现）

> 详细文档见 `docs/output_governor.md`

`core/output_governor.py` 位于 LLMInvoker 与外部之间，对流式输出进行实时拦截和修复：

| 职责 | 说明 |
|------|------|
| **块类型识别** | 区分 TextBlock（透传）/ ToolUseBlock（缓冲修复）/ ThinkingBlock（透传） |
| **JSON 修复（5级）** | 补括号→截断不完整键→补引号→去尾逗号→兜底错误包装 |
| **修复置信度** | HIGH（补括号）/ MEDIUM（截断）/ LOW（补引号）/ FAILED（不可修复） |
| **LLM 自愈** | `needs_llm_fix=True` 时 Orchestrator 可将残缺 JSON 发回 LLM 修复 |
| **内容过滤** | 正则规则：`block`（拦截）/ `sanitize`（脱敏）/ `warn`（透传警告） |

**流式处理流程**：
```
LLM stream chunk
  │
  ├─ 检测到 "thinking": " → ThinkingBlock → 透传（不缓冲）
  ├─ 检测到 "arguments": " → ToolUseBlock → 缓冲累积
  │     └─ flush() → _repair_json() → FlushResult(output, confidence, needs_llm_fix)
  └─ 其他 → TextBlock → _filter_content() → 透传
```

### 4.4 安全策略引擎（✅ 已实现）

> 详细文档见 `docs/policy_engine.md`

`core/policy_engine.py` 负责工具调用的安全管控，融合了 Claude Code 权限模式与 Harness 加权风险评估：

| 职责 | 说明 |
|------|------|
| **白名单** | 显式放行指定工具，覆盖后续所有检查 |
| **黑名单** | 命令子串匹配，命中即拦截（`rm -rf`、`sudo`、`mkfs` 等） |
| **风险评估** | Harness 加权因子模型：数据敏感度×0.3 + 财务影响×0.25 + 不可逆性×0.2 + 置信度×0.15 + 授权×0.1 |
| **三级权限** | FREE（读文件/cat/grep）→ ASK_FIRST（写文件/patch/chmod）→ APPROVE_ONCE（shell/docker/kubectl） |
| **审批缓存** | SHA256(cache_key) → TTL 秒 → 命中跳过审批，减少重复询问 |
| **成本熔断** | `CostCircuitBreaker` 三态：closed→open→half-open，超限后禁止 LLM 调用 |

**评估流程**：
```
evaluate_tool(tool_name, command, session_id, risk_factors)
  ├─ 1. 白名单检查 → 命中的直接放行
  ├─ 2. 黑名单检查 → 命中的直接拦截（CRITICAL）
  ├─ 3. 权限级别 → FREE→放行 / APPROVE_ONCE→检查任务授权
  ├─ 4. 风险评估 → 加权分数 → RiskLevel(LOW/MEDIUM/HIGH/CRITICAL)
  ├─ 5. 审批缓存 → 命中→跳过 / 未命中→needs_approval=True
  └─ 返回 PolicyDecision(allowed, needs_approval, risk_level, risk_score, cache_key)
```

### 4.5 统一可观测性（✅ 已实现）

> 详细文档见 `docs/telemetry.md`

`core/telemetry.py` 提供结构化日志、指标收集、Span 追踪和 Prometheus 导出，采用"被调用者"模式——其他模块在完成任务后主动记录：

| 组件 | 职责 |
|------|------|
| `StructuredLogger` | 双轨 JSONL：debug.jsonl（5 级过滤）+ audit.jsonl（`AuditRecord`） |
| `MetricsCollector` | counter / gauge / histogram + P50/P99 / Prometheus 文本导出 |
| `AgentMetrics` | 领域记录：LLM 调用 / 工具调用 / 钩子耗时 / 会话统计 / 成本 / 上下文 |
| `Tracer` | 单进程 Span（trace_id 关联 LLM→工具→响应全链路） |

**设计原则**：Telemetry 不主动调用任何其他模块，只被调用。`Gateway.on_event()` 可注册全局监听者实现被动推送。

### 4.6 工具执行器（✅ 已实现）

> 详细文档见 `docs/tool_executor.md`

`core/tool_executor.py` 是薄胶水层，编排已有模块：

```
execute(tool_name, args, session_id)
  ├─ Find → Policy.evaluate → Pre-hooks → Execute(Semaphore+timeout+retry)
  ├─ Truncate → Post-hooks → Telemetry.record_tool_call()
  └─ → ToolResult(status, output, error, duration_ms, truncated)
```

| 特性 | 说明 |
|------|------|
| 并发 | `execute_batch()` + Semaphore 上限 |
| 超时+重试 | 60s 超时，指数退避 2 次 |
| 错误即观察 | 异常包装为 ToolResult，不抛给 LLM |

### 4.7 主控制器（✅ 已实现）

> 详细文档见 `docs/orchestrator.md`

`core/orchestrator.py` 是 Think-Act-Observe 循环的薄胶水层，协调所有子系统：

```
Orchestrator.run(user_input, session_id)
  └─ while iteration < 50:
       ├─ 1. ContextManager.assemble()       → 上下文组装
       ├─ 2. Dispatcher.dispatch()           → LLM + 工具调度循环
       │    ├─ LLMInvoker.invoke(messages, tools=schemas)
       │    ├─ ToolExecutor.execute_batch()
       │    └─ 结果注入 → 继续调用 LLM
       └─ 3. finish_reason=stop → 结束
```

| 终止条件 | 说明 |
|----------|------|
| `finish_reason=stop` 且无 tool_calls | LLM 任务完成 |
| `max_iterations=50` 耗尽 | 强制终止 |
| LLM 认证错误/熔断 | 不可恢复，立即终止 |

### 4.8 待实现模块

| 模块 | 文件 | 阶段 |
|------|------|------|
| ErrorReporter（已跳过） | `core/error_reporter.py` | — |
| **TokenCounter** | `context/token_counter.py` | 📋 阶段 4 |
| **Summarizer** | `context/summarizer.py` | 📋 阶段 4 |
| **Anthropic 适配器** | `model/anthropic_client.py` | 📋 阶段 3 |

---


## 5. 模型管理层

> 详细文档见 `docs/model.md`

### 5.1 LLM 调用器（✅ 已实现）

`core/llm_invoker.py` 位于 Orchestrator 与模型适配器之间，通过最小接口（`AsyncIterator[StreamChunk]`）与适配器解耦：

```
invoke(messages, tools, session_id)
  ├─ PolicyEngine.evaluate_cost()      → 熔断检查
  ├─ model_client.stream()             → StreamChunk 流
  ├─ Gateway.publish(TokenChunkEvent)  → 前端实时推送
  ├─ OutputGovernor.validate_tool_call() → JSON 修复
  ├─ Telemetry.agent.record_llm_call()
  └─ → LLMResponse(text, tool_calls, usage, finish_reason, duration_ms)
```

### 5.2 模型适配器

> 详细文档见 `docs/openai_client.md`、`docs/factory.md`

| 适配器 | 文件 | 支持 API | 状态 |
|--------|------|----------|------|
| **OpenAI 客户端** | `core/llm_clients/openai_client.py` | OpenAI / DeepSeek / Ollama / Qwen | ✅ |
| **工厂** | `core/llm_clients/factory.py` | `ModelProviderConfig` → 客户端 | ✅ |
| **路由器** | `core/llm_clients/router.py` | priority/cost/random + 故障转移 | ✅ |
| **用量追踪器** | `core/llm_clients/usage_tracker.py` | Token/成本累加（调用后精确统计） | ✅ |
| **重试处理器** | `core/llm_clients/retry_handler.py` | 指数退避 + 429 感知 + 认证不重试 | ✅ |
| **Anthropic 客户端** | `core/llm_clients/anthropic_client.py` | Claude | 📋 |

通过 `base_url` 参数，`OpenAIClient` 可兼容任何 OpenAI-compatible API。

```python
# OpenAI
OpenAIClient(api_key="sk-...", model="gpt-4")
# DeepSeek
OpenAIClient(api_key="sk-...", model="deepseek-chat", base_url="https://api.deepseek.com/v1")
# Ollama
OpenAIClient(model="llama3", base_url="http://localhost:11434/v1", api_key="ollama")
```

### 4.1 🔮 多模态扩展预留点

| 扩展点 | 位置 | 说明 | 计划版本 |
|--------|------|------|----------|
| `supports_vision` 属性 | `ModelProviderConfig`（config.py） | 标记模型是否支持视觉输入，`ModelRouter` 按此筛选 | v1.5+ |
| `VisionClient` | `core/llm_clients/vision_client.py` | 视觉模型专用适配器，处理图片编码/多消息结构 | v1.5+ |
| 图片 token 折算 | `UsageTracker`（阶段 3） | 多模态内容按像素/尺寸折合 token，加入成本计算 | v1.5+ |

<!-- TODO: 补充模型管理层架构说明 + VisionClient 接口定义 -->

---

## 6. 记忆与上下文子系统

> **占位** — 阶段 4 实现 `StaticRuleLoader` / `MemoryManager` / `ContextAssembler` / `ContextCompressor` 后补充。

**设计方向**：
- 分层加载 `CHACHA.md`（用户 ~/.chacha/ → 项目/ → 子目录/，支持 @import）
- `MemoryManager`：读写 MEMORY.md + CHACHA_MEMORY.md + Topics，支持 remember/load_memory/write_topic/read_topic
- `DreamPipeline`：项目级后台整合，每 N 轮或定时触发 → 同时输出 MEMORY.md + CHACHA_MEMORY.md
- `GlobalDream`：用户级跨项目整合，DreamPipeline 触发后检查 → 合并为 ~/.chacha/USER_MEMORY.md
- `ContextAssembler`：9 种 BlockSource 并行收集 + priority 排序 + Token 预算硬约束
- `ContextCompressor`：渐进式压缩（FROZEN→TRIMMED→SUMMARIZED→CONSOLIDATED）
- 详细文档见 `docs/memory.md`、`docs/context.md`、`docs/context_manager.md`

### 5.1 🔮 多模态压缩预留点

| 扩展点 | 位置 | 说明 | 计划版本 |
|--------|------|------|----------|
| `multimodal_compression` 策略 | `ContextConfig`（config.py） | 压缩时对多模态内容的处理：`drop`=丢弃 / `describe`=转文字 / `keep`=保留 | v1.5+ |
| 图片→文本降级 | `ContextCompressor`（阶段 4） | 无法传输图片时，将图片 block 转换为文字描述（如「用户上传了一张架构截图」） | v1.5+ |
| 多模态 token 折算 | `TokenCounter`（阶段 4） | 图片按像素折算，音频按秒折算，统一计入 `ContextBlock.token_count` | v1.5+ |

<!-- TODO: 补充上下文子系统架构说明 + 压缩流程图 -->

---

## 7. 能力与插件层

### 工具开发

所有工具继承 `capabilities/base.py` 的 `BaseTool`，定义 name/description/parameters + 实现 execute()。

```python
from capabilities.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "工具描述"
    parameters = {
        "type": "object",
        "properties": {"arg1": {"type": "string"}},
        "required": ["arg1"],
    }
    risk = "low"
    requires_approval = False

    async def execute(self, arg1: str) -> str:
        return f"result: {arg1}"
```

注册到 `ToolExecutor`，自动生成 function calling schema：

```python
executor = ToolExecutor(tools=[MyTool(), ReadFileTool()])
schemas = executor.get_schemas()  # → LLM 可用工具列表
result = await executor.execute("my_tool", {"arg1": "test"}, session_id)
```

---

> 内置工具已实现，详见 `docs/builtin_tools.md`。
> 记忆系统详见 `docs/memory.md`。
> CLI 使用详见 `docs/cli.md`。
> 子Agent 系统详见 `docs/subagent.md`。

**设计方向**：
- `UnifiedTool` 统一工具基类契约
- 三轨工具总线：内置技能 + OpenClaw 技能 + MCP 协议工具
- Code-RAG 引擎：LanceDB 语义检索 + Tree-sitter 符号图
- `capabilities/multimodal/` 目录已创建，预留给 v1.5+ 的截图识别、语音转文字等工具

### 5.7 子Agent 系统

> 详细文档见 `docs/subagent.md`

`core/subagent/spawner.py` 派生子Agent 执行独立任务，参考 Claude Code sub-agent：

| 类型 | 用途 | 工具 | LLM 自决 |
|------|------|------|---------|
| `explore` | 代码库搜索 | read_file, grep | ✅ |
| `plan` | 规划设计 | read_file, grep, load_memory | ✅ |
| `worker` | 执行修改 | read_file, grep, edit_file | ✅ |

注册为 `subagent` 内置工具，LLM 根据任务复杂度自动判断是否委托。子Agent 独立上下文、工具白名单限制、超时控制。

```python
spawner = SubAgentSpawner(llm, parent_tools)
result = await spawner.spawn("explore", "梳理项目架构")
```

## 8. 表现层

> **占位** — 阶段 7/8 实现 CLI 和 Web 前端后补充。

**设计方向**：
- CLI：Textual TUI（消息滚动 / PTY 终端 / 审批弹窗 / Tab 自动补全）
- Web：FastAPI + WebSocket 实时推送 + React 前端
- Web 端原生渲染富媒体（多模态预留），CLI 降级显示 URL 或文本描述

<!-- TODO: 补充表现层架构说明 -->

---

## 9. 多模态扩展预留总览

| 层面 | 预留项 | 当前状态 | 计划版本 |
|------|--------|----------|----------|
| **协议层** | `GatewayMessage.payload` 承载 `ImageChunk` / `AudioChunk` | 已在 `rpc_schema.py` 中标注 union 扩展点 | v1.5+ |
| **模型层** | `supports_vision` / `VisionClient` / 多模态 token 折算 | 配置段已定义（`MultimodalConfig`） | v1.5+ |
| **上下文层** | `multimodal_compression` 策略 / 图片→文本降级 | 压缩模型已预留字段 | v1.5+ |
| **能力层** | `capabilities/multimodal/` 目录（截图识别、语音转文字、图像生成） | `__init__.py` 已创建 | v1.5+ |
| **安全层** | 图片元数据校验（尺寸、格式、来源），防提示词注入 | 尚未实现 | v1.5+ |
| **表现层** | Web 原生富媒体渲染 / CLI 降级文字 | 尚未实现 | v1.5+ |

---

## 10. 测试与质量保障

> **占位** — 阶段 9 作为独立里程碑补充。

**当前覆盖**（阶段 0）：
- 单元测试：
  `environment_validator`（4） ·
  `config`（18） ·
  `config_manager`（20） ·
  `session`（22） ·
  `audit`（55） ·
  `hook`（48） ·
  `context`（36） ·
  `rpc_schema`（47）
- 集成测试：`test_config_integration.py`（2）

<!-- TODO: 补充测试架构、基准测试、模糊测试、行为评测设计 -->
