# 工具调度器 (`core/dispatcher.py`)

`Dispatcher` 桥接 `LLMInvoker` 和 `ToolExecutor`，处理 LLM ⇄ 工具的完整循环。

## 链条

```
Dispatcher.dispatch(messages, session_id)
  │
  ├─ 1. schemas = tool_executor.get_schemas()
  ├─ 2. resp = llm_invoker.invoke(messages, tools=schemas)
  ├─ 3. 无 tool_calls → 返回最终文本
  ├─ 4. 工具执行 = tool_executor.execute_batch(tool_calls)
  ├─ 5. 结果注入 messages → 返回步骤 2 继续
  └─ → LLMResponse（最终文本 + 累计 usage）
```

## 使用

```python
from core.dispatcher import Dispatcher
from core.tool_executor import ToolExecutor
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
from capabilities.builtins.memory_tool import LoadMemoryTool, RememberTool
from core.llm_invoker import LLMInvoker

tools = ToolExecutor(tools=[
    ReadFileTool(root=project_root),
    GrepTool(root=project_root),
    LoadMemoryTool(memory_manager),
    RememberTool(memory_manager),
])

dispatcher = Dispatcher(llm_invoker, tools)
resp = await dispatcher.dispatch(messages, session_id)
```

## Orchestrator 集成

```python
# 有 Dispatcher：自动处理工具循环
orchestrator = Orchestrator(
    context_manager=mgr,
    dispatcher=Dispatcher(llm, tools),
)

# 无 Dispatcher：手动 LLM + 工具循环（向后兼容）
orchestrator = Orchestrator(
    context_manager=mgr,
    llm_invoker=llm,
    tool_executor=tools,
)
```

## 安全限制

`max_rounds=20` — 防止 LLM 陷入无限工具调用循环。
