# 工具调度器 (`core/dispatcher.py`)

LLM ⇄ 工具循环 + 并发工具执行 + Circuit Breaker + 工具结果冻结 + 流式输出。

## 两个 API

| 方法 | 用途 |
|------|------|
| `dispatch_stream(messages, session_id)` | 流式：yield text/tool/error/done chunks（**生产主流程**） |
| `dispatch(messages, session_id)` | 同步返回 `LLMResponse`（兼容旧 API） |

## 工具执行流程（v2.1 并发）

```
同一轮收到 N 个 tool_calls:
  ├─ Phase 1: 遍历 → 发出 tool_exec_start 事件 → 收集 tasks
  ├─ Phase 2: asyncio.gather(*tasks, return_exceptions=True)  ← 并发！
  │             ToolExecutor 内部 Semaphore(5) 做并发上限保护
  └─ Phase 3: 按原始顺序遍历结果 → Circuit Breaker 按序累加 → yield tool_exec_end
```

- **参数依赖**：同轮 tool_calls 的 args 都是 LLM 独立生成，不存在 A 依赖 B 结果（依赖只能是跨轮）
- **副作用依赖**：极端罕见，LLM 不应假设执行顺序
- **return_exceptions=True**：单工具异常不中断其他工具
- **Circuit Breaker**：结果仍按序处理，断路器逻辑完全不变

## 流式输出格式

`dispatch_stream()` 产出 `StreamEvent` discriminated union（`core/models/stream_event.py`）。所有事件均为 Pydantic BaseModel，消费方用 `isinstance()` 匹配：

```
yield TextEvent(content="...")
yield ReasoningEvent(content="...")
yield ToolCallStartEvent(tool_name="read_file", tool_index=0)
yield ToolExecStartEvent(tool_name="read_file", args="📄 main.py")
yield ToolExecEndEvent(tool_name="read_file", preview="第1-100行...")
yield DoneEvent(text="...", tokens=N, usage={})
yield ErrorEvent(message="...")
yield CompactEvent(reason="Token 超 80%")
```

```python
# 消费方示例
from core.models.stream_event import TextEvent, DoneEvent, ErrorEvent

async for event in dispatcher.dispatch_stream(...):
    if isinstance(event, TextEvent):
        print(event.content, end="")
    elif isinstance(event, DoneEvent):
        print(f"\n[{event.tokens} tokens]")
    elif isinstance(event, ErrorEvent):
        print(f"错误: {event.message}")
```

## 工具结果冻结

不等压缩压力，超过 `KEEP_TOOL_RESULTS`(8) 个结果时主动占位并缓存到磁盘。

```json
{"toolname":"read_file","result_summary":"读取 main.py...","cache_path":"tool_cache/read_file_c1.json"}
```

缓存位置: `{session_dir}/tool_cache/`

## 构造函数

```python
dispatcher = Dispatcher(
    llm_invoker=invoker,
    tool_executor=executor,
    telemetry=telemetry,
    project_id="b33a37744e81",
    context_window=1_048_576,  # 自适应 KEEP_TOOL_RESULTS
)

# 计数器（子Agent 用）
print(dispatcher.tool_calls_made)  # 本轮工具调用次数
```
