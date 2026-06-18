# 重试处理器 (`core/llm_clients/retry_handler.py`)

`RetryHandler` 实现 LLM 调用的指数退避重试。

## 重试策略

| 异常 | 行为 |
|------|------|
| `429` / rate limit | 等待 `retry_after` 或指数退避后重试 |
| `timeout` / `connection` | 指数退避 1s → 2s → 4s |
| `401` / `403` / 认证 | **不重试**，立即抛 |
| 其他异常 | 指数退避重试 |

## 使用

```python
from core.llm_clients.retry_handler import RetryHandler

handler = RetryHandler(max_retries=3)
try:
    result = await handler.execute(model_client.stream, messages, tools)
except Exception:
    # 重试耗尽
    pass
```
