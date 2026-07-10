# 主控制器 (`core/orchestrator.py`) (v2.1)

`Orchestrator` 是 ChachaAgent 的**唯一编排入口**，所有对话（CLI / Web / API）最终都走 `run_stream()` 13 步流水线。

## v2.1 架构统一

- **`run_stream()`** 成为唯一生产路径，不再委托 ChatEngine，独立完成全链路编排
- **ChatEngine** 降级为纯消息存储 + 检查点持久化，不再参与运行时调度
- **Dispatcher** 工具执行并发化 (`asyncio.gather`)，同轮独立工具调用并行执行

### `run_stream()` 13 步流水线

```
用户输入
  │
  ├─ 1. ConversationState 初始化 + 消息追加
  ├─ 2. Hook: PRE_CONTEXT_ASSEMBLY  ← Git 上下文注入等
  ├─ 3. Policy 检查                 ← 速率/权限拦截
  ├─ 4. Gateway: session_started
  ├─ 5. ContextManager 上下文组装   ← MEMORY.md 常驻 + 双区模型
  ├─ 6. Dispatcher.dispatch_stream() ← 直接调用（并发工具执行）
  │     ├─ LLMInvoker.stream()
  │     ├─ asyncio.gather(*tools)   ← 同轮独立工具并发
  │     └─ Circuit Breaker 按序检查
  ├─ 7. 自动压缩                    ← ContextCompressor.auto_compact()（token 阈值触发 + 轮次计数器更新）
  ├─ 7.5. 轮次压缩                  ← compression_round_interval（默认 30 轮），force=True 跳过阈值检查
  ├─ 8. 上下文利用率遥测
  ├─ 9. 最终回答提取                ← DeepSeek think 兼容
  ├─ 10. 检查点保存                 ← CheckpointManager
  ├─ 11. 会话记忆保存               ← _save_round_memory()
  ├─ 12. Gateway: session_ended
  └─ 13. 清理 + DreamPipeline 触发
```

### API

| 方法 | 流式 | 用途 | 状态 |
|------|------|------|------|
| `run_stream(user_input, session_id)` | ✅ yield `StreamEvent` | **唯一编排入口** | ✅ v2.1 |

---

## 1. 初始化

```python
Orchestrator(
    engine,             # ChatEngine（消息存储 + 检查点）
    context_manager,    # ContextManager（上下文组装）
    llm_invoker,        # LLMInvoker（流式 LLM 调用）
    tool_executor,      # ToolExecutor（工具执行 + 审批）
    dispatcher,         # Dispatcher（LLM↔工具循环 + 并发）
    memory_manager,     # MemoryManager（记忆保存 + 清理）
    dream_pipeline,     # DreamPipeline（记忆整合触发）
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
  ├─ ConversationState 初始化
  ├─ Hook.PRE_CONTEXT_ASSEMBLY（Git 上下文注入）
  ├─ Policy 检查（速率/权限）
  ├─ Gateway → SessionLifecycleEvent(event="started")
  ├─ ContextManager.assemble()（双区模型 + MEMORY.md）
  ├─ Dispatcher.dispatch_stream()（LLM + 并发工具循环）
  ├─ ContextCompressor.auto_compact()（Token 阈值触发 + 轮次计数器更新）
  ├─ compression_round_interval 触发（默认 30 轮 force 压缩）
  ├─ save_checkpoint()
  ├─ _save_round_memory(user_input, assistant_text)
  ├─ _end_session_cleanup()
  │   ├─ cleanup_tool_cache()
  │   ├─ dream_pipeline.record_session()
  │   └─ if dream_pipeline.should_run() → asyncio.create_task(dream.run())
  ├─ Gateway → SessionLifecycleEvent(event="ended")
  └─ yield DoneEvent(text=..., tokens=N, usage=...)
```

---

## 3. 错误处理

| 错误类型 | 处理方式 |
|----------|----------|
| LLM 认证错误/熔断 | **立即终止**，返回 error |
| LLM 其他错误 | 注入 state 作为系统消息，继续下一轮 |
| 工具执行错误 | ToolResult(error=True) → 正常注入 |
| 最大迭代耗尽 | 强制终止，记录警告日志 |
| Policy 拦截 | 立即返回 error chunk |

---

## 4. 使用示例

```python
from core.orchestrator import Orchestrator

orch = Orchestrator(
    engine=chat_engine,
    context_manager=ctx_mgr,
    llm_invoker=llm,
    tool_executor=tools,
    dispatcher=disp,
    memory_manager=mem_mgr,
    dream_pipeline=dream,
)

from core.models.stream_event import TextEvent, DoneEvent

async for event in orch.run_stream("帮我读一下 main.py", session_id="s1"):
    if isinstance(event, TextEvent):
        print(event.content, end="")
    elif isinstance(event, DoneEvent):
        print(f"\n[{event.tokens} tokens]")
```
