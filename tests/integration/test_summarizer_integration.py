"""
tests/integration/test_summarizer_integration.py
集成测试：Summarizer + MemoryManager 配合
"""

import tempfile
from pathlib import Path

import pytest

from core.context.summarizer import Summarizer
from core.context.memory_manager import MemoryManager


class MockLLM:
    async def invoke(self, messages, session_id=""):
        from core.llm_invoker import LLMResponse
        return LLMResponse(text="【摘要】关键信息: 用户偏好 Python 3.11")


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d)


@pytest.mark.asyncio
async def test_summarize_memory_then_write(mgr):
    """摘要旧记忆 → 写入新文件"""
    mgr.remember("用户偏好 Python 3.11", date_str="2026-01-01")
    mgr.remember("项目使用 ruff 格式化", date_str="2026-01-01")
    mgr.remember("部署使用 Docker", date_str="2026-01-02")

    # 读取所有记忆
    raw = mgr.read_day("2026-01-01") + "\n" + mgr.read_day("2026-01-02")

    # LLM 摘要
    s = Summarizer(llm_invoker=MockLLM())
    summary = await s.summarize(raw, style="detailed")

    # 写入摘要文件
    mgr.remember(summary, date_str="summary")

    content = mgr.read_day("summary")
    assert "摘要" in content
    assert "Python" in content


@pytest.mark.asyncio
async def test_summarize_blocks(mgr):
    """summarize_blocks 从 ContextBlock 列表生成摘要"""
    from core.models.context import ContextBlock, BlockSource

    blocks = [
        ContextBlock(source=BlockSource.HISTORY, role="user",
                     content="偏好 Python", zone="dynamic", priority=3),
        ContextBlock(source=BlockSource.HISTORY, role="user",
                     content="项目使用 ruff", zone="dynamic", priority=3),
    ]

    s = Summarizer(llm_invoker=MockLLM())
    result = await s.summarize_blocks(blocks, style="brief")
    assert "摘要" in result
