# 模型管理指南

本文档覆盖 ChachaAgent 模型管理的完整链路：配置 → 工厂 → 路由器 → 重试 → 用量追踪。

## 模块总览

```
chachaConfig.toml
  │
  ├─ ModelFactory.create(cfg)  →  模型客户端
  ├─ ModelRouter.select(factory) →  自动选择/故障转移
  ├─ RetryHandler.execute()      →  指数退避重试
  └─ UsageTracker.record()       →  累计统计
```

| 模块 | 文件 | 说明 |
|------|------|------|
| OpenAI 客户端 | `core/llm_clients/openai_client.py` | OpenAI / DeepSeek / Ollama 兼容 |
| 工厂 | `core/llm_clients/factory.py` | `ModelProviderConfig` → 客户端 |
| 路由器 | `core/llm_clients/router.py` | priority/cost/random + 故障转移 |
| 重试处理器 | `core/llm_clients/retry_handler.py` | 指数退避 + 429 感知 |
| 用量追踪器 | `core/llm_clients/usage_tracker.py` | Token/成本累加 |
| LLM 调用器 | `core/llm_invoker.py` | 编排上述模块，流式输出 |

---

## 1. 配置

```toml
# chachaConfig.toml
[model.providers.default]
provider = "openai"
default_model = "gpt-4"
api_key = "sk-..."
cost_per_1k_input = 0.003
cost_per_1k_output = 0.015

[model.providers.cheap]
provider = "openai"
default_model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key = "sk-..."
cost_per_1k_input = 0.001
cost_per_1k_output = 0.002

[model]
router_strategy = "priority"
fallback_chain = ["default", "cheap"]
retry_max_attempts = 3
```

**环境变量替代**：`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 可替代 `api_key` 字段。

## 2. 使用

### 2.1 单模型（最简单）

```python
from core.llm_clients.factory import ModelFactory
from core.llm_invoker import LLMInvoker

client = ModelFactory.create(config.model.providers["default"])
invoker = LLMInvoker(model_client=client)
resp = await invoker.invoke(messages, tools, session_id)
```

### 2.2 多模型 + 故障转移

```python
from core.llm_clients.router import ModelRouter
from core.llm_clients.factory import ModelFactory

router = ModelRouter(config.model)
client = router.select(ModelFactory)

# 调用失败时切换
router.mark_failure("default")
client = router.select(ModelFactory)  # → 自动 fallback
```

### 2.3 重试

```python
from core.llm_clients.retry_handler import RetryHandler

invoker = LLMInvoker(
    model_client=client,
    retry_handler=RetryHandler(max_retries=3),
)
```

### 2.4 用量追踪

```python
from core.llm_clients.usage_tracker import UsageTracker

tracker = UsageTracker()
tracker.record("gpt-4", 1000, 500, 0.003, 0.015)
print(f"累计成本: ${tracker.total_cost:.4f}")
```

## 3. 完整示例：生产级 LLMInvoker

```python
from core.llm_clients.factory import ModelFactory
from core.llm_clients.router import ModelRouter
from core.llm_clients.retry_handler import RetryHandler
from core.llm_clients.usage_tracker import UsageTracker
from core.llm_invoker import LLMInvoker
from core.telemetry import Telemetry

# 多模型配置
router = ModelRouter(config.model)
client = router.select(ModelFactory)

# 重试 + 遥测 + 熔断
invoker = LLMInvoker(
    model_client=client,
    telemetry=Telemetry(config.telemetry),
    policy_engine=PolicyEngine(config.policy),
    retry_handler=RetryHandler(max_retries=3),
)

# 调用
resp = await invoker.invoke(messages, tools, session_id)
```

## Token 计数

`core/context/token_counter.py` 使用 tiktoken 精确计数。

### 文本计数

```python
from core.context.token_counter import TokenCounter

counter = TokenCounter("deepseek-chat")
tokens = counter.count_text("Hello")           # 单文本
tokens = counter.count_messages(messages)      # 消息列表
tokens = counter.count_tool_schemas(tools)     # 工具定义
```

### 多模态 Token 计数（v1.5 预留）

```python
# 图片 token 估算（参考 OpenAI Vision pricing）
tokens = TokenCounter.estimate_image_tokens(512, 512, detail="low")   # → 85
tokens = TokenCounter.estimate_image_tokens(2048, 2048, detail="high")  # → 按 tiles 计算
```

| detail | 规则 |
|--------|------|
| `low` | 固定 85 tokens |
| `high` | 85 + tiles × 170（tiles = ceil(w/512) × ceil(h/512)） |
| `auto` | 同 low（当前默认） |

> 当前版本仅预留接口，实际多模态内容不计入上下文 token 统计。
