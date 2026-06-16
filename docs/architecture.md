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
  StaticRuleLoader  →  MemoryManager  →  ContextAssembler  →  ContextCompressor

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

---

## 5. 模型管理层（预留）

> **占位** — 阶段 3 实现 LLM 客户端适配后补充。

**设计方向**：
- 抽象基类 `BaseLLMClient` + 各厂商适配（OpenAI / Anthropic / Ollama）
- `ModelFactory` + `ModelRouter`（优先级/成本/随机策略）
- `UsageTracker`（Token 计数 + 成本熔断）+ `RetryHandler`（指数退避）
- 详细文档见 `docs/model.md`（尚未创建）

### 4.1 🔮 多模态扩展预留点

| 扩展点 | 位置 | 说明 | 计划版本 |
|--------|------|------|----------|
| `supports_vision` 属性 | `ModelProviderConfig`（config.py） | 标记模型是否支持视觉输入，`ModelRouter` 按此筛选 | v1.5+ |
| `VisionClient` | `core/model/vision_client.py` | 视觉模型专用适配器，处理图片编码/多消息结构 | v1.5+ |
| 图片 token 折算 | `UsageTracker`（阶段 3） | 多模态内容按像素/尺寸折合 token，加入成本计算 | v1.5+ |

<!-- TODO: 补充模型管理层架构说明 + VisionClient 接口定义 -->

---

## 6. 记忆与上下文子系统

> **占位** — 阶段 4 实现 `StaticRuleLoader` / `MemoryManager` / `ContextAssembler` / `ContextCompressor` 后补充。

**设计方向**：
- 分层加载 `CHACHA.md`（用户 ~/.chacha/ → 项目/ → 子目录/，支持 @import）
- `MemoryManager`：读写 MEMORY.md + autoDream 后台清洗
- `ContextAssembler`：9 种 BlockSource 并行收集 + priority 排序 + Token 预算硬约束
- `ContextCompressor`：渐进式压缩（FROZEN→TRIMMED→SUMMARIZED→CONSOLIDATED）
- 详细文档见 `docs/context.md`

### 5.1 🔮 多模态压缩预留点

| 扩展点 | 位置 | 说明 | 计划版本 |
|--------|------|------|----------|
| `multimodal_compression` 策略 | `ContextConfig`（config.py） | 压缩时对多模态内容的处理：`drop`=丢弃 / `describe`=转文字 / `keep`=保留 | v1.5+ |
| 图片→文本降级 | `ContextCompressor`（阶段 4） | 无法传输图片时，将图片 block 转换为文字描述（如「用户上传了一张架构截图」） | v1.5+ |
| 多模态 token 折算 | `TokenCounter`（阶段 4） | 图片按像素折算，音频按秒折算，统一计入 `ContextBlock.token_count` | v1.5+ |

<!-- TODO: 补充上下文子系统架构说明 + 压缩流程图 -->

---

## 7. 能力与插件层

> **占位** — 阶段 5 实现 `UnifiedTool` / `sandbox.py` / `mcp_client.py` / Code-RAG / 内置技能后补充。

**设计方向**：
- `UnifiedTool` 统一工具基类契约
- 三轨工具总线：内置技能 + OpenClaw 技能 + MCP 协议工具
- Code-RAG 引擎：LanceDB 语义检索 + Tree-sitter 符号图
- `capabilities/multimodal/` 目录已创建，预留给 v1.5+ 的截图识别、语音转文字等工具
- 详细文档见 `docs/architecture.md`（后续补充）

<!-- TODO: 补充能力与插件层架构说明 -->

---

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
