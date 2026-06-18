"""
tests/unit/test_orchestrator.py
单元测试：core/orchestrator.py Orchestrator (v2.0)

覆盖：
  - _save_round_memory 只保存 user+assistant
  - _end_session_cleanup 清理 tool_cache
  - DreamPipeline 会话计数和触发
  - OrchResponse 数据结构
"""

from pathlib import Path

import pytest

from core.orchestrator import Orchestrator, OrchResponse
from core.context_manager import ContextManager


# ====== Mock 实现 ======

class MockLLMInvoker:
    def __init__(self):
        self.calls = []

    async def invoke(self, messages, tools=None, session_id=""):
        self.calls.append(messages)
        from core.llm_invoker import LLMResponse
        return LLMResponse(text="Hello, world!", finish_reason="stop")


class MockMemoryManager:
    def __init__(self, project_id="test", session_id=""):
        self._project_id = project_id
        self._session_id = session_id
        self.remembered = []
        self.cleaned_up = False

    def remember(self, content, date_str=None):
        self.remembered.append(content)
        return Path("/fake/path")

    def cleanup_tool_cache(self):
        self.cleaned_up = True
        return 5

    def read_permanent_memory(self):
        return ""

    def read(self):
        return ""


class MockDreamPipeline:
    def __init__(self):
        self.session_count = 0
        self.last_run = False

    def record_session(self):
        self.session_count += 1

    def should_run(self):
        return self.session_count >= 10

    async def run(self, memory_manager):
        self.last_run = True
        return "MEMORY.md", "CHACHA_MEMORY.md"


# ====== 基本 ======

@pytest.mark.asyncio
async def test_text_only_task():
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=MockLLMInvoker(),
    )
    resp = await orch.run("你好", session_id="s1")
    assert "Hello" in resp.text
    assert resp.iterations == 1
    assert resp.error is None


@pytest.mark.asyncio
async def test_empty_llm_invoker():
    orch = Orchestrator()
    resp = await orch.run("hello", session_id="s1")
    assert "No LLM invoker" in (resp.error or "")


# ====== v2.0: 会话记忆保存 ======

@pytest.mark.asyncio
async def test_save_round_memory():
    memory = MockMemoryManager(project_id="test", session_id="session-001")
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=MockLLMInvoker(),
        memory_manager=memory,
    )

    resp = await orch.run("帮我分析代码", session_id="session-001", project_id="test")
    assert "Hello" in resp.text
    assert len(memory.remembered) >= 1
    entry = memory.remembered[0]
    assert "Q:" in entry
    assert "A:" in entry
    assert "帮我分析代码" in entry
    assert "Hello" in entry


# ====== v2.0: 会话结束清理 ======

@pytest.mark.asyncio
async def test_end_session_cleanup():
    memory = MockMemoryManager(project_id="test", session_id="session-001")
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=MockLLMInvoker(),
        memory_manager=memory,
    )

    await orch.run("hello", session_id="session-001", project_id="test")
    assert memory.cleaned_up is True


# ====== v2.0: DreamPipeline ======

@pytest.mark.asyncio
async def test_dream_record_session_called():
    dream = MockDreamPipeline()
    orch = Orchestrator(
        context_manager=ContextManager(),
        llm_invoker=MockLLMInvoker(),
        dream_pipeline=dream,
    )

    await orch.run("hello", session_id="s1")
    assert dream.session_count == 1


# ====== OrchResponse ======

def test_orch_response_defaults():
    resp = OrchResponse()
    assert resp.text == ""
    assert resp.iterations == 0
    assert resp.total_tokens == 0
    assert resp.error is None


def test_orch_response_with_data():
    resp = OrchResponse(
        text="结果", session_id="s1",
        iterations=3, total_tokens=1500, duration_ms=2000,
    )
    assert resp.text == "结果"
    assert resp.iterations == 3
    assert resp.duration_ms == 2000
