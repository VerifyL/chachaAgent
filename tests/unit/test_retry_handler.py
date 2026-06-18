"""
tests/unit/test_retry_handler.py
单元测试：core/model/retry_handler.py RetryHandler
"""

import pytest

from core.llm_clients.retry_handler import RetryHandler


@pytest.mark.asyncio
async def test_success_first_attempt():
    handler = RetryHandler(max_retries=3)

    async def ok():
        return "done"

    result = await handler.execute(ok)
    assert result == "done"


@pytest.mark.asyncio
async def test_retry_then_succeed():
    handler = RetryHandler(max_retries=3, initial_backoff=0.01)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ConnectionError("timeout")
        return "ok"

    result = await handler.execute(flaky)
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_exhausted():
    handler = RetryHandler(max_retries=2, initial_backoff=0.01)

    async def always_fail():
        raise ConnectionError("timeout")

    with pytest.raises(ConnectionError):
        await handler.execute(always_fail)


@pytest.mark.asyncio
async def test_auth_error_no_retry():
    handler = RetryHandler(max_retries=3)

    async def auth_fail():
        raise RuntimeError("401 Authentication error")

    with pytest.raises(RuntimeError):
        await handler.execute(auth_fail)


@pytest.mark.asyncio
async def test_rate_limit_retry():
    handler = RetryHandler(max_retries=3, initial_backoff=0.01)
    calls = 0

    async def rate_limited():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("429 Too Many Requests")
        return "ok"

    result = await handler.execute(rate_limited)
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_max_retries_zero():
    handler = RetryHandler(max_retries=1)

    async def fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await handler.execute(fail)
