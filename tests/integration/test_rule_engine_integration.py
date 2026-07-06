"""
tests/integration/test_rule_engine_integration.py
集成测试：YAML 规则 → HookOrchestrator 注册 → 执行
"""

import tempfile
from pathlib import Path

import pytest

from core.hook_orchestrator import HookOrchestrator
from core.models.hook import HookPoint, HookResult, ToolCallContext
from core.rule_engine import RuleEngine


@pytest.mark.asyncio
async def test_yaml_rule_to_hook_execution():
    """YAML 规则 → 注册 → 钩子执行"""
    d = Path(tempfile.mkdtemp())
    (d / "rules.yaml").write_text("""rules:
  - id: test-hook
    hook_point: pre_tool_execution
    handler: command:echo '{"action":"continue"}'
    matcher:
      type: tool_name
      pattern: "shell"
    priority: 10
""", encoding="utf-8")

    engine = RuleEngine()
    engine.load_dir(d)
    orch = HookOrchestrator()
    count = engine.register_all(orch)
    assert count >= 1

    hooks = orch.list_hooks(HookPoint.PRE_TOOL_EXECUTION)
    assert any(h["name"] == "test-hook" for h in hooks)


@pytest.mark.asyncio
async def test_builtin_rule_registered_and_executed():
    """内置处理器规则 → 注册 + 执行"""
    async def my_handler(ctx):
        if ctx.tool_call and "rm" in (ctx.tool_call.command_or_action or ""):
            return HookResult.block("blocked")
        return HookResult.continue_()

    builtins = {"security_check": my_handler}

    d = Path(tempfile.mkdtemp())
    (d / "security.yaml").write_text("""rules:
  - id: security-rule
    hook_point: pre_tool_execution
    handler: builtins.security_check
    matcher:
      type: command
      pattern: "rm"
    priority: 10
""", encoding="utf-8")

    engine = RuleEngine(builtins=builtins)
    engine.load_dir(d)
    orch = HookOrchestrator()
    engine.register_all(orch)

    # 安全命令 → 通过
    tc = ToolCallContext(tool_name="shell", tool_use_id="c1")
    result = await orch.run(session_id="s1", hook_point=HookPoint.PRE_TOOL_EXECUTION, tool_call=tc)
    assert result.is_continue()

    # 危险命令 → 拦截
    tc2 = ToolCallContext(tool_name="shell", tool_use_id="c2", command_or_action="rm -rf /")
    result2 = await orch.run(session_id="s1", hook_point=HookPoint.PRE_TOOL_EXECUTION, tool_call=tc2)
    assert result2.is_blocked()


@pytest.mark.asyncio
async def test_multiple_rules_different_priorities():
    """多条规则 → 按优先级顺序执行"""
    order = []

    async def handler_h1(ctx):
        order.append("h1")
        return HookResult.continue_()

    async def handler_h2(ctx):
        order.append("h2")
        return HookResult.continue_()

    builtins = {"h1": handler_h1, "h2": handler_h2}

    d = Path(tempfile.mkdtemp())
    (d / "rules.yaml").write_text("""rules:
  - id: high-priority
    hook_point: pre_tool_execution
    handler: builtins.h1
    priority: 10
  - id: low-priority
    hook_point: pre_tool_execution
    handler: builtins.h2
    priority: 1
""", encoding="utf-8")

    engine = RuleEngine(builtins=builtins)
    engine.load_dir(d)
    orch = HookOrchestrator()
    engine.register_all(orch)

    await orch.run(session_id="s1", hook_point=HookPoint.PRE_TOOL_EXECUTION)
    assert order == ["h1", "h2"]
