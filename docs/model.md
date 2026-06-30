# 模型管理指南

当前 v3.1 实现了 OpenAI 兼容客户端 + 重试处理器 + 模型工厂 + 路由 + 用量追踪。

## 模块状态

| 模块 | 文件 | 状态 |
|------|------|------|
| OpenAI 客户端 | core/llm_clients/openai_client.py | ✅ OpenAI / DeepSeek / Ollama 兼容 |
| 重试处理器 | core/llm_clients/retry_handler.py | ✅ 指数退避 + 429 感知 + 认证不重试 |
| LLM 调用器 | core/llm_invoker.py | ✅ 编排流式调用 + tool_call 解析 |
| 模型工厂 | core/llm_clients/factory.py | ✅ OpenAI/DeepSeek/Ollama |
| 模型路由器 | core/llm_clients/router.py | ✅ priority/cost/random + 故障隔离 |
| 用量追踪器 | core/llm_clients/usage_tracker.py | ✅ 按模型累积统计 |

## 当前使用方式

CLI 通过 AgentBridge.initialize() 直接构造 OpenAIClient:

```python
from core.llm_clients.openai_client import OpenAIClient
from core.llm_clients.retry_handler import RetryHandler
from core.llm_invoker import LLMInvoker

client = OpenAIClient(api_key=key, model=model, base_url=url)
invoker = LLMInvoker(model_client=client, retry_handler=RetryHandler(max_retries=3))
```

## 配置

```toml
[model.providers.default]
provider = "openai"
api_key = "sk-..."
base_url = "https://api.deepseek.com"
default_model = "deepseek-v4-pro"
context_window = 1048576
```

环境变量: DEEPSEEK_API_KEY / OPENAI_API_KEY 可替代 api_key 字段。

## 兼容 API

通过 base_url 参数兼容任何 OpenAI-compatible API:

```python
# DeepSeek
OpenAIClient(api_key="sk-...", model="deepseek-chat", base_url="https://api.deepseek.com/v1")
# Ollama (本地)
OpenAIClient(model="llama3", base_url="http://localhost:11434/v1", api_key="ollama")
```

## Token 计数

core/context/token_counter.py 提供 Token 估算（基于字符比例，非精确 tiktoken）。

## 🚧 待实现

- Anthropic 客户端适配器
