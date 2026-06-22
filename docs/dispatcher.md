# 工具调度器 (`core/dispatcher.py`)

LLM ⇄ 工具循环 + 主动工具结果冻结。

## 冻结

不等压缩压力，超过 8 个结果时主动占位：

```json
{"toolname":"read_file","result_summary":"读取 main.py...","cache_path":"tool_cache/read_file_c1.json"}
```

| 配置 | 值 |
|------|------|
| `KEEP_TOOL_RESULTS` | 8 |
| `MAX_TOOL_ROUNDS` | 200 |
| 缓存 | `sessions/{sid}/tool_cache/` |

## 使用

```python
dispatcher = Dispatcher(llm_invoker, tool_executor, memory_manager)
resp = await dispatcher.dispatch(messages, session_id)
```
