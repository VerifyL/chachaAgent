"""
tests/integration/test_summarizer_integration.py
集成测试：Summarizer + MemoryManager 配合 (v2.1)
"""

import tempfile
from pathlib import Path

import pytest

from core.context.summarizer import Summarizer
from core.context.memory_manager import MemoryManager


def _write_to_date(mgr, date_str: str, content: str):
    """直接写入指定日期的记忆文件。"""
    path = mgr._session_dir / f"{date_str}.md" if mgr._session_dir else mgr._base / f"{date_str}.md"
    existing = MemoryManager._read(path)
    entry = f"\n## 00:00\n{content.strip()}"
    full = (existing + entry).strip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full, encoding="utf-8")


class MockLLM:
    async def invoke(self, messages, session_id=""):
        from core.llm_invoker import LLMResponse
        return LLMResponse(text="【摘要】关键信息: 用户偏好 Python 3.11")


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="test-summarizer")


@pytest.mark.asyncio
async def test_summarize_memory_then_write(mgr):
    """摘要旧记忆 → 写入新文件"""
    _write_to_date(mgr, "2026-01-01", "用户偏好 Python 3.11")
    _write_to_date(mgr, "2026-01-01", "项目使用 ruff 格式化")
    _write_to_date(mgr, "2026-01-02", "部署使用 Docker")

    raw = mgr.read_day("2026-01-01") + "\n" + mgr.read_day("2026-01-02")

    s = Summarizer(llm_invoker=MockLLM())
    summary = await s.summarize(raw, style="detailed")

    # 写入摘要到今日记忆
    mgr.remember(summary)

    today = mgr._today_str()
    content = mgr.read_day(today)
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
