# 模型工厂 (`core/llm_clients/factory.py`)

`ModelFactory.create()` 根据 `ModelProviderConfig` 创建对应的客户端实例。

## 支持的 provider

| provider | 客户端 | 状态 |
|----------|--------|------|
| `openai` | `OpenAIClient` | ✅ |
| `ollama` | `OpenAIClient`（兼容 API） | ✅ |
| `anthropic` | — | 📋 阶段 3 待实现 |

## 使用

```python
from core.llm_clients.factory import ModelFactory
from core.models.config import ModelProviderConfig

cfg = ModelProviderConfig(provider="openai", default_model="gpt-4")
client = ModelFactory.create(cfg)

# 注入 LLMInvoker
invoker = LLMInvoker(model_client=client)
```
