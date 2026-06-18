"""
tests/unit/test_dream.py
单元测试：core/context/dream.py DreamPipeline
"""

import tempfile
from pathlib import Path

import pytest

from core.context.dream import DreamPipeline
from core.context.memory_manager import MemoryManager


# ====== Mock LLMInvoker ======

class MockLLM:
    def __init__(self, text: str = ""):
        self._text = text
        self.calls = []

    async def invoke(self, messages, session_id=""):
        self.calls.append(messages)
        from core.llm_invoker import LLMResponse
        return LLMResponse(text=self._text, finish_reason="stop")


# ====== Fixtures ======

@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d)


# ====== 1. 基础流程 ======

@pytest.mark.asyncio
async def test_run_writes_memory_md(mgr):
    """整合 → MEMORY.md 被写入"""
    mgr.remember("偏好 Python", date_str="2026-01-01")
    llm = MockLLM("## User Preferences\n- Prefers Python")

    pipeline = DreamPipeline(llm)
    result = await pipeline.run(mgr)

    assert result == "## User Preferences\n- Prefers Python"
    assert "Prefers Python" in mgr.read()


# ====== 2. 空记忆 ======

@pytest.mark.asyncio
async def test_run_no_files_returns_empty(mgr):
    """无每日文件 → 返回空"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    result = await pipeline.run(mgr)
    assert result == ""


# ====== 3. Gather ======

def test_gather_reads_daily_files(mgr):
    mgr.remember("偏好 Python", date_str="2026-01-01")
    mgr.remember("项目使用 ruff", date_str="2026-01-01")

    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    raw = pipeline._gather(mgr)

    assert "2026-01-01" in raw
    assert "偏好 Python" in raw
    assert "ruff" in raw


# ====== 4. Prune ======

def test_prune_removes_old_files(mgr):
    """30 天前的文件被删除"""
    # 写入 60 天前的文件
    path = mgr.remember("old memory", date_str="2000-01-01")
    assert path.exists()

    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    deleted = pipeline._prune(mgr)

    assert deleted >= 1
    assert not path.exists()


def test_prune_keeps_recent_files(mgr):
    """最近文件不被删除"""
    mgr.remember("recent", date_str="2026-06-15")
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    pipeline._prune(mgr)

    content = mgr.read_day("2026-06-15")
    assert "recent" in content


# ====== 5. should_run ======

def test_should_run_first_time():
    """首次运行 → True"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    assert pipeline.should_run() is True
