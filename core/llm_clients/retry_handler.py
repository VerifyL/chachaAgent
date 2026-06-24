"""
core/model/retry_handler.py
RetryHandler — 指数退避重试。

用法:
    handler = RetryHandler(max_retries=3)
    try:
        result = await handler.execute(async_fn, arg1, arg2)
    except Exception as e:
        # 重试耗尽
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# 不可重试的关键词
_NON_RETRYABLE = ("401", "403", "authentication", "invalid_api_key")


class RetryHandler:
    """指数退避重试"""

    def __init__(
        self,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ):
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff

    async def execute(self, gen_fn, *args, **kwargs):
        """执行异步生成器函数，失败时按策略重试。重试耗尽时抛出最后异常。"""
        last_error = None

        for attempt in range(self._max_retries):
            try:
                async for chunk in gen_fn(*args, **kwargs):
                    yield chunk
                return
            except GeneratorExit:
                return
            except GeneratorExit:
                return
            except (KeyboardInterrupt, asyncio.CancelledError):
                return
            except Exception as e:
                last_error = e
                msg = str(e).lower()

                if any(keyword in msg for keyword in _NON_RETRYABLE):
                    raise

                if "429" in msg or "rate" in msg:
                    wait = self._extract_retry_after(e) or self._initial_backoff * (2 ** attempt)
                else:
                    wait = min(self._initial_backoff * (2 ** attempt), self._max_backoff)

                if attempt < self._max_retries - 1:
                    logger.warning("重试 %d/%d (%.1fs): %s", attempt + 1, self._max_retries, wait, e)
                    await asyncio.sleep(wait)
                else:
                    raise

        raise last_error or RuntimeError("retry exhausted")

    @staticmethod
    def _extract_retry_after(exc: Exception) -> float:
        """从异常消息中提取 retry_after 秒数"""
        msg = str(exc)
        if "retry_after" in msg.lower():
            try:
                parts = msg.split("retry_after=")
                if len(parts) > 1:
                    return float(parts[1].split()[0].rstrip(",)}'\""))
            except (ValueError, IndexError):
                pass
        return 0.0
