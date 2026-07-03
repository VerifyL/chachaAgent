"""
tests/integration/test_tool_executor_integration.py
集成测试：执行内置工具、策略+钩子+遥测联动、批量并发
"""

import asyncio

import pytest

from core.tool_executor import ToolExecutor
from core.policy_engine import PolicyEngine
from core.hook_orchestrator import HookOrchestrator
from core.models.hook import HookPoint, HookResult
from core.telemetry import Telemetry
from core.models.config import TelemetryConfig


# ====== 模拟内置工具 ======

async def _read_file(args):
    path = args.get("path", "")
    return f"content of {path}"

async def _shell_exec(args):
    cmd = args.get("cmd", "")
    return f"executed: {cmd}"

async def _grep(args):
    pattern = args.get("pattern", "")
    return f"matched pattern: {pattern}"


# ====== 完整联动测试 ======

@pytest.mark.asyncio
async def test_full_tool_execution_with_policy_and_hooks():
    """内置工具 + PolicyEngine + HookOrchestrator + Telemetry 全联动"""
    t = Telemetry(TelemetryConfig(log_level="WARNING", enabled=True))
    t.start()

    policy = PolicyEngine()
    hooks = HookOrchestrator(telemetry=t)

    order = []
    def audit_hook(ctx):
        order.append("pre-audit")
        return HookResult.continue_()

    def post_hook(ctx):
        order.append("post-audit")
        return HookResult.continue_()

    hooks.register("pre-audit", HookPoint.PRE_TOOL_EXECUTION, audit_hook, priority=1)
    hooks.register("post-audit", HookPoint.POST_TOOL_EXECUTION, post_hook, priority=1)

    executor = ToolExecutor(
        {"read_file": _read_file, "shell": _shell_exec},
        policy_engine=policy,
        hook_orchestrator=hooks,
        telemetry=t,
    )

    # 安全读文件 → 应通过
    result = await executor.execute("read_file", {"path": "/tmp/a.py"}, "s1", "c1")
    assert result.status == "success"
    assert order == ["pre-audit", "post-audit"]

    # 验证遥测：工具调用 + 钩子调用
    tool_key = 'chacha_tool_calls_total{status="success",tool="read_file"}'
    assert t.metrics.counters[tool_key] == 1
    hook_key = 'chacha_hook_calls_total{action="continue",hook="pre-audit"}'
    assert t.metrics.counters[hook_key] == 1

    t.stop()


@pytest.mark.asyncio
async def test_policy_blocks_and_metrics_recorded():
    """黑名单拦截 → blocked 状态 + 策略决策指标"""
    t = Telemetry(TelemetryConfig(log_level="WARNING", enabled=True))
    t.start()

    policy = PolicyEngine(telemetry=t)
    executor = ToolExecutor({"shell": _shell_exec}, policy_engine=policy, telemetry=t)

    result = await executor.execute("shell", {"cmd": "rm -rf /"}, "s1", "c1")
    assert result.status == "error"
    assert result.error_type == "blocked"

    # 策略决策指标
    assert t.metrics.counters['chacha_policy_decisions_total{status="blocked",tool="shell"}'] >= 1

    t.stop()


@pytest.mark.asyncio
async def test_concurrent_batch_execution():
    """批量并发执行 3 个工具"""
    executor = ToolExecutor(
        {"read_file": _read_file, "shell": _shell_exec, "grep": _grep},
        max_concurrent=2,
    )

    calls = [
        {"tool_name": "read_file", "arguments": {"path": "/a"}, "tool_use_id": "c1"},
        {"tool_name": "shell", "arguments": {"cmd": "ls"}, "tool_use_id": "c2"},
        {"tool_name": "grep", "arguments": {"pattern": "TODO"}, "tool_use_id": "c3"},
    ]

    results = await executor.execute_batch(calls, "s1")
    assert len(results) == 3
    statuses = [r.status for r in results]
    assert statuses == ["success", "success", "success"]


@pytest.mark.asyncio
async def test_tool_not_found_in_execution():
    """MCP 风格：注册工具后调用，未知工具应报错"""
    executor = ToolExecutor({"read_file": _read_file})
    result = await executor.execute("mcp_unknown_tool", {}, "s1", "c1")
    assert result.status == "error"
    assert "not found" in (result.error or "")
