"""
tests/unit/test_hook.py
单元测试：core/models/hook.py 钩子上下文与结果模型
覆盖：HookContext 不可变性、HookResult 各动作、工厂方法、匹配器、序列化
"""

import json

import pytest
from pydantic import ValidationError

from core.models.hook import (
    ErrorContext,
    HookAction,
    HookContext,
    HookMatcher,
    HookPoint,
    HookResult,
    LLMRequestContext,
    ToolCallContext,
)

# ========== 1. HookPoint 枚举 ==========

def test_hook_point_values():
    assert HookPoint.PRE_TOOL_EXECUTION == "pre_tool_execution"
    assert HookPoint.POST_TOOL_EXECUTION == "post_tool_execution"
    assert HookPoint.PRE_LLM_CALL == "pre_llm_call"
    assert HookPoint.POST_LLM_CALL == "post_llm_call"
    assert HookPoint.ON_ERROR == "on_error"
    assert HookPoint.ON_SESSION_START == "on_session_start"
    assert HookPoint.ON_SESSION_END == "on_session_end"
    assert len(HookPoint) == 11


# ========== 2. HookAction 枚举 ==========

def test_hook_action_values():
    assert HookAction.CONTINUE == "continue"
    assert HookAction.STOP == "stop"
    assert HookAction.BLOCK == "block"
    assert HookAction.MODIFY == "modify"


# ========== 3. HookMatcher 测试 ==========

class TestHookMatcher:
    def test_always_matcher(self):
        m = HookMatcher(type="always")
        assert m.matches(tool_name="anything") is True
        assert m.matches(command="anything") is True
        assert m.matches() is True

    def test_tool_name_match(self):
        m = HookMatcher(type="tool_name", pattern="read_file|write_file")
        assert m.matches(tool_name="read_file") is True
        assert m.matches(tool_name="write_file") is True
        assert m.matches(tool_name="shell_exec") is False

    def test_tool_name_invert(self):
        m = HookMatcher(type="tool_name", pattern="shell", invert=True)
        assert m.matches(tool_name="shell") is False
        assert m.matches(tool_name="read_file") is True

    def test_command_match(self):
        m = HookMatcher(type="command", pattern="git\\s+push")
        assert m.matches(command="git push origin main") is True
        assert m.matches(command="git status") is False

    def test_composite_and(self):
        m = HookMatcher(
            type="composite",
            composite_op="and",
            children=[
                HookMatcher(type="tool_name", pattern="shell"),
                HookMatcher(type="command", pattern="pip"),
            ],
        )
        assert m.matches(tool_name="shell", command="pip install x") is True
        assert m.matches(tool_name="shell", command="ls -la") is False
        assert m.matches(tool_name="read_file", command="pip install x") is False

    def test_composite_or(self):
        m = HookMatcher(
            type="composite",
            composite_op="or",
            children=[
                HookMatcher(type="tool_name", pattern="shell"),
                HookMatcher(type="tool_name", pattern="read_file"),
            ],
        )
        assert m.matches(tool_name="shell") is True
        assert m.matches(tool_name="read_file") is True
        assert m.matches(tool_name="write_file") is False
        # 无参数调用时，子匹配器全部返回 False → any([]) = False
        assert m.matches() is False

    def test_regex_match_handles_invalid_pattern(self):
        m = HookMatcher(type="tool_name", pattern="[invalid")
        assert m.matches(tool_name="test") is False  # re.error → False

    def test_regex_match_with_none_value(self):
        m = HookMatcher(type="tool_name", pattern=".*")
        assert m.matches(tool_name=None) is False
        assert m.matches() is False

    def test_matcher_frozen(self):
        m = HookMatcher(type="always")
        with pytest.raises(ValidationError):
            m.type = "tool_name"

    def test_serialization(self):
        m = HookMatcher(type="tool_name", pattern="read_file", invert=False)
        j = m.model_dump_json()
        restored = HookMatcher.model_validate_json(j)
        assert restored.type == "tool_name"
        assert restored.pattern == "read_file"


# ========== 4. 子上下文测试 ==========

class TestToolCallContext:
    def test_minimal(self):
        ctx = ToolCallContext(tool_name="read_file", tool_use_id="call_1")
        assert ctx.tool_name == "read_file"
        assert ctx.arguments == {}
        assert ctx.command_or_action is None

    def test_with_command(self):
        ctx = ToolCallContext(
            tool_name="shell",
            tool_use_id="call_2",
            arguments={"cmd": "ls"},
            command_or_action="ls -la",
        )
        assert ctx.command_or_action == "ls -la"

    def test_frozen(self):
        ctx = ToolCallContext(tool_name="t", tool_use_id="id")
        with pytest.raises(ValidationError):
            ctx.tool_name = "new"


class TestLLMRequestContext:
    def test_minimal(self):
        ctx = LLMRequestContext(model_name="gpt-4", provider="openai")
        assert ctx.messages_count == 0
        assert ctx.estimated_input_tokens == 0

    def test_full(self):
        ctx = LLMRequestContext(
            model_name="claude-3",
            provider="anthropic",
            messages_count=15,
            estimated_input_tokens=5000,
        )
        assert ctx.estimated_input_tokens == 5000

    def test_frozen(self):
        ctx = LLMRequestContext(model_name="m", provider="p")
        with pytest.raises(ValidationError):
            ctx.model_name = "new"


class TestErrorContext:
    def test_minimal(self):
        ctx = ErrorContext(exception_type="ValueError", message="invalid config")
        assert ctx.recoverable is False
        assert ctx.source_module is None

    def test_recoverable(self):
        ctx = ErrorContext(
            exception_type="RetryError",
            message="rate limited",
            source_module="core.llm_invoker",
            recoverable=True,
        )
        assert ctx.recoverable is True


# ========== 5. HookContext 测试 ==========

class TestHookContext:
    def test_minimal(self):
        ctx = HookContext(hook_point=HookPoint.PRE_TOOL_EXECUTION)
        assert ctx.hook_point == "pre_tool_execution"
        assert ctx.tool_call is None
        assert ctx.llm_request is None
        assert ctx.error is None

    def test_with_tool_context(self):
        ctx = HookContext(
            hook_point=HookPoint.PRE_TOOL_EXECUTION,
            session_id="s1",
            project_id="p1",
            tool_call=ToolCallContext(tool_name="read_file", tool_use_id="c1"),
        )
        assert ctx.tool_call is not None
        assert ctx.tool_call.tool_name == "read_file"

    def test_with_llm_context(self):
        ctx = HookContext(
            hook_point=HookPoint.PRE_LLM_CALL,
            session_id="s1",
            llm_request=LLMRequestContext(model_name="gpt-4", provider="openai"),
        )
        assert ctx.llm_request is not None
        assert ctx.llm_request.model_name == "gpt-4"

    def test_with_error_context(self):
        ctx = HookContext(
            hook_point=HookPoint.ON_ERROR,
            error=ErrorContext(exception_type="RuntimeError", message="timeout"),
        )
        assert ctx.error is not None
        assert ctx.error.exception_type == "RuntimeError"

    def test_with_matched_by(self):
        m = HookMatcher(type="tool_name", pattern="shell")
        ctx = HookContext(
            hook_point=HookPoint.PRE_TOOL_EXECUTION,
            matched_by=m,
        )
        assert ctx.matched_by is not None
        assert ctx.matched_by.pattern == "shell"

    def test_metadata(self):
        ctx = HookContext(
            hook_point=HookPoint.ON_SESSION_START,
            metadata={"source": "test", "version": "1.0"},
        )
        assert ctx.metadata["source"] == "test"

    def test_frozen_immutable(self):
        ctx = HookContext(hook_point=HookPoint.PRE_TOOL_EXECUTION)
        with pytest.raises(ValidationError):
            ctx.hook_point = HookPoint.POST_TOOL_EXECUTION
        with pytest.raises(ValidationError):
            ctx.session_id = "new-id"

    def test_serialization_with_tool_context(self):
        ctx = HookContext(
            hook_point=HookPoint.PRE_TOOL_EXECUTION,
            session_id="s1",
            tool_call=ToolCallContext(tool_name="read_file", tool_use_id="c1"),
            metadata={"key": "value"},
        )
        j = ctx.model_dump_json()
        restored = HookContext.model_validate_json(j)
        assert restored.hook_point == "pre_tool_execution"
        assert restored.tool_call.tool_name == "read_file"
        assert restored.metadata["key"] == "value"

    def test_serialization_with_llm_context(self):
        ctx = HookContext(
            hook_point=HookPoint.PRE_LLM_CALL,
            llm_request=LLMRequestContext(model_name="gpt-4", provider="openai", messages_count=10),
        )
        j = ctx.model_dump_json()
        restored = HookContext.model_validate_json(j)
        assert restored.llm_request.messages_count == 10

    def test_serialization_with_error_context(self):
        ctx = HookContext(
            hook_point=HookPoint.ON_ERROR,
            error=ErrorContext(exception_type="ValueError", message="bad input", recoverable=True),
        )
        j = ctx.model_dump_json()
        restored = HookContext.model_validate_json(j)
        assert restored.error.recoverable is True

    def test_serialization_with_matched_by(self):
        m = HookMatcher(type="command", pattern="rm", invert=True)
        ctx = HookContext(hook_point=HookPoint.PRE_TOOL_EXECUTION, matched_by=m)
        j = ctx.model_dump_json()
        restored = HookContext.model_validate_json(j)
        assert restored.matched_by.invert is True

    def test_missing_hook_point_raises(self):
        with pytest.raises(ValidationError):
            HookContext()


# ========== 6. HookResult 测试 ==========

class TestHookResult:
    def test_default_is_continue(self):
        r = HookResult()
        assert r.action == "continue"
        assert r.is_continue() is True
        assert r.is_blocked() is False
        assert r.is_modified() is False
        assert r.is_stopped() is False

    def test_is_blocked(self):
        r = HookResult(action=HookAction.BLOCK, message="denied")
        assert r.is_blocked() is True
        assert r.is_continue() is False

    def test_is_modified(self):
        r = HookResult(action=HookAction.MODIFY, modified_tool_args={"path": "/safe"})
        assert r.is_modified() is True

    def test_is_stopped(self):
        r = HookResult(action=HookAction.STOP)
        assert r.is_stopped() is True

    def test_frozen(self):
        r = HookResult()
        with pytest.raises(ValidationError):
            r.action = HookAction.BLOCK

    def test_serialization(self):
        r = HookResult(
            action=HookAction.BLOCK,
            message="command in blacklist",
            additional_context="用户试图执行 rm -rf，已阻止。",
        )
        j = r.model_dump_json()
        restored = HookResult.model_validate_json(j)
        assert restored.action == "block"
        assert restored.additional_context is not None

    def test_invalid_action_raises(self):
        with pytest.raises(ValidationError):
            HookResult(action="unknown")


# ========== 7. 工厂方法测试 ==========

class TestHookResultFactories:
    def test_continue_factory(self):
        r = HookResult.continue_(message="all good", additional_context="继续执行")
        assert r.action == HookAction.CONTINUE
        assert r.message == "all good"
        assert r.additional_context == "继续执行"
        assert r.is_continue() is True

    def test_continue_minimal(self):
        r = HookResult.continue_()
        assert r.action == HookAction.CONTINUE
        assert r.message is None
        assert r.additional_context is None

    def test_block_factory(self):
        r = HookResult.block(
            message="command matches blacklist",
            additional_context="用户被提示危险操作",
        )
        assert r.action == HookAction.BLOCK
        assert r.is_blocked() is True

    def test_modify_factory(self):
        r = HookResult.modify(
            modified_tool_args={"path": "/safe/path", "safe": True},
            message="路径已修正",
            additional_context="系统已将危险路径修正为安全路径",
        )
        assert r.action == HookAction.MODIFY
        assert r.is_modified() is True
        assert r.modified_tool_args == {"path": "/safe/path", "safe": True}

    def test_stop_factory(self):
        r = HookResult.stop(message="无需继续检查")
        assert r.action == HookAction.STOP
        assert r.is_stopped() is True


# ========== 8. 模拟责任链场景 ==========

def test_full_chain_scenario():
    """模拟完整的钩子链执行场景"""
    # 1. HookOrchestrator 构建上下文
    ctx = HookContext(
        hook_point=HookPoint.PRE_TOOL_EXECUTION,
        session_id="s1",
        project_id="p1",
        tool_call=ToolCallContext(
            tool_name="shell",
            tool_use_id="call_99",
            arguments={"cmd": "rm -rf /tmp/test"},
            command_or_action="rm -rf /tmp/test",
        ),
        matched_by=HookMatcher(type="tool_name", pattern="shell"),
    )

    # 2. 安全钩子检查
    if "rm" in (ctx.tool_call.command_or_action or ""):
        result1 = HookResult.block(
            message="命中危险命令",
            additional_context="⚠️ 命令包含 'rm'，系统已阻止执行。",
        )
    else:
        result1 = HookResult.continue_()

    assert result1.is_blocked() is True
    assert "rm" in (ctx.tool_call.command_or_action or "")

    # 3. 模拟 approval 钩子（通过时）
    result_approve = HookResult.continue_(
        message="用户已确认",
        additional_context="用户手动确认执行此操作。",
    )
    assert result_approve.is_continue() is True
    assert result_approve.additional_context is not None


def test_multiple_hooks_chain():
    """多个钩子顺序执行"""
    ctx = HookContext(
        hook_point=HookPoint.PRE_LLM_CALL,
        llm_request=LLMRequestContext(
            model_name="gpt-4",
            provider="openai",
            messages_count=50,
            estimated_input_tokens=30000,
        ),
    )

    # 钩子1：成本检查
    result1 = HookResult.continue_(message="cost check passed")
    assert result1.is_continue()

    # 钩子2：上下文压缩检查
    if (ctx.llm_request and ctx.llm_request.estimated_input_tokens > 20000):
        result2 = HookResult.modify(
            modified_tool_args={},
            message="建议压缩上下文",
            additional_context="📊 输入 token 数较高，建议在此之前执行上下文压缩。",
        )
    else:
        result2 = HookResult.continue_()

    assert result2.is_modified() is True
    assert result2.additional_context is not None
    assert "压缩" in (result2.additional_context or "")


def test_all_results_are_frozen():
    """所有 HookResult 实例不可变"""
    results = [
        HookResult(),
        HookResult.continue_(),
        HookResult.block("denied"),
        HookResult.modify(modified_tool_args={}, message="modified"),
        HookResult.stop(),
    ]
    for r in results:
        with pytest.raises(ValidationError):
            r.action = HookAction.CONTINUE


# ========== 9. JSON 往返 ==========

def test_roundtrip_complex_context():
    """复杂 HookContext 完整序列化/反序列化"""
    ctx = HookContext(
        hook_point=HookPoint.PRE_TOOL_EXECUTION,
        session_id="s1",
        project_id="p1",
        tool_call=ToolCallContext(
            tool_name="read_file",
            tool_use_id="call_1",
            arguments={"path": "/tmp/test.py", "offset": 0},
            command_or_action="read_file /tmp/test.py",
        ),
        matched_by=HookMatcher(type="tool_name", pattern="read_file|write_file"),
        metadata={"source": "system"},
    )

    json_str = ctx.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["hook_point"] == "pre_tool_execution"
    assert parsed["tool_call"]["tool_name"] == "read_file"
    assert parsed["tool_call"]["arguments"] == {"path": "/tmp/test.py", "offset": 0}
    assert parsed["matched_by"]["pattern"] == "read_file|write_file"

    restored = HookContext.model_validate_json(json_str)
    assert restored.tool_call.tool_name == "read_file"
    assert restored.matched_by.pattern == "read_file|write_file"
