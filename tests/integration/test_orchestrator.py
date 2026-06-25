"""
tests/integration/test_orchestrator.py (v2.0)
集成测试：端到端模拟任务（读文件→回复）

v2.0 新增:
  - 会话结束记忆保存验证
  - tool_cache 清理验证
  - DreamPipeline 触发验证
"""

import pytest

from core.orchestrator import Orchestrator
from core.context_manager import ContextManager
from core.llm_invoker import (
    LLMInvoker, StreamChunk,
    TextChunk, ToolCallStartChunk, ToolCallDeltaChunk, ToolCallEndChunk, DoneChunk,
)
from core.tool_executor import ToolExecutor


# ====== Mock 实现 ======

class MockReadFileClient:
    def __init__(self):
        self._call_count = 0

    async def stream(self, messages, tools):
        self._call_count += 1
        if self._call_count == 1:
            yield ToolCallStartChunk(tool_index=0,
                              tool_id="c1", tool_name="read_file")
            yield ToolCallDeltaChunk(tool_index=0,
                              tool_args_delta='{"path": "/tmp/main.py"}')
            yield ToolCallEndChunk(tool_index=0)
            yield DoneChunk( finish_reason="tool_calls")
        else:
            yield TextChunk(content="文件内容是 print('hello')")
            yield DoneChunk( finish_reason="stop")


class MockMemoryTracker:
    def __init__(self):
        self.remembered_entries: list[str] = []
        self.cleaned_up = False
        self.dream_recorded = False

    def remember(self, content: str, date_str=None):
        self.remembered_entries.append(content)
        from pathlib import Path
        return Path("/fake")

    def cleanup_tool_cache(self) -> int:
        self.cleaned_up = True
        return 3

    def read_permanent_memory(self) -> str:
        return ""

    def read(self) -> str:
        return ""


class MockDreamTracker:
    def __init__(self):
        self.count = 0
        self.ran = False

    def record_session(self):
        self.count += 1

    def should_run(self) -> bool:
        return self.count >= 3


async def _read_file(args):
    path = args.get("path", "")
    return f"content of {path}"


# ====== 端到端任务 ======

@pytest.mark.skip(reason="run() removed in v2.1")
@pytest.mark.asyncio
async def test_read_file_task():
    """端到端：用户要求读文件 → Agent 调用工具 → 得到结果 → 回复"""
    client = MockReadFileClient()
    tools = ToolExecutor({"read_file": _read_file})
    llm = LLMInvoker(model_client=client)
    ctx_mgr = ContextManager()
    orch = Orchestrator(
        context_manager=ctx_mgr,
        llm_invoker=llm,
        tool_executor=tools,
    )

    resp = await orch.run(
        "帮我读一下 /tmp/main.py",
        session_id="s1",
        project_id="p1",
    )

    assert resp.text == "文件内容是 print('hello')"
    assert resp.iterations >= 2
    assert resp.error is None


@pytest.mark.skip(reason="run() removed in v2.1")
@pytest.mark.asyncio
async def test_empty_llm_invoker():
    orch = Orchestrator()
    resp = await orch.run("hello", session_id="s1")
    assert "No LLM invoker" in (resp.error or "")


@pytest.mark.skip(reason="run() removed in v2.1")
@pytest.mark.asyncio
async def test_text_only_task():
    class TextOnlyClient:
        async def stream(self, messages, tools):
            yield TextChunk(content="你好，有什么可以帮助你的？")
            yield DoneChunk( finish_reason="stop")

    llm = LLMInvoker(model_client=TextOnlyClient())
    orch = Orchestrator(context_manager=ContextManager(), llm_invoker=llm)

    resp = await orch.run("你好", session_id="s1")
    assert "你好" in resp.text
    assert resp.iterations == 1


# ====== v2.0: 记忆保存 ======

@pytest.mark.skip(reason="run() removed in v2.1")
@pytest.mark.asyncio
async def test_session_memory_saved_on_final_answer():
    """最终回答后写入 session 记忆"""
    memory = MockMemoryTracker()
    dream = MockDreamTracker()

    class SimpleTextClient:
        async def stream(self, messages, tools):
            yield TextChunk(content="这是最终回答")
            yield DoneChunk( finish_reason="stop")

    llm = LLMInvoker(model_client=SimpleTextClient())
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=llm,
        memory_manager=memory,
        dream_pipeline=dream,
    )

    resp = await orch.run("用户问题", session_id="s-mem", project_id="test")
    assert resp.iterations == 1
    assert len(memory.remembered_entries) >= 1

    entry = memory.remembered_entries[0]
    assert "Q:" in entry
    assert "A:" in entry
    assert "用户问题" in entry
    assert "这是最终回答" in entry


# ====== v2.0: 会话清理 ======

@pytest.mark.skip(reason="run() removed in v2.1")
@pytest.mark.asyncio
async def test_tool_cache_cleaned_on_session_end():
    """会话结束时 tool_cache 被清理"""
    memory = MockMemoryTracker()

    class SimpleTextClient:
        async def stream(self, messages, tools):
            yield TextChunk(content="回答")
            yield DoneChunk( finish_reason="stop")

    llm = LLMInvoker(model_client=SimpleTextClient())
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=llm,
        memory_manager=memory,
    )

    await orch.run("hello", session_id="s-clean")
    assert memory.cleaned_up is True


# ====== v2.0: DreamPipeline ======

@pytest.mark.skip(reason="run() removed in v2.1")
@pytest.mark.asyncio
async def test_dream_counted_on_each_session():
    """每次会话结束 count+1"""
    dream = MockDreamTracker()

    class SimpleTextClient:
        async def stream(self, messages, tools):
            yield TextChunk(content="回答")
            yield DoneChunk( finish_reason="stop")

    llm = LLMInvoker(model_client=SimpleTextClient())
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=llm,
        dream_pipeline=dream,
    )

    await orch.run("hello", session_id="s1")
    assert dream.count == 1

    await orch.run("world", session_id="s2")
    assert dream.count == 2
