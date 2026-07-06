"""
tests/unit/test_hook_orchestrator.py
单元测试：core/hook_orchestrator.py
覆盖：注册/注销、责任链顺序、BLOCK短路、MODIFY链式覆盖、
      additional_context累积、POST倒序、超时、外部进程、容错推断
"""

import pytest

from core.hook_orchestrator import HookOrchestrator
from core.models.hook import (
    HookContext,
    HookMatcher,
    HookPoint,
    HookResult,
)


def _make_ctx(**kw):
    return HookContext(hook_point=HookPoint.PRE_TOOL_EXECUTION, **kw)


# ========== 1. 注册与注销 ==========


def test_register_and_list():
    o = HookOrchestrator()
    o.register("audit", HookPoint.PRE_TOOL_EXECUTION, lambda ctx: HookResult.continue_())
    hooks = o.list_hooks()
    assert len(hooks) == 1
    assert hooks[0]["name"] == "audit"


def test_unregister():
    o = HookOrchestrator()
    o.register("a", HookPoint.PRE_TOOL_EXECUTION, lambda ctx: HookResult.continue_())
    o.unregister("a")
    assert len(o.list_hooks()) == 0


def test_register_sorted_by_priority():
    """高 priority 先执行"""
    o = HookOrchestrator()
    o.register("low", HookPoint.PRE_TOOL_EXECUTION, lambda ctx: HookResult.continue_(), priority=0)
    o.register("high", HookPoint.PRE_TOOL_EXECUTION, lambda ctx: HookResult.continue_(), priority=10)
    hooks = o.list_hooks()
    assert hooks[0]["name"] == "high"
    assert hooks[1]["name"] == "low"


# ========== 2. 责任链顺序 ==========


@pytest.mark.asyncio
async def test_chain_execution_order():
    order = []

    def make_hook(name):
        def hook(ctx):
            order.append(name)
            return HookResult.continue_()

        return hook

    o = HookOrchestrator()
    o.register("a", HookPoint.PRE_TOOL_EXECUTION, make_hook("a"), priority=1)
    o.register("b", HookPoint.PRE_TOOL_EXECUTION, make_hook("b"), priority=2)
    o.register("c", HookPoint.PRE_TOOL_EXECUTION, make_hook("c"), priority=3)

    await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert order == ["c", "b", "a"]  # 高优先级先


@pytest.mark.asyncio
async def test_post_hook_reverse_order():
    """POST 钩子倒序执行"""
    order = []

    def make_hook(name):
        def hook(ctx):
            order.append(name)
            return HookResult.continue_()

        return hook

    o = HookOrchestrator()
    o.register("a", HookPoint.POST_TOOL_EXECUTION, make_hook("a"), priority=1)
    o.register("b", HookPoint.POST_TOOL_EXECUTION, make_hook("b"), priority=2)
    o.register("c", HookPoint.POST_TOOL_EXECUTION, make_hook("c"), priority=3)

    await o.run(hook_point=HookPoint.POST_TOOL_EXECUTION)
    assert order == ["a", "b", "c"]  # POST 倒序：低优先级先


# ========== 3. BLOCK 短路 ==========


@pytest.mark.asyncio
async def test_block_short_circuit():
    order = []

    def safe_hook(ctx):
        order.append("safe")
        return HookResult.continue_()

    def block_hook(ctx):
        order.append("block")
        return HookResult.block("denied")

    def after_hook(ctx):
        order.append("after")
        return HookResult.continue_()

    o = HookOrchestrator()
    o.register("safe", HookPoint.PRE_TOOL_EXECUTION, safe_hook, priority=3)
    o.register("blocker", HookPoint.PRE_TOOL_EXECUTION, block_hook, priority=2)
    o.register("after", HookPoint.PRE_TOOL_EXECUTION, after_hook, priority=1)

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert result.is_blocked() is True
    assert order == ["safe", "block"]  # after 未执行


# ========== 4. STOP 停止链但不拒绝 ==========


@pytest.mark.asyncio
async def test_stop_chain():
    order = []

    def h1(ctx):
        order.append("h1")
        return HookResult.continue_()

    def h2(ctx):
        order.append("h2")
        return HookResult.stop("enough")

    def h3(ctx):
        order.append("h3")
        return HookResult.continue_()

    o = HookOrchestrator()
    o.register("h1", HookPoint.PRE_TOOL_EXECUTION, h1, priority=3)
    o.register("h2", HookPoint.PRE_TOOL_EXECUTION, h2, priority=2)
    o.register("h3", HookPoint.PRE_TOOL_EXECUTION, h3, priority=1)

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert result.is_stopped() is False  # 最终结果按 CONTINUE 返回
    assert order == ["h1", "h2"]


# ========== 5. MODIFY 链式覆盖 ==========


@pytest.mark.asyncio
async def test_modify_chain():
    def add_path(ctx):
        return HookResult.modify(modified_tool_args={"path": "/a"})

    def add_safe(ctx):
        return HookResult.modify(modified_tool_args={"safe": True})

    o = HookOrchestrator()
    o.register("path", HookPoint.PRE_TOOL_EXECUTION, add_path, priority=2)
    o.register("safe", HookPoint.PRE_TOOL_EXECUTION, add_safe, priority=1)

    from core.models.hook import ToolCallContext

    tc = ToolCallContext(tool_name="test", tool_use_id="c1")
    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION, tool_call=tc)

    assert result.is_modified() is True
    assert result.modified_tool_args == {"path": "/a", "safe": True}


# ========== 6. additional_context 累积 ==========


@pytest.mark.asyncio
async def test_additional_context_accumulation():
    def h1(ctx):
        return HookResult.continue_(additional_context="提示A")

    def h2(ctx):
        return HookResult.continue_(additional_context="提示B")

    o = HookOrchestrator()
    o.register("h1", HookPoint.PRE_TOOL_EXECUTION, h1, priority=2)
    o.register("h2", HookPoint.PRE_TOOL_EXECUTION, h2, priority=1)

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert result.additional_context == "提示A\n提示B"


# ========== 7. 钩子匹配器 ==========


@pytest.mark.asyncio
async def test_matcher_filters_hooks():
    called = []

    def shell_only(ctx):
        called.append("shell")
        return HookResult.continue_()

    def all_tools(ctx):
        called.append("all")
        return HookResult.continue_()

    o = HookOrchestrator()
    o.register(
        "shell",
        HookPoint.PRE_TOOL_EXECUTION,
        shell_only,
        matcher=HookMatcher(type="tool_name", pattern="shell"),
    )
    o.register("all", HookPoint.PRE_TOOL_EXECUTION, all_tools)

    from core.models.hook import ToolCallContext

    # 调用 read_file → shell_only 不匹配
    tc = ToolCallContext(tool_name="read_file", tool_use_id="c1")
    await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION, tool_call=tc)
    assert called == ["all"]


# ========== 8. 无匹配钩子 ==========


@pytest.mark.asyncio
async def test_no_matching_hooks_returns_continue():
    o = HookOrchestrator()
    o.register("a", HookPoint.PRE_TOOL_EXECUTION, lambda c: HookResult.block("no"))
    # 在其他 hook_point 上调用
    result = await o.run(hook_point=HookPoint.POST_TOOL_EXECUTION)
    assert result.is_continue() is True


# ========== 9. 超时 ==========


@pytest.mark.asyncio
async def test_timeout_triggers_continue_for_safe_hook():
    import asyncio

    async def slow(ctx):
        await asyncio.sleep(10)  # 远超过 timeout
        return HookResult.continue_()

    o = HookOrchestrator()
    o.register(
        "slow", HookPoint.PRE_TOOL_EXECUTION, slow, timeout=0.1, on_timeout_continue=True, on_error_continue=True
    )

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    # v2.1: 显式 on_timeout_continue=True 时超时继续
    assert result.is_continue() is True


# ========== 10. 外部进程（mock） ==========


@pytest.mark.asyncio
async def test_external_style_hook_blocked():
    """模拟外部钩子的行为：返回 BLOCK 结果"""
    o = HookOrchestrator()
    o.register("ext", HookPoint.PRE_TOOL_EXECUTION, lambda ctx: HookResult.block("rejected"))

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert result.is_blocked() is True


# ========== 11. 容错推断 ==========


@pytest.mark.asyncio
async def test_error_in_continue_hook_continues():
    def crashy(ctx):
        raise RuntimeError("boom")

    o = HookOrchestrator()
    o.register("crashy", HookPoint.PRE_TOOL_EXECUTION, crashy, on_error_continue=True, on_timeout_continue=True)

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    # v2.1: 显式 on_error_continue=True 时异常继续
    assert result.is_continue() is True


# ========== 12. 完整链场景 ==========


@pytest.mark.asyncio
async def test_full_chain():
    """
    模拟完整钩子链：
    audit(日志) → security(BLOCK) → param(MODIFY) → helper(CONTINUE)
    """
    results = []

    async def audit_hook(ctx):
        results.append("audit")
        return HookResult.continue_(additional_context="审计: 工具即将执行")

    def security_hook(ctx):
        results.append("security")
        if "rm" in (ctx.tool_call.command_or_action or ""):
            return HookResult.block("危险命令: rm 禁止执行", additional_context="⚠️ rm被拦截")
        return HookResult.continue_()

    def param_hook(ctx):
        results.append("param")
        return HookResult.modify(modified_tool_args={"timeout": 30})

    def helper_hook(ctx):
        results.append("helper")
        return HookResult.continue_()

    o = HookOrchestrator()
    o.register("audit", HookPoint.PRE_TOOL_EXECUTION, audit_hook, priority=4)
    o.register("security", HookPoint.PRE_TOOL_EXECUTION, security_hook, priority=3)
    o.register("param", HookPoint.PRE_TOOL_EXECUTION, param_hook, priority=2)
    o.register("helper", HookPoint.PRE_TOOL_EXECUTION, helper_hook, priority=1)

    from core.models.hook import ToolCallContext

    tc = ToolCallContext(
        tool_name="shell",
        tool_use_id="c1",
        command_or_action="rm -rf /tmp/test",
    )
    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION, tool_call=tc)

    assert result.is_blocked() is True
    assert "rm" in (result.additional_context or "")
    assert "audit" in results and "security" in results
    # param 和 helper 不应执行
    assert "param" not in results
    assert "helper" not in results


@pytest.mark.asyncio
async def test_full_chain_all_continue():
    """所有钩子 CONTINUE → 最终 CONTINUE + 累积 context"""

    def h1(ctx):
        return HookResult.continue_(additional_context="H1")

    def h2(ctx):
        return HookResult.modify({"key": "val"}, additional_context="H2")

    o = HookOrchestrator()
    o.register("h1", HookPoint.PRE_TOOL_EXECUTION, h1, priority=2)
    o.register("h2", HookPoint.PRE_TOOL_EXECUTION, h2, priority=1)

    result = await o.run(hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert result.is_modified() is True
    assert "H1" in (result.additional_context or "")
    assert "H2" in (result.additional_context or "")
    assert result.modified_tool_args == {"key": "val"}
