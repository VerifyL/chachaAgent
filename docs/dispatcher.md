# 工具调度器 (`core/dispatcher.py`)

LLM ⇄ 工具循环 + 工具结果冻结 + 流式输出。

## 两个 API

| 方法 | 用途 |
|------|------|
| `dispatch_stream(messages, session_id)` | 流式：yield text/tool/error/done chunks（CLI 使用） |
| `dispatch(messages, session_id)` | 同步返回 `LLMResponse`（兼容旧 API） |

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
