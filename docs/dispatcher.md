# 工具调度器 (`core/dispatcher.py`) (v2.0)

`Dispatcher` 桥接 `LLMInvoker` 和 `ToolExecutor`，处理 LLM ⇄ 工具的完整循环。

## v2.0 新增：Stage 1 工具结果缓存

保留最近 **10 个**完整工具结果，更早的替换为 JSON 占位符：

```
{"toolname": "read_file", "result_summary": "读取 main.py 前200行...", "cache_path": "tool_cache/tool_3.json"}
```

缓存到 `session/{session_id}/tool_cache/`，会话结束时清理。

## 链条

```
Dispatcher.dispatch(messages, session_id)
  │
  ├─ 1. schemas = tool_executor.get_schemas()
  ├─ 2. LLM 流式调用
  ├─ 3. 无 tool_calls → 返回最终文本
  ├─ 4. 工具执行 → 结果注入 messages
  ├─ 5. Stage 1 缓存：>10 个工具结果 → JSON 占位符
  ├─ 6. 返回步骤 2 继续
  └─ → LLMResponse（最终文本 + 累计 usage）
```

## Stage 1 vs Stage 2

| 阶段 | 位置 | 触发条件 | 格式 | 力度 |
|------|------|---------|------|------|
| Stage 1 | Dispatcher | > 10 个工具结果 | `{"toolname":"x","result_summary":"x","cache_path":"x"}` | 宽松 |
| Stage 2 | ContextCompressor FROZEN | utilization > trigger_ratio | `{"t":"x","s":"x","p":"x"}` | 激进 |

## 使用

```python
from core.dispatcher import Dispatcher
from core.tool_executor import ToolExecutor
from core.context.memory_manager import MemoryManager

mgr = MemoryManager(project_id="p1", session_id="s1")

dispatcher = Dispatcher(
    llm_invoker,
    ToolExecutor(tools=[...]),
    memory_manager=mgr,  # v2.0: 可选，用于工具结果缓存
)

# 流式
async for chunk in dispatcher.dispatch_stream(messages, "s1"):
    ...

# 同步
resp = await dispatcher.dispatch(messages, "s1")
```

## Orchestrator 集成

```python
orchestrator = Orchestrator(
    context_manager=mgr,
    dispatcher=Dispatcher(llm, tools, memory_manager=mem_mgr),
    memory_manager=mem_mgr,
)
```

## 安全限制

`max_rounds=50` —— 防止 LLM 陷入无限工具调用循环。
