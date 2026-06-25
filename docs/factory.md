# 模型工厂

`core/llm_clients/factory.py` — ✅ 已实现。

当前 AgentBridge.initialize() 通过 ModelFactory.create() 创建客户端。

## 当前状态

| 功能 | 状态 |
|------|------|
| ModelFactory.create() | ✅ OpenAI/DeepSeek/Ollama |
| 根据 provider 动态选择客户端 | ✅ |
| Anthropic 客户端 | 🚧 待实现 |

## 使用方式

```python
from core.llm_clients.factory import ModelFactory
from core.llm_invoker import LLMInvoker

client = ModelFactory.create(provider_cfg)
invoker = LLMInvoker(model_client=client)
```
