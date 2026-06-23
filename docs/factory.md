# 模型工厂

`core/llm_clients/factory.py` — 🚧 骨架占位，待实现。

当前 AgentBridge.initialize() 直接实例化 `OpenAIClient`，未经过工厂模式。

## 当前状态

| 功能 | 状态 |
|------|------|
| ModelFactory.create() | 🚧 骨架 |
| 根据 provider 动态选择客户端 | 🚧 |
| Anthropic 客户端 | 🚧 待实现 |

## 当前替代方案

```python
# AgentBridge.initialize() 中直接创建：
from core.llm_clients.openai_client import OpenAIClient
client = OpenAIClient(api_key=key, model=model, base_url=base_url)
```
