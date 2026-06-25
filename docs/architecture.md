# ChachaAgent 架构设计文档

> **当前版本 v3.1.4** — 架构统一：`Orchestrator.run_stream()` 成为唯一编排入口（13 步流水线），
> Dispatcher 工具执行并发化，ChatEngine 降级为消息存储层。
> 各模块详细文档：session.md | config.md | audit.md | hook.md | context.md | configuration.md

---

## 1. 架构全景

```
interface/                        表现层
  ├── cli/app.py          ✅      prompt_toolkit + Rich 终端 (v0.2)
  ├── cli/agent_bridge.py ✅      CLI ↔ 核心桥接层
  └── web/                🚧      FastAPI + React (仅 __init__.py 占位)

protocol/                         网关与协议层
  ├── gateway.py          ✅      ChaChaAsyncGateway JSON-RPC 2.0 消息总线
  └── rpc_schema.py       ✅      JSON-RPC 2.0 消息模型 (6 种事件类型)

core/                             核心编排层 (微内核控制平面)
  ├── orchestrator.py     ✅      编排主入口 (run_stream 13步流水线)
  ├── chat_engine.py      ✅      消息存储 + 检查点持久化 (v2.1 降级)
  ├── dispatcher.py       ✅      LLM ↔ 工具桥接调度器 (v2.1 并发 + Circuit Breaker)
  ├── llm_invoker.py      ✅      流式 LLM 调用器 (StreamChunk 接口)
  ├── tool_executor.py    ✅      工具执行调度器 (审批 + 钩子 + 重试)
  ├── context_manager.py  ✅      上下文组装 (双区模型 protected/dynamic)
  ├── policy_engine.py    ✅      安全策略引擎 (风险评估 + 成本熔断)
  ├── hook_orchestrator.py ✅     钩子责任链引擎 (Python/外部进程双模式)
  ├── output_governor.py  ✅      流式 JSON 修复 + 非法内容拦截
  ├── rule_engine.py      ✅      YAML 声明式规则引擎
  ├── telemetry.py        ✅      统一可观测性 (日志/指标/Span/Prometheus)
  ├── config_manager.py   ✅      配置加载 + 热重载 + 环境变量覆盖
  ├── checkpoint_manager.py ✅    会话检查点 (保存/恢复/清理)
  ├── session_service.py  ✅      会话编排服务 (生命周期/Dream/审计)
  ├── project_init.py     ✅      项目初始化器
  ├── cli_theme.py        ✅      CLI 主题配置
  ├── environment_validator.py ✅ 环境校验
  ├── llm_clients/               LLM 客户端适配器
  │   ├── openai_client.py ✅    OpenAI/DeepSeek 兼容流式适配器
  │   ├── retry_handler.py ✅    指数退避重试
  │   ├── factory.py       ✅   模型工厂 (OpenAI/DeepSeek/Ollama)
  │   ├── router.py        ✅   模型路由 (priority/cost/random + 故障标记)
  │   └── usage_tracker.py ✅   用量追踪 (per-model/per-session)
  ├── context/                   记忆与上下文子系统
  │   ├── memory_manager.py    ✅  记忆文件 I/O (每日/永久/Topic/Session)
  │   ├── context_compressor.py ✅  混合压缩 (FROZEN→TRIMMED→SUMMARIZED→CONSOLIDATED)
  │   ├── dream.py            ✅  DreamPipeline 项目级记忆整合
  │   ├── global_dream.py     ✅  GlobalDream 跨项目永久记忆整合
  │   ├── summarizer.py       ✅  LLM 摘要压缩
  │   ├── token_counter.py    ✅  Token 估算
  │   └── static_rule_loader.py ✅ CHACHA.md 分层加载
  ├── subagent/                  子Agent 系统
  │   ├── spawner.py         ✅  孵化器
  │   └── definitions.py     ✅  类型定义 (explore/plan/worker)
  ├── models/                    Pydantic 数据模型
  │   ├── config.py          ✅  全配置模型 (含多模态预留)
  │   ├── context.py         ✅  上下文模型 (9 BlockSource + 5 级压缩)
  │   ├── session.py         ✅  会话模型 (ConversationState + 事件日志)
  │   ├── hook.py            ✅  钩子模型 (9 HookPoint + 4 HookAction)
  │   └── audit.py           ✅  审计模型 (6 种事件 + SensitiveString 脱敏)
  └── debug/               🚧  调试工具 (仅 __init__.py)

capabilities/                     能力与插件层
  ├── base.py             ✅     BaseTool 抽象基类
  ├── registry.py         ✅     工具注册表 (build_tools 单一来源)
  ├── sandbox.py          ✅     沙箱执行器 (subprocess + 环境白名单 + 资源限制)
  ├── atomic_writer.py    ✅     原子写入工具
  ├── mcp_client.py       🚧    MCP 客户端 (骨架, 缺 stdio 通信)
  ├── plugin_installer.py 🚧    插件安装器 (骨架)
  ├── openclaw_loader.py  🚧    OpenClaw 加载器 (骨架)
  ├── builtins/                 内置工具 (全部 ✅)
  │   ├── chunk_streamer.py    read_file / read_files (流式读取)
  │   ├── code_patcher.py      edit_file (精确替换)
  │   ├── code_intel.py        跨文件语义分析 (AST)
  │   ├── depe_analyzer.py     依赖分析 (imports/exports/graph)
  │   ├── file_outline.py      文件骨架提取
  │   ├── list_files.py        目录列表 (glob/深度/git状态)
  │   ├── project_overview.py  项目总览 (README+结构)
  │   ├── git_tools.py         git_diff / git_log / git_status
  │   ├── git_context.py       Git 上下文钩子
  │   ├── memory_tool.py       load_memory / write_topic / read_topic
  │   ├── subagent_tool.py     子Agent 调度工具
  │   └── http_tool.py         HTTP 请求工具
  ├── multimodal/          🚧   多模态 (仅 __init__.py)
  └── rag/                 🚧   Code-RAG (symbol_parser/vector_store 骨架)
```

> ✅ = 已实现　🚧 = 占位/骨架，待实现

---

## 2. 数据模型层

所有模型基于 **Pydantic v2**，遵循「不可变优先」原则。

### 2.1 模型关系

```
ChaChaConfig (config.py)
  ├── ModelConfig → ModelProviderConfig (多提供商)
  ├── ContextConfig (Token 预算/压缩参数)
  ├── PolicyConfig (黑名单/成本上限/审批 TTL)
  ├── SandboxConfig (命令白名单/超时)
  ├── TelemetryConfig (日志/审计/Prometheus)
  ├── MultimodalConfig (预留, v1.5+)
  ├── MemoryConfig (记忆根目录/清理间隔)
  └── InterfaceConfig (CLI 主题/Web 端口, Web 部分预留)

HookContext → HookResult (hook.py)
  - 不可变上下文 + 纯返回值，责任链模式

AssembledContext → ContextBlock (context.py)
  - 双区模型 (protected/dynamic)，9 BlockSource，5 级压缩

ConversationState (session.py)
  - 会话唯一可变实体，不可变事件日志 + AgentLoopState + 检查点

AuditRecord (audit.py)
  - 6 种事件类型 + SensitiveString 脱敏，JSONL 输出
```

### 2.2 不可变设计

| 模型 | 可变性 | 原因 |
|------|--------|------|
| ChaChaConfig | 可变 (validate_assignment) | 支持热重载 |
| ConversationState | 可变 | 会话运行时唯一可变聚集点 |
| BaseEvent 及子类 | 不可变 (frozen=True) | 审计追溯、历史回放 |
| HookContext / HookResult | 不可变 (frozen=True) | 钩子链只读传递 |
| ContextBlock / AssembledContext | 不可变 (frozen=True) | 压缩追踪一致性 |
| AuditRecord / AuditEvent | 不可变 (frozen=True) | 安全合规 |

---

## 3. 协议与网关层

### 3.1 JSON-RPC 2.0

ChachaAgent 采用 JSON-RPC 2.0 作为组件间统一通信协议。GatewayMessage 包装所有消息：

```
GatewayMessage (路由包装)
  ├─ seq: int                全局自增序列号
  ├─ session_id / project_id 路由信息
  └─ payload: Union[
       RPCRequest,      # 客户端→服务端 (id + method + params)
       RPCResponse,     # 服务端→客户端 (id + result/error)
       RPCEvent,        # 服务端单向推送 (无 id)
     ]

RPCEvent 子类型 (6 种):
  TokenChunkEvent          stream/token         流式文本 + tool_call 增量
  ToolStatusEvent          tool/status          工具状态 (pending→done→error)
  PermissionRequestEvent   permission/request   审批弹窗
  AuditTrailEvent          audit/trail          复用 AuditRecord
  SessionLifecycleEvent    session/lifecycle    会话启停/检查点
  SystemNotificationEvent  system/notification  系统通知
```

### 3.2 ChaChaAsyncGateway

异步消息总线，实现各组件解耦：

| 特性 | 说明 |
|------|------|
| 会话队列 | 每会话独立 asyncio.Queue，慢消费者不阻塞 |
| 全局 seq | asyncio.Lock 保护自增，跨会话有序 |
| 全局监听者 | on_event() 注册，Telemetry/Audit 订阅所有事件 |
| 背压控制 | 队列满时阻塞等待，publish_timeout 超时返回 False |
| 事件历史 | deque 保留最近 N 条，get_event_history() 查询 |
| 优雅关闭 | stop() 发送 None 哨兵，等待队列排空 |

---

## 4. 核心编排层

### 4.1 Orchestrator — 主循环（v2.1 统一编排入口）

**`Orchestrator.run_stream()`** 是所有对话的唯一生产路径，直接编排 Dispatcher，不再委托 ChatEngine。

```
Orchestrator.run_stream(user_input, session_id)
  ├─ 1. ConversationState 初始化 + 消息追加
  ├─ 2. Hook: PRE_CONTEXT_ASSEMBLY           ← Git 上下文注入等
  ├─ 3. Policy 检查                           ← 速率/权限拦截
  ├─ 4. Gateway: session_started
  ├─ 5. ContextManager.assemble()             ← 双区模型 + MEMORY.md
  ├─ 6. Dispatcher.dispatch_stream()          ← 直接调用（并发工具执行）
  │     ├─ LLMInvoker.stream()
  │     ├─ asyncio.gather(*tool_calls)        ← 同轮独立工具并发
  │     └─ Circuit Breaker 按序检查
  ├─ 7. ContextCompressor.auto_compact()       ← Token 压力触发
  ├─ 8. 上下文利用率遥测
  ├─ 9. 最终回答提取                           ← DeepSeek think 兼容
  ├─ 10. save_checkpoint()
  ├─ 11. _save_round_memory()
  ├─ 12. Gateway: session_ended
  └─ 13. 清理 + DreamPipeline 触发

Orchestrator.run_stream() 为唯一编排入口，返回 `AsyncIterator[StreamEvent]`。
```

### 4.2 ChatEngine — 对话引擎（v2.1 降级为存储层）

ChatEngine 不再参与运行时调度，专注消息存储 + 检查点持久化：

- `send_message()` → 简化版，直接委托 `Dispatcher.dispatch_stream()`，不再做上下文组装/自动压缩
- `set_checkpoint_dir()` 注入会话目录，自动恢复/保存
- `infer_context_window()` 根据模型名推断上下文窗口 (DeepSeek/Gemini=1M, Claude=200K, GPT/LLaMA=128K)
- 检查点恢复：优先 CheckpointManager 格式，回退旧 checkpoint.json
- 上下文组装/自动压缩/最终回答提取/遥测 全部迁入 `Orchestrator.run_stream()`

### 4.3 Dispatcher — LLM ↔ 工具桥接（v2.1 并发）

桥接 ToolExecutor 和 LLMInvoker，驱动工具调用循环：

```
dispatch_stream(messages, session_id)
  └─ while rounds < 200:
       ├─ LLMInvoker.stream() → 收集 text + tool_calls
       ├─ 无 tool_calls → yield done → 结束
       ├─ 构造 assistant 消息 (含 tool_calls)
       ├─ 工具执行三阶段:
       │    Phase 1: 遍历 → tool_exec_start → 收集 tasks
       │    Phase 2: asyncio.gather(*tasks, return_exceptions=True)  ← 并发
       │    Phase 3: 按序处理结果 → Circuit Breaker 按序累加 → yield
       └─ _freeze_old_tool_results(): 保留最近 8 个完整工具结果,
            更早的替换为 JSON 占位符 {toolname, result_summary, cache_path}
```

### 4.4 LLMInvoker — 流式 LLM 调用器

通过最小接口 `AsyncIterator[StreamChunk]` 与模型适配器解耦：

```
invoke(messages, tools, session_id)
  ├─ PolicyEngine.evaluate_cost()      成本熔断检查
  ├─ model_client.stream()             StreamChunk 流
  ├─ Gateway.publish(TokenChunkEvent)  前端实时推送
  ├─ OutputGovernor.validate_tool_call() JSON 修复
  ├─ RetryHandler (如果注入)           指数退避重试
  ├─ Telemetry.record_llm_call()       遥测记录
  └─ → LLMResponse(text, tool_calls, usage, duration_ms)
```

异常映射: 429→RateLimited | 401/403→Authentication (API Key 遮罩) | Timeout→超时 | Connection→连接错误

### 4.5 ToolExecutor — 工具执行调度器

编排 PolicyEngine + HookOrchestrator + 并发 + 重试：

```
execute(tool_name, args, session_id)
  ├─ Find tool → PolicyEngine.evaluate_tool()
  │    ├─ 黑名单拦截
  │    ├─ 权限级别 (FREE/ASK_FIRST/APPROVE_ONCE)
  │    └─ 风险评估 (加权因子模型)
  ├─ 审批: needs_approval → approval_handler() → 通过/拒绝
  ├─ Pre-hooks (记忆工具豁免)
  ├─ Execute: Semaphore(5) + asyncio.wait_for(60s) + 指数退避(2次)
  │    可重试: TimeoutError/ConnectionError/OSError
  │    不重试: ValueError/TypeError/PermissionError/FileNotFoundError
  ├─ Truncate: 超 100K 字符截断
  ├─ Post-hooks
  ├─ Telemetry.record_tool_call()
  └─ → ToolResult(status, output, error, duration_ms, truncated)
```

并发: `execute_batch()` 用 asyncio.gather 并发执行多个工具。

### 4.6 PolicyEngine — 安全策略引擎

融合 Claude Code 权限模式 + Harness 加权风险评估：

| 机制 | 说明 |
|------|------|
| 黑名单 | 命令子串匹配，命中直接 CRITICAL 拦截 |
| 白名单 | 显式放行，覆盖后续检查 |
| 三级权限 | FREE(只读/记忆) / ASK_FIRST(bash/edit) / APPROVE_ONCE(任务级) |
| 风险评估 | 加权因子: 数据敏感度×0.3 + 财务×0.25 + 不可逆×0.2 + 置信度×0.15 + 授权×0.1 |
| 审批缓存 | SHA256(cache_key) → TTL 秒 → 缓存命中跳过 |
| 成本熔断 | CostCircuitBreaker: closed→open→half-open 三态 |
| 工具预设 | MEMORY_TOOLS(完全跳过) / READONLY_TOOLS(FREE) / SYSTEM_TOOLS(ASK_FIRST+HIGH) / EDIT_TOOLS(ASK_FIRST) |

### 4.7 HookOrchestrator — 钩子责任链

双模式 handler：Python callable + ShellCommand (Claude Code 风格 stdin/stdout JSON)：

- PRE 正序 (高 priority 先)，POST 倒序 (洋葱语义)
- BLOCK 短路 → 终止整个操作
- MODIFY 链式覆盖参数
- additional_context 跨钩子拼接
- 安全钩子超时→默认拒绝，日志钩子→容错继续

### 4.8 OutputGovernor — 流式输出治理

LLMInvoker 与外部之间的流式处理：

| 职责 | 说明 |
|------|------|
| 块类型识别 | TextBlock(透传) / ToolUseBlock(缓冲) / ThinkingBlock(透传) |
| JSON 修复 (5 级) | 合法→补括号→截断→补引号→去尾逗号→兜底 |
| 修复置信度 | HIGH/MEDIUM/LOW/FAILED，LOW/FAILED 触发 LLM 自愈 |
| 内容过滤 | block(拦截) / sanitize(脱敏) / warn(透传警告) |

### 4.9 Telemetry — 统一可观测性

| 组件 | 职责 |
|------|------|
| StructuredLogger | 双轨 JSONL: debug.jsonl(5级过滤) + audit.jsonl(AuditRecord) |
| MetricsCollector | counter/gauge/histogram + P50/P99 + Prometheus 导出 |
| AgentMetrics | LLM/工具/钩子/会话/成本/上下文 领域指标 |
| Tracer | 单进程 Span (trace_id 关联全链路) |

---

## 5. 模型客户端层

### 5.1 OpenAIClient

`core/llm_clients/openai_client.py` — 实现 `async stream(messages, tools) → AsyncIterator[StreamChunk]`

兼容任何 OpenAI-compatible API (DeepSeek / Ollama / Qwen / 自定义代理)：

```python
# OpenAI
OpenAIClient(api_key=sk-..., model=gpt-4)
# DeepSeek (含 reasoning_content 支持)
OpenAIClient(api_key=sk-..., model=deepseek-chat, base_url=https://api.deepseek.com/v1)
# Ollama
OpenAIClient(model=llama3, base_url=http://localhost:11434/v1, api_key=ollama)
```

**StreamChunk** 是 Pydantic discriminated union（7 个子类：TextChunk / ReasoningChunk / ToolCallStartChunk / ToolCallDeltaChunk / ToolCallEndChunk / DoneChunk / ErrorChunk），消费方用 `isinstance()` 匹配

### 5.2 RetryHandler

`core/llm_clients/retry_handler.py` — 指数退避重试，429 感知，认证错误不重试。

### 5.3 占位模块 🚧

| 模块 | 文件 | 当前状态 |
|------|------|---------|
| ModelFactory | factory.py | 骨架，待实现 Provider → Client 映射 |
| ModelRouter | router.py | 骨架，待实现 priority/cost/random 策略 |
| UsageTracker | usage_tracker.py | 骨架，待实现精确 Token 成本计算 |
| Anthropic Client | 未创建 | 待实现 |
| Ollama Client | 未创建 | 待实现 |

---

## 6. 记忆与上下文子系统

### 6.1 MemoryManager — 记忆文件 I/O

文件结构 (v2.1):

```
~/.chacha/projects/{project_id}/
  CHACHA_MEMORY.md              ← 永久记忆 (保护区，无条目上限，永不删除)
  memory/
    sessions/{session_id}/
      MEMORY.md                 ← 该 session 的 DreamPipeline 产物
      {YYYY-MM-DD}.md           ← 每日对话记忆 (7 天老化)
      topics/                   ← 主题记忆 (5 主题 × N 条目)
        user-preferences.md
        project-decisions.md
        lessons-learned.md
        errors-fixed.md
        project-progress.md
      tool_cache/               ← 工具结果缓存 (会话结束自动清理)
```

核心操作：`remember()` | `read()` | `list_days()` | `read_day()` | `read_recent_days()` | `read_permanent_memory()` | `cleanup_tool_cache()`

### 6.2 DreamPipeline — 项目级记忆整合

会话结束后异步运行，不阻塞对话：

1. 收集最近 7 天所有 session 每日文件
2. 读取当前 MEMORY.md + CHACHA_MEMORY.md
3. 1 次 LLM 调用 → 同时输出更新后的 MEMORY.md + CHACHA_MEMORY.md
4. Prune: 删除超过 7 天的旧每日文件

触发条件 (二选一)：累计 N 次会话 | 距上次运行超过 N 小时

### 6.3 GlobalDream — 跨项目永久记忆整合

用户级，扫描 `~/.chacha/projects/` 下所有项目，提取通用规律写入 `~/.chacha/USER_MEMORY.md`。

### 6.4 ContextCompressor — 上下文压缩

4 级渐进压缩策略 (FROZEN → TRIMMED → SUMMARIZED → CONSOLIDATED)：

- `auto_compact()` 自动判断压缩级别并执行
- `estimate_tokens()` 估算消息列表 Token 数
- FROZEN: 保留最近 N 条工具结果
- TRIMMED: 保留头尾 N 条消息
- SUMMARIZED: LLM 摘要中间部分

### 6.5 ContextManager — 上下文组装

双区模型：

```
protected zone (永不截断):
  SYSTEM_PROMPT → CHACHA.md(宪法) → USER_MEMORY → CHACHA_MEMORY → SKILLS

dynamic zone (按 importance 排序):
  MEMORY.md(索引) → 今日会话记忆 → 对话历史 → 工具结果 → RAG → Hooks
```

Token 预算感知: utilization > trigger_ratio → needs_compression=True

---

## 7. 能力与插件层

### 7.1 BaseTool 基类

所有工具继承 `capabilities/base.py` 的 BaseTool：

```python
class BaseTool(ABC):
    name: str           # 工具名
    description: str    # LLM 可见的描述
    parameters: dict    # JSON Schema
    risk: str           # low | medium | high
    requires_approval: bool

    async def execute(self, **kwargs) -> str: ...   # 子类实现
    def to_function_schema(self) -> dict: ...        # 自动生成
```

### 7.2 工具注册表

`capabilities/registry.py` 的 `build_tools()` 是工具列表单一来源，CLI 和 Web 共用：

16 个工具: ProjectOverviewTool, FileOutlineTool, ListFilesTool, DepsAnalyzerTool, CodeIntelTool, SubAgentTool, ReadFileTool, ReadFilesTool, GrepTool, EditFileTool, LoadMemoryTool, WriteTopicTool, ReadTopicTool, GitDiffTool, GitLogTool, GitStatusTool, Sandbox

### 7.3 沙箱执行器

`capabilities/sandbox.py` — bash 命令安全执行：

- shell=False + shlex.split() — 命令注入防护
- start_new_session + os.killpg() — 进程组隔离
- 环境变量白名单 (仅 24 个安全变量)
- 资源限制: CPU 60s / 内存 256MB
- ANSI 转义序列清理
- 输出截断 100K 字符

### 7.4 子Agent 系统

`core/subagent/` — 派生子Agent 执行独立任务：

| 类型 | 用途 | 允许工具 | max_iter |
|------|------|---------|---------|
| explore | 代码库搜索 | read_file, grep | 15 |
| plan | 规划设计 | read_file, grep, load_memory | 10 |
| worker | 执行修改 | read_file, grep, edit_file | 10 |

特性: 独立上下文、工具白名单、超时 300s、前后置钩子。

### 7.5 占位模块 🚧

| 模块 | 当前状态 |
|------|---------|
| MCP 客户端 (mcp_client.py) | 骨架，缺 stdio 通信、工具注册 |
| Code-RAG (rag/) | 骨架，symbol_parser/vector_store 待实现 |
| OpenClaw 加载器 (openclaw_loader.py) | 骨架 |
| 插件安装器 (plugin_installer.py) | 骨架 |
| 多模态 (multimodal/) | 仅 __init__.py，配置模型已预留 (v1.5+) |

---

## 8. 数据流全链路（v2.1）

一次典型对话的完整数据流：

```
CLI app.py
  └─ AgentBridge.send_message(user_input)
       └─ AgentBridge.send_message_orchestrated(user_input)
            └─ Orchestrator.run_stream()              ← 唯一编排入口
                 ├─ 1. ConversationState 初始化
                 ├─ 2. Hook.PRE_CONTEXT_ASSEMBLY      Git 上下文注入
                 ├─ 3. Policy 检查                    速率/权限
                 ├─ 4. Gateway: session_started
                 ├─ 5. ContextManager.assemble()      上下文组装
                 │     ├─ protected: SYSTEM_PROMPT + CHACHA.md + PERMANENT_MEMORY + SKILLS
                 │     └─ dynamic: MEMORY.md + 今日记忆 + 历史
                 ├─ 6. Dispatcher.dispatch_stream()   ← 直接调用，不经 ChatEngine
                 │     ├─ LLMInvoker.stream()
                 │     │    └─ OpenAIClient.stream()  → StreamChunk
                 │     └─ asyncio.gather(*tools)      ← 同轮独立工具并发
                 │          ├─ PolicyEngine.evaluate_tool()
                 │          ├─ approval_handler()     CLI 交互审批
                 │          ├─ HookOrchestrator.run() pre-tool hooks
                 │          └─ Tool.execute()         实际执行
                 ├─ 7. ContextCompressor.auto_compact() Token 压力触发
                 ├─ 8. 上下文利用率遥测
                 ├─ 9. 最终回答提取                    DeepSeek think 兼容
                 ├─ 10. save_checkpoint()
                 ├─ 11. _save_round_memory()
                 ├─ 12. Gateway: session_ended
                 └─ 13. 清理 + DreamPipeline.record_session()
  └─ 审计: telemetry.agent.record_*()
```

---

## 9. 表现层

### 9.1 CLI (✅ 已实现)

`interface/cli/app.py` — prompt_toolkit + Rich：

- Enter 发送，Ctrl+J 换行，支持多行粘贴
- 命令历史持久化 (~/.chacha/cli_history)
- Session 管理: Ctrl+N 新建 | Ctrl+B 列表 | /session 切换/删除
- 实时流式渲染: 文本 + 工具调用追踪 + 错误显示
- 审批弹窗: PolicyEngine 触发 → CLI 交互式输入
- 快捷键: Ctrl+S 保存 | Ctrl+X 压缩 | Ctrl+L 清屏 | Ctrl+D 退出
- 审计栏: 显示耗时/Token/压缩率/轮次

`interface/cli/agent_bridge.py` — CLI ↔ 核心薄桥接层：

- `send_message()` / `send_message_orchestrated()` 统一走 `Orchestrator.run_stream()`
- 组装 AgentBridge: LLMInvoker → Dispatcher → ToolExecutor → PolicyEngine → Hooks
- CLI 审批回调 (bash 等系统工具默认拒绝)
- 模型/URL/Key 运行时切换命令
- MemoryManager/SessionService 注入

`interface/web/` — 仅 static/ 和 templates/ 的 __init__.py 占位。计划使用 FastAPI + React + WebSocket。

---

## 10. 多模态扩展预留

| 层面 | 预留项 | 计划版本 |
|------|--------|----------|
| 协议层 | GatewayMessage.payload 承载 ImageChunk/AudioChunk | v1.5+ |
| 模型层 | supports_vision / VisionClient / 多模态 token 折算 | v1.5+ |
| 上下文层 | multimodal_compression 策略 (drop/describe/keep) | v1.5+ |
| 能力层 | capabilities/multimodal/ 目录 (截图识别/语音转文字) | v1.5+ |
| 安全层 | 图片元数据校验、防提示词注入 | v1.5+ |
| 表现层 | Web 原生富媒体渲染 / CLI 降级文字 | v1.5+ |

---

## 11. 模块依赖图（v2.1）

```
interface/cli/app.py
  └── interface/cli/agent_bridge.py
        ├── core/orchestrator.py                      ← 唯一编排入口
        │     ├── core/context_manager.py
        │     │     ├── core/context/token_counter.py
        │     │     └── core/models/context.py
        │     ├── core/dispatcher.py                  ← 直接调用（并发工具执行）
        │     │     ├── core/llm_invoker.py
        │     │     │     ├── core/llm_clients/openai_client.py
        │     │     │     ├── core/llm_clients/retry_handler.py
        │     │     │     └── core/output_governor.py
        │     │     └── core/tool_executor.py
        │     │           ├── core/policy_engine.py
        │     │           ├── core/hook_orchestrator.py
        │     │           └── capabilities/builtins/*
        │     ├── core/chat_engine.py                 ← 仅消息存储 + 检查点
        │     ├── core/context/context_compressor.py
        │     ├── core/session_service.py
        │     │     └── core/context/memory_manager.py
        │     └── core/telemetry.py
        ├── core/project_init.py
        │     ├── core/context/static_rule_loader.py
        │     └── capabilities/registry.py
        └── protocol/gateway.py (可选)
              └── protocol/rpc_schema.py
```

---

> 本文档基于 v0.3 代码自动分析生成。各子系统的详细设计见对应文档。
> 图例: ✅ = 已完整实现　🚧 = 占位/骨架，待实现
