# 模型路由器 (`core/llm_clients/router.py`)

`ModelRouter` 实现多模型的选择与故障转移。

## 三种策略

| 策略 | 行为 | 配置示例 |
|------|------|----------|
| `priority` | 按 `fallback_chain` 顺序，返回第一个可用 | `fallback_chain: [default, cheap, local]` |
| `cost` | 按 `cost_per_1k_input` 升序，选最便宜 | — |
| `random` | 随机选一个可用 | — |

## 故障转移

```
gpt-4 调用失败 → router.mark_failure("default")
  → 60s 内自动切到 deepseek-chat
  → deepseek 恢复后 router.mark_success("default")
```

单模型场景下 Router 直接返回唯一的 provider，无额外开销。

## 使用

```python
from core.llm_clients.router import ModelRouter
from core.llm_clients.factory import ModelFactory

router = ModelRouter(config.model)
client = router.select(ModelFactory)
```
