# OpenAI 客户端 (`core/llm_clients/openai_client.py`)

`OpenAIClient` 将 OpenAI 及兼容 API 的流式响应转换为 `StreamChunk` 序列，供 `LLMInvoker` 消费。

## 支持的 API

| 提供商 | 配置 |
|--------|------|
| OpenAI | `model="gpt-4"` |
| DeepSeek | `model="deepseek-chat", base_url="https://api.deepseek.com/v1"` |
| Ollama | `model="llama3", base_url="http://localhost:11434/v1", api_key="ollama"` |
| Qwen / 其他 | `model="qwen-max", base_url="..."` |

## 使用

```python
from core.llm_clients.openai_client import OpenAIClient
from core.llm_invoker import LLMInvoker

# DeepSeek
client = OpenAIClient(
    api_key="sk-...",
    model="deepseek-chat",
    base_url="https://api.deepseek.com/v1",
)
invoker = LLMInvoker(model_client=client)
resp = await invoker.invoke(messages, tools, session_id)
```

## 流式转换规则

| OpenAI 事件 | StreamChunk |
|-------------|-------------|
| `delta.content = "你好"` | `{type: "text", content: "你好"}` |
| `delta.tool_calls[0].id = "c1"` | `{type: "tool_call_start", tool_index: 0, tool_id: "c1", tool_name: "read_file"}` |
| `delta.tool_calls[0].function.arguments = '{"pa'` | `{type: "tool_call_delta", tool_index: 0, tool_args_delta: '{"pa'}` |
| `finish_reason = "stop"` | `{type: "done", finish_reason: "stop", usage: {...}}` |

**多工具并行**：通过 `tool_calls[].index` 区分，每个 index 独立追踪 `tool_call_start`（仅首次 id 出现时发送）。
