"""
tests/integration/test_orchestrator.py (v2.1)
集成测试：Orchestrator 构造函数和属性验证

v2.1: run() 已移除，改为 run_stream()。集成测试验证构造和基本配置。
"""

import pytest

from core.context_manager import ContextManager
from core.llm_invoker import LLMInvoker
from core.orchestrator import Orchestrator
from core.tool_executor import ToolExecutor

# ====== Mock 实现 ======


async def _read_file(args):
    path = args.get("path", "")
    return f"content of {path}"


class MockClient:
    def __init__(self):
        self._call_count = 0

    async def stream(self, messages, tools):
        self._call_count += 1
        from core.llm_invoker import (
            DoneChunk,
            TextChunk,
            ToolCallDeltaChunk,
            ToolCallEndChunk,
            ToolCallStartChunk,
        )

        if self._call_count == 1:
            yield ToolCallStartChunk(tool_index=0, tool_id="c1", tool_name="read_file")
            yield ToolCallDeltaChunk(tool_index=0, tool_args_delta='{"path": "/tmp/main.py"}')
            yield ToolCallEndChunk(tool_index=0)
            yield DoneChunk(finish_reason="tool_calls")
        else:
            yield TextChunk(content="文件内容是 print('hello')")
            yield DoneChunk(finish_reason="stop")


# ====== 构造和属性测试 ======


def test_orchestrator_construction():
    """Orchestrator 可以正常构造"""
    ctx_mgr = ContextManager()
    tools = ToolExecutor({"read_file": _read_file})
    orch = Orchestrator(
        context_manager=ctx_mgr,
        tool_executor=tools,
    )
    assert orch._context is ctx_mgr
    assert orch._tools is tools


def test_orchestrator_with_all_deps():
    """所有可选依赖注入"""
    ctx_mgr = ContextManager()
    tools = ToolExecutor({"read_file": _read_file})
    client = MockClient()
    llm = LLMInvoker(model_client=client)

    orch = Orchestrator(
        context_manager=ctx_mgr,
        llm_invoker=llm,
        tool_executor=tools,
        dispatcher=None,
        gateway=None,
        telemetry=None,
        hook_orchestrator=None,
        policy_engine=None,
        memory_manager=None,
        dream_pipeline=None,
    )
    assert orch._context is not None
    assert orch._llm is not None
    assert orch._tools is not None


def test_orchestrator_empty_construction():
    """空构造函数不抛异常"""
    orch = Orchestrator()
    assert orch._context is None


def test_orchestrator_set_engine():
    """set_engine 注入 ChatEngine"""
    orch = Orchestrator()

    class FakeEngine:
        pass

    engine = FakeEngine()
    orch.set_engine(engine)
    assert orch._engine is engine


@pytest.mark.asyncio
async def test_orchestrator_run_stream_requires_engine():
    """run_stream 需要 ChatEngine"""
    orch = Orchestrator()
    with pytest.raises(RuntimeError, match="ChatEngine"):
        async for _ in orch.run_stream("hello"):
            pass


def test_orchestrator_memory_manager_injection():
    """memory_manager 可以注入"""
    memory = type(
        "MockMemory",
        (),
        {
            "remember": lambda self, c: None,
            "read_permanent_memory": lambda self: "",
            "read": lambda self: "",
        },
    )()
    orch = Orchestrator(memory_manager=memory)
    assert orch._memory is memory
