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

```
yield {"type": "text", "content": "..."}
yield {"type": "tool_call_start", "tool_name": "read_file"}
yield {"type": "tool_exec_start", "tool_name": "read_file", "args": "📄 path.py"}
yield {"type": "tool_exec_end", "tool_name": "read_file", "preview": "..."}
yield {"type": "done", "text": "...", "tokens": N, "usage": {}, "session_id": ""}
yield {"type": "error", "message": "..."}
yield {"type": "compact", "reason": "..."}
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
)
```
