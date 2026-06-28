"""
tests/unit/test_tool_executor.py
单元测试：core/tool_executor.py ToolExecutor
覆盖：查找/执行/超时/重试/并发、PolicyEngine拦截、钩子拦截、
      输出截断、遥测调用、批量执行
"""

import asyncio

import pytest

from core.tool_executor import ToolExecutor, ToolResult


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
    assert result.duration_ms >= 0


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
    from core.models.hook import HookPoint, HookMatcher, HookResult

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
    from core.telemetry import Telemetry
    from core.models.config import TelemetryConfig

    t = Telemetry(TelemetryConfig(log_level="WARNING"))
    t.start()

    executor = ToolExecutor({"echo": _echo}, telemetry=t)
    result = await executor.execute("echo", {}, "s1", "c1")

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


# ========== 9. optional injected ==========

@pytest.mark.asyncio
async def test_no_policy_no_hooks_no_telemetry():
    """所有注入都可选，不应崩溃"""
    executor = ToolExecutor({"echo": _echo})
    result = await executor.execute("echo", {}, "s1", "c1")
    assert result.status == "success"
