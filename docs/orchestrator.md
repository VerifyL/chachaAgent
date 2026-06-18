# 主控制器 (`core/orchestrator.py`)

本文档说明 `Orchestrator` 的 Think-Act-Observe 循环、子系统协调和终止条件。Orchestrator 是薄胶水层，所有重活已在下游模块完成。

## 概述

设计融合了 **Claude Code 异步生成器模式**（事件驱动 + 流式输出）和 **Harness 线性流水线**（清晰的阶段划分）：

- **薄胶水层**：不重复实现任何业务逻辑，只编排调用顺序
- **Think-Act-Observe**：每轮迭代 = LLM 推理 → 工具执行 → 观察结果 → 下一轮
- **事件发布**：通过 Gateway 发布 `SessionLifecycleEvent`（started/ended）
- **终止条件**：LLM 返回 stop 且无工具调用 / 达到最大迭代次数 / 不可恢复错误

### 主循环状态机

```
                    ┌──────────────────┐
                    │   用户输入消息      │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  会话初始化/恢复   │
                    └────────┬─────────┘
                             │
              ┌──────────────▼──────────────┐
              │     iteration = 0           │
              │     while < max_iterations  │
              └──────────────┬──────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │  1. ContextManager.assemble │
              │     ConversationState       │
              │     → AssembledContext      │
              │     → messages[]            │
              └──────────────┬──────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │  2. LLMInvoker.invoke       │
              │     → 流式 token 推送       │
              │     → LLMResponse            │
              └──────────────┬──────────────┘
                             │
                    ┌────────┴────────┐
                    │   response.error?  │
                    └────────┬────────┘
                  是          │  否
                    ▼          │
        ┌──────────────┐      │
        │ 认证/熔断 → 终止  │      │
        │ 其他 → 注入 state │      │
        │ 继续下一轮        │      │
        └──────────────┘      │
                              │
                              ▼
                    ┌─────────────────────┐
                    │  3. 有 tool_calls?   │
                    └──────────┬──────────┘
                         是     │     否
                          ▼     │      │
              ┌──────────────────┐ │      │
              │ ToolExecutor     │ │      │
              │ .execute_batch() │ │      │
              │ → ToolResult[]   │ │      │
              │ → state.add_event│ │      │
              └────────┬─────────┘ │      │
                       │           │      │
                       ▼           │      │
              ┌──────────────────┐ │      │
              │ iteration++      │ │      │
              │ → 回到步骤 1     │ │      │
              └──────────────────┘ │      │
                                   │      │
                                   ▼      ▼
                          ┌──────────────────┐
                          │  4. 终止条件       │
                          │  finish=stop 或无  │
                          │  tool_calls → 结束 │
                          └────────┬─────────┘
                                   │
                                   ▼
                          ┌──────────────────┐
                          │  Gateway.publish  │
                          │  session:ended    │
                          │  Telemetry 记录   │
                          │  → OrchResponse   │
                          └──────────────────┘
```

---

## 1. Orchestrator 初始化

```python
Orchestrator(
    context_manager,    # ContextManager（组装上下文）
    llm_invoker,        # LLMInvoker（调用模型）
    tool_executor,      # ToolExecutor（执行工具）
    gateway,            # ChaChaAsyncGateway（事件推送）
    telemetry,          # Telemetry（记录指标）
    hook_orchestrator,  # HookOrchestrator（钩子）
    policy_engine,      # PolicyEngine（安全策略）
    max_iterations=50,  # 最大迭代次数
)
```

**所有参数均为可选**，支持渐进构建和测试。

---

## 2. 主循环执行流程

### 2.1 完整示例

```python
from core.orchestrator import Orchestrator

orch = Orchestrator(
    context_manager=context_mgr,
    llm_invoker=llm_invoker,
    tool_executor=tool_executor,
    gateway=gateway,
    telemetry=telemetry,
)

resp = await orch.run(
    user_input="帮我读一下 main.py",
    session_id="session-abc",
    tools=[{"type": "function", "function": {"name": "read_file", ...}}],
)
# → OrchResponse(text="文件内容是 print('hello')", iterations=2)
```

### 2.2 每轮迭代的细节

```
Iteration 1:
  1. ContextManager → [system_prompt, user:"帮我读一下 main.py"]
  2. LLMInvoker   → 流式 "正在读取..." + tool_call(read_file, path="main.py")
  3. ToolExecutor → PolicyEngine(FREE) → execute → "print('hello')"
  4. has tool_calls → append ObservationEvent → continue

Iteration 2:
  1. ContextManager → [system_prompt, user, assistant, tool_result:"print('hello')"]
  2. LLMInvoker   → 流式 "文件内容是 print('hello')" + finish_reason=stop
  3. no tool_calls + finish=stop → break
  → return OrchResponse(text="文件内容是 print('hello')")
```

---

## 3. 错误处理

| 错误类型 | 处理方式 |
|----------|----------|
| LLM 认证错误/熔断 | **立即终止**，返回 error |
| LLM 其他错误 | 注入 state 作为系统消息，继续下一轮 |
| 工具执行错误 | ToolResult(error=True) → 正常注入 ObservationEvent |
| 最大迭代耗尽 | 强制终止，记录警告日志 |

```python
if resp.error:
    if "authentication" in resp.error.lower() or "circuit" in resp.error.lower():
        return OrchResponse(error=resp.error)  # 终止
    state.add_event(MessageEvent(role="system", content=f"[Error] {resp.error}"))
    continue  # 继续下一轮
```

---

## 4. 会话生命周期

```
用户输入 "hello"
  │
  ├─ Gateway → SessionLifecycleEvent(event="started")
  │
  ├─ iteration 1 ... N
  │
  ├─ Gateway → SessionLifecycleEvent(event="ended", total_tokens=X)
  ├─ Telemetry.agent.record_session(...)
  │
  └─ → OrchResponse(text, iterations, total_tokens, duration_ms)
```

---

## 5. 子系统联动图

```
Orchestrator.run()
  │
  ├─ ContextManager      → state → 每条事件转为 ContextBlock
  ├─ LLMInvoker          → messages → StreamChunk 流 → TokenChunkEvent → Gateway
  ├─ ToolExecutor        → tool_calls → PolicyEngine → HookOrch → execute → Telemetry
  ├─ Gateway             → SessionLifecycleEvent(started/ended)
  └─ Telemetry           → record_session(total_tokens, duration_ms)
```
