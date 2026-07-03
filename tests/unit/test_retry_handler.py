"""
tests/unit/test_retry_handler.py
单元测试：core/llm_clients/retry_handler.py RetryHandler
"""

import pytest

from core.llm_clients.retry_handler import RetryHandler


@pytest.mark.asyncio
async def test_success_first_attempt():
    """gen_fn 是 async generator function，首次调用即成功。"""
    handler = RetryHandler(max_retries=3)

    async def ok():
        yield "done"

    result = [chunk async for chunk in handler.execute(ok)]
    assert result == ["done"]


@pytest.mark.asyncio
async def test_retry_then_succeed():
    """flaky async generator: 第一次 raise，第二次 yield 成功。"""
    handler = RetryHandler(max_retries=3, initial_backoff=0.01)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ConnectionError("timeout")
        yield "ok"

    result = [chunk async for chunk in handler.execute(flaky)]
    assert result == ["ok"]
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_exhausted():
    """always_fail 一直 raise，重试耗尽后抛出最终异常。"""
    handler = RetryHandler(max_retries=2, initial_backoff=0.01)

    async def always_fail():
        raise ConnectionError("timeout")
        # 必须包含 yield 才成为 async generator
        yield  # pragma: no cover

    with pytest.raises(ConnectionError):
        [chunk async for chunk in handler.execute(always_fail)]


@pytest.mark.asyncio
async def test_auth_error_no_retry():
    """认证错误 (401) 属于不可重试错误，直接 raise 不重试。"""
    handler = RetryHandler(max_retries=3)

    async def auth_fail():
        raise RuntimeError("401 Authentication error")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        [chunk async for chunk in handler.execute(auth_fail)]


@pytest.mark.asyncio
async def test_rate_limit_retry():
    """429 限流错误应触发重试，第二次返回成功。"""
    handler = RetryHandler(max_retries=3, initial_backoff=0.01)
    calls = 0

    async def rate_limited():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("429 Too Many Requests")
        yield "ok"

    result = [chunk async for chunk in handler.execute(rate_limited)]
    assert result == ["ok"]
    assert calls == 2


@pytest.mark.asyncio
async def test_max_retries_zero():
    """max_retries=1：首次失败直接 raise，不重试。"""
    handler = RetryHandler(max_retries=1)

    async def fail():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        [chunk async for chunk in handler.execute(fail)]
