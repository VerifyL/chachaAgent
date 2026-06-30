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

## ModelRouter — 多模型选择与故障转移

`core/llm_clients/router.py` 提供三种路由策略 + 自动故障隔离：

```python
from core.llm_clients.router import ModelRouter
from core.llm_clients.factory import ModelFactory

# 从配置构建
router = ModelRouter(config.model)

# 按策略选择可用客户端
client = router.select(factory=ModelFactory)
if client is None:
    raise RuntimeError("所有模型提供者均不可用")

# 故障时标记，60 秒内自动跳过
router.mark_failure("default")
# 切换 fallback
client = router.select(factory=ModelFactory)

# 恢复时标记成功
router.mark_success("default")
```

### 三种路由策略

| 策略 | 配置值 | 行为 |
|------|--------|------|
| **priority** | `"priority"` | 按 `fallback_chain` 顺序，返回第一个可用的 |
| **cost** | `"cost"` | 按 `cost_per_1k_input` 升序，选最便宜的可用 |
| **random** | `"random"` | 从所有可用 provider 中随机选一个 |

### 故障隔离

- 调用 `mark_failure(name)` 后，该 provider 被 **临时禁用 60 秒**（`BAN_TTL_SECONDS`）
- 到期自动恢复；`mark_success(name)` 可立即恢复
- `is_available(name)` 查询当前状态
- `failed_count(name)` 查看累计失败次数

### 配置示例

```toml
[model]
router_strategy = "priority"
fallback_chain = ["default", "cheap", "fallback"]

[model.providers.default]
provider = "openai"
api_key = "sk-..."
default_model = "deepseek-v4-pro"
cost_per_1k_input = 0.001
cost_per_1k_output = 0.002

[model.providers.cheap]
provider = "ollama"
default_model = "llama3"
base_url = "http://localhost:11434/v1"
cost_per_1k_input = 0.0
cost_per_1k_output = 0.0
```

## ModelFactory — 客户端创建

`core/llm_clients/factory.py` 根据 `ModelProviderConfig` 创建流式客户端：

```python
from core.llm_clients.factory import ModelFactory

# provider 类型自动映射客户端
# "openai" / "deepseek" / "qwen" → OpenAIClient
# "ollama"                        → OpenAIClient (兼容 API)
# "anthropic"                     → 尚未实现

for name, provider_cfg in config.model.providers.items():
    client = ModelFactory.create(provider_cfg)
    if client:
        print(f"{name}: {client.model} @ {client.base_url}")
```

## UsageTracker — 用量统计

`core/llm_clients/usage_tracker.py` 提供按模型累积的 Token 和成本统计：

```python
from core.llm_clients.usage_tracker import UsageTracker

tracker = UsageTracker()

# 每次 LLM 调用后记录
tracker.record(
    model="deepseek-v4-pro",
    input_tokens=1200,
    output_tokens=450,
    cost_per_1k_input=0.001,
    cost_per_1k_output=0.002,
)

# 查询
print(tracker.total_input)    # 1200
print(tracker.total_output)   # 450
print(tracker.total_cost)     # 0.0021
print(tracker.call_count)     # 1

# 按模型统计
print(tracker.per_model("deepseek-v4-pro"))
# {'input': 1200, 'output': 450, 'cost': 0.0021, 'calls': 1}

# 完整摘要
print(tracker.summary())
# {
#   'total_input': 1200, 'total_output': 450,
#   'total_cost': 0.0021, 'call_count': 1,
#   'per_model': {...}
# }

# 重置
tracker.reset()
```

## 完整集成示例：Router + Factory + UsageTracker

```python
from core.llm_clients.router import ModelRouter
from core.llm_clients.factory import ModelFactory
from core.llm_clients.usage_tracker import UsageTracker
from core.llm_invoker import LLMInvoker
from core.llm_clients.retry_handler import RetryHandler

def create_invoker(config):
    router = ModelRouter(config.model)
    tracker = UsageTracker()

    client = router.select(factory=ModelFactory)
    if client is None:
        raise RuntimeError("无可用模型")

    invoker = LLMInvoker(
        model_client=client,
        retry_handler=RetryHandler(max_retries=3),
        usage_tracker=tracker,
    )
    return invoker, router, tracker
```

## 🚧 待实现

- Anthropic 客户端适配器
