"""
tests/unit/test_tool_executor.py
单元测试：core/tool_executor.py ToolExecutor
覆盖：查找/执行/超时/重试/并发、PolicyEngine拦截、钩子拦截、
      输出截断、遥测调用、批量执行
"""

import asyncio

import pytest

from core.tool_executor import ToolExecutor

# ========== Fixtures ==========


async def _echo(args):
    return f"echo: {args}"


async def _slow(args):
    await asyncio.sleep(10)
    return "done"


async def _crash(args):
    raise ValueError("boom")


# ========== 1. 基本执行 ==========


@pytest.mark.asyncio
async def test_execute_success():
    executor = ToolExecutor({"read_file": _echo})
    result = await executor.execute("read_file", {"path": "/tmp/a.py"}, "s1", "c1")
    assert result.status == "success"
    assert "/tmp/a.py" in result.content
    assert result.execution_time_ms >= 0


@pytest.mark.asyncio
async def test_tool_not_found():
    executor = ToolExecutor({})
    result = await executor.execute("nonexistent", {}, "s1", "c1")
    assert result.status == "error"
    assert "not found" in (result.error or "")


# ========== 2. 超时 + 重试 ==========


@pytest.mark.asyncio
async def test_timeout_and_retry():
    executor = ToolExecutor(
        {"slow": _slow},
        default_timeout=0.05,
        max_retries=1,
    )
    result = await executor.execute("slow", {}, "s1", "c1")
    assert result.status == "error" and result.error_type == "timeout"
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_execution_error():
    executor = ToolExecutor({"crash": _crash})
    result = await executor.execute("crash", {}, "s1", "c1")
    assert result.status == "error"
    assert "boom" in (result.error or "")


# ========== 3. 输出截断 ==========


@pytest.mark.asyncio
async def test_output_truncation():
    async def _big(args):
        return "x" * 200_000

    executor = ToolExecutor({"big": _big}, max_output_chars=1000)
    result = await executor.execute("big", {}, "s1", "c1")
    assert result.truncated is True
    assert "截断" in result.content
    assert "cache_key" in result.content


# ========== 4. PolicyEngine 拦截 ==========


@pytest.mark.asyncio
async def test_policy_block():
    from core.policy_engine import PolicyEngine

    engine = PolicyEngine()
    executor = ToolExecutor({"shell": _echo}, policy_engine=engine)

    # rm -rf 命中黑名单
    result = await executor.execute("shell", {"cmd": "rm -rf /"}, "s1", "c1")
    assert result.status == "error" and result.error_type == "blocked"


@pytest.mark.asyncio
async def test_policy_allow():
    from core.policy_engine import PolicyEngine

    engine = PolicyEngine()
    executor = ToolExecutor({"read_file": _echo}, policy_engine=engine)
    result = await executor.execute("read_file", {"path": "/tmp/a.py"}, "s1", "c1")
    assert result.status == "success"


# ========== 5. 钩子拦截 ==========


@pytest.mark.asyncio
async def test_hook_block():
    from core.hook_orchestrator import HookOrchestrator
    from core.models.hook import HookPoint, HookResult

    orch = HookOrchestrator()

    def blocker(ctx):
        return HookResult.block("blocked by test hook")

    orch.register("blocker", HookPoint.PRE_TOOL_EXECUTION, blocker)
    executor = ToolExecutor({"echo": _echo}, hook_orchestrator=orch)

    result = await executor.execute("echo", {}, "s1", "c1")
    assert result.status == "error" and result.error_type == "blocked"
    assert "blocked by test hook" in (result.error or "")


# ========== 6. 遥测 ==========


@pytest.mark.asyncio
async def test_telemetry_called():
    from core.models.config import TelemetryConfig
    from core.telemetry import Telemetry

    t = Telemetry(TelemetryConfig(enabled=True, log_level="WARNING"))
    t.start()

    executor = ToolExecutor({"echo": _echo}, telemetry=t)
    _ = await executor.execute("echo", {}, "s1", "c1")

    # 验证遥测记录
    key = 'chacha_tool_calls_total{status="success",tool="echo"}'
    assert t.metrics.counters[key] == 1

    t.stop()


# ========== 7. 批量执行 ==========


@pytest.mark.asyncio
async def test_execute_batch():
    executor = ToolExecutor({"echo": _echo})
    calls = [
        {"tool_name": "echo", "arguments": {"x": 1}, "tool_use_id": "c1"},
        {"tool_name": "echo", "arguments": {"x": 2}, "tool_use_id": "c2"},
        {"tool_name": "echo", "arguments": {"x": 3}, "tool_use_id": "c3"},
    ]
    results = await executor.execute_batch(calls, "s1")
    assert len(results) == 3
    for r in results:
        assert r.status == "success"


# ========== 8. 查询 ==========


def test_list_tools():
    executor = ToolExecutor({"a": _echo, "b": _echo})
    assert executor.list_tools() == ["a", "b"]
    assert executor.has_tool("a") is True
    assert executor.has_tool("c") is False


# ========== 9. 缓存读写 ==========


def test_cache_write_and_read():
    """主 Agent 截断 → 写入缓存 → cache_read 续读成功"""
    executor = ToolExecutor({"echo": _echo}, max_output_chars=100)
    # 手动写入缓存（模拟截断流程的内部操作）
    import time

    executor._output_cache["abc123"] = ("hello world " * 50, time.time())

    result = executor._get_cached_output("abc123", offset=20, limit=30)
    assert result.startswith("[cache_key=abc123]")
    assert "hello world" in result


def test_cache_miss_on_unknown_key():
    """缓存未命中 → 返回错误信息"""
    executor = ToolExecutor({"echo": _echo})
    result = executor._get_cached_output("nonexistent", offset=0, limit=100)
    assert result.startswith("[错误]")


def test_cache_cleanup_removes_expired():
    """超过 600 秒的缓存被清理"""
    import time

    executor = ToolExecutor({"echo": _echo}, max_output_chars=100)
    executor._output_cache["fresh"] = ("data", time.time())
    executor._output_cache["stale"] = ("data", time.time() - 3600)  # 1 小时前

    assert len(executor._output_cache) == 2
    executor._cleanup_cache()
    assert len(executor._output_cache) == 1
    assert "fresh" in executor._output_cache
    assert "stale" not in executor._output_cache


def test_cache_merge_from_subagent():
    """spawner 合并子 Agent 缓存 → 时间戳应刷新为当前时间"""
    import time

    parent = ToolExecutor({"echo": _echo})

    # 模拟子 Agent 在 300 秒前写入缓存
    old_ts = time.time() - 300
    child_cache = {"sub_key": ("sub output", old_ts)}

    # 合并（模拟 spawner.py:126-130 行为）
    now = time.time()
    for k, (output, _) in child_cache.items():
        parent._output_cache[k] = (output, now)

    assert "sub_key" in parent._output_cache
    _, merged_ts = parent._output_cache["sub_key"]
    # 合并后时间戳应在 5 秒以内（而非 300 秒前）
    assert time.time() - merged_ts < 5


def test_cache_ttl_is_600_seconds():
    """验证 TTL 为 600"""
    executor = ToolExecutor({"echo": _echo})
    import time

    executor._output_cache["test"] = ("data", time.time() - 590)  # 590 秒前
    executor._cleanup_cache()
    assert "test" in executor._output_cache, "590 秒不应过期"

    executor._output_cache["test"] = ("data", time.time() - 610)  # 610 秒前
    executor._cleanup_cache()
    assert "test" not in executor._output_cache, "610 秒应过期"


# ========== 10. optional injected ==========


@pytest.mark.asyncio
async def test_no_policy_no_hooks_no_telemetry():
    """所有注入都可选，不应崩溃"""
    executor = ToolExecutor({"echo": _echo})
    result = await executor.execute("echo", {}, "s1", "c1")
    assert result.status == "success"
