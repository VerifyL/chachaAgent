# 主控制器 (`core/orchestrator.py`) (v2.0)

本文档说明 `Orchestrator` 的 Think-Act-Observe 循环、子系统协调和终止条件。

## v2.0 新增

- **每轮记忆保存**：assistant 最终回答后异步保存 `session/{date}.md`（只含 user+assistant）
- **tool_cache 清理**：会话结束时自动清理 `tool_cache/` 目录
- **DreamPipeline 触发**：每次会话记录计数，10 次或 24h 触发记忆整合
- **永久记忆联动**：支持 CHACHA_MEMORY.md 的异步更新

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
              │     + PRE_CONTEXT_ASSEMBLY  │
              │     钩子注入（Git感知等）    │
              │     → messages[]            │
              └──────────────┬──────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │  2. LLM 流式调用             │
              │     → 流式 token 推送       │
              │     → LLMResponse            │
              └──────────────┬──────────────┘
                             │
                    ┌────────┴────────┐
                    │  有 tool_calls?   │
                    └────────┬────────┘
                         是     │     否
                          ▼     │      │
              ┌──────────────────┐ │      │
              │ ToolExecutor     │ │      │
              │ .execute_batch() │ │      │
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
                          │  3. 最终回答       │
                          │  → _save_round    │
                          │    _memory()      │
                          └────────┬─────────┘
                                   │
                                   ▼
                          ┌──────────────────┐
                          │  4. 会话结束       │
                          │  cleanup_tool_cache│
                          │  dream.record_    │
                          │  session()        │
                          │  → OrchResponse   │
                          └──────────────────┘
```

---

## 1. 初始化

```python
Orchestrator(
    context_manager,    # ContextManager（组装上下文）
    llm_invoker,        # LLMInvoker（调用模型）
    tool_executor,      # ToolExecutor（执行工具）
    dispatcher,         # Dispatcher（v2.0: 含 Stage 1 工具缓存）
    memory_manager,     # MemoryManager（v2.0: 记忆保存 + 清理）
    dream_pipeline,     # DreamPipeline（v2.0: 记忆整合触发）
    gateway,            # ChaChaAsyncGateway
    telemetry,          # Telemetry
    hook_orchestrator,  # HookOrchestrator
    policy_engine,      # PolicyEngine
    max_iterations=50,
)
```

---

## 2. 会话生命周期

```
用户输入 "hello"
  │
  ├─ Gateway → SessionLifecycleEvent(event="started")
  ├─ iteration 1 ... N
  ├─ 最终回答 → _save_round_memory(user_input, assistant_text)
  ├─ _end_session_cleanup()
  │   ├─ cleanup_tool_cache()
  │   ├─ dream_pipeline.record_session()
  │   └─ if dream_pipeline.should_run() → asyncio.create_task(dream.run())
  ├─ Gateway → SessionLifecycleEvent(event="ended")
  └─ → OrchResponse(text, iterations, total_tokens, duration_ms)
```

---

## 3. 错误处理

| 错误类型 | 处理方式 |
|----------|----------|
| LLM 认证错误/熔断 | **立即终止**，返回 error |
| LLM 其他错误 | 注入 state 作为系统消息，继续下一轮 |
| 工具执行错误 | ToolResult(error=True) → 正常注入 ObservationEvent |
| 最大迭代耗尽 | 强制终止，记录警告日志 |

---

## 4. 使用示例

```python
from core.orchestrator import Orchestrator
from core.context_manager import ContextManager
from core.context.memory_manager import MemoryManager
from core.context.dream import DreamPipeline

mem_mgr = MemoryManager(project_id="p1", session_id="s1")
dream = DreamPipeline(llm_invoker)

orch = Orchestrator(
    context_manager=ContextManager(),
    llm_invoker=llm_invoker,
    tool_executor=tool_executor,
    memory_manager=mem_mgr,
    dream_pipeline=dream,
)

resp = await orch.run(
    "帮我读一下 main.py",
    session_id="s1",
    project_id="p1",
)
# → OrchResponse(text="文件内容是 print('hello')", iterations=2)
```
