"""
tests/unit/test_dream.py
单元测试：core/context/dream.py DreamPipeline (v2.0)

新增覆盖：
  - 10 次会话 / 24h 触发条件
  - CHACHA_MEMORY.md 双输出
  - _parse_llm_output 解析 MEMORY_MD / CHACHA_MEMORY_MD 分隔符
  - _gather 收集 session 记忆
  - record_session 计数
"""

import tempfile
import time
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
    return MemoryManager(project_id="test", base_dir=d, session_id="test-dream")


# ====== 1. 基础流程 ======

@pytest.mark.asyncio
async def test_run_writes_memory_md(mgr):
    """整合 → MEMORY.md 被写入"""
    mgr.remember("偏好 Python", date_str="2026-01-01")
    llm = MockLLM("===MEMORY_MD===\n## User Preferences\n- Prefers Python\n\n===CHACHA_MEMORY_MD===")

    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = await pipeline.run(mgr)

    assert "Prefers Python" in memory_md
    assert "Prefers Python" in mgr.read()


@pytest.mark.asyncio
async def test_run_writes_permanent_memory(mgr):
    """整合 → CHACHA_MEMORY.md 被写入"""
    mgr.remember("关键决策: 使用 Python", date_str="2026-01-01")
    llm = MockLLM(
        "===MEMORY_MD===\n## Decisions\n- Python\n\n===CHACHA_MEMORY_MD===\n## Critical\n- Project uses Python"
    )

    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = await pipeline.run(mgr)

    assert "Project uses Python" in permanent_md
    assert "Project uses Python" in mgr.read_permanent_memory()


# ====== 2. 空记忆 ======

@pytest.mark.asyncio
async def test_run_no_files_returns_empty(mgr):
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = await pipeline.run(mgr)
    assert memory_md == ""
    assert permanent_md == ""


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


# ====== 4. Prune (7天) ======

def test_prune_removes_old_files(mgr):
    """7 天前的文件被删除"""
    path = mgr.remember("old memory", date_str="2000-01-01")
    assert path.exists()

    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    deleted = pipeline._prune(mgr)

    assert deleted >= 1
    assert not path.exists()


def test_prune_keeps_recent_files(mgr):
    """最近文件不被删除"""
    mgr.remember("recent", date_str="2026-06-22")
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    pipeline._prune(mgr)

    content = mgr.read_day("2026-06-22")
    assert "recent" in content


# ====== 5. 触发条件 ======

def test_should_run_first_time_with_insufficient_sessions():
    """首次运行，不足 10 次会话 → False"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    assert pipeline.should_run() is False


def test_should_run_after_10_sessions():
    """累计 10 次会话 → True"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    for _ in range(10):
        pipeline.record_session()
    assert pipeline.should_run() is True


def test_should_run_after_5_sessions_not_enough():
    """1 次会话（默认 _DREAM_SESSION_COUNT=1）→ True"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    pipeline.record_session()
    assert pipeline.should_run() is True


def test_should_run_after_24_hours():
    """距上次运行 > 24h → True"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm, hours_trigger=0)  # 0小时触发
    pipeline.record_session()
    # 至少需要一次先运行
    pipeline._last_run = 0  # 模拟很久之前运行过
    assert pipeline.should_run() is True


def test_session_count_resets_after_run():
    """运行后 session_count 重置为 0"""
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    for _ in range(10):
        pipeline.record_session()
    assert pipeline.session_count == 10

    # 模拟 run （手动重置）
    pipeline._last_run = time.time()
    pipeline._session_count = 0
    assert pipeline.should_run() is False


# ====== 6. _parse_llm_output ======

def test_parse_standard_format():
    """标准格式：两个标记都存在"""
    text = "===MEMORY_MD===\n## Memory Index\n- entry1\n\n===CHACHA_MEMORY_MD===\n## Permanent\n- forever"
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = pipeline._parse_llm_output(text)

    assert "entry1" in memory_md
    assert "forever" in permanent_md
    assert "===MEMORY_MD===" not in memory_md
    assert "===CHACHA_MEMORY_MD===" not in permanent_md


def test_parse_only_memory_md():
    """只有 MEMORY_MD"""
    text = "===MEMORY_MD===\n## Index\n- entry"
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = pipeline._parse_llm_output(text)

    assert "entry" in memory_md
    assert permanent_md == ""


def test_parse_only_permanent():
    """只有 CHACHA_MEMORY_MD"""
    text = "===CHACHA_MEMORY_MD===\n## Critical\n- important"
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = pipeline._parse_llm_output(text)

    assert memory_md == ""
    assert "important" in permanent_md


def test_parse_fallback_no_markers():
    """无标记 → 全部作为 MEMORY_MD"""
    text = "## User Preferences\n- Python"
    llm = MockLLM()
    pipeline = DreamPipeline(llm)
    memory_md, permanent_md = pipeline._parse_llm_output(text)

    assert "Python" in memory_md
    assert permanent_md == ""


# ====== 7. Consolidate 带旧记忆 ======

@pytest.mark.asyncio
async def test_consolidate_with_old_memory(mgr):
    """旧 MEMORY.md 被传递给 LLM"""
    mgr.update_index("## Old Index\n- old entry")
    mgr.remember("new memory", date_str="2026-06-18")

    llm = MockLLM("===MEMORY_MD===\n## Updated\n- new+old\n\n===CHACHA_MEMORY_MD===")
    pipeline = DreamPipeline(llm)
    memory_md, _ = await pipeline.run(mgr)

    # 验证 LLM 收到了 old + new
    assert len(llm.calls) == 1
    user_msg = llm.calls[0][1]["content"]
    assert "OLD MEMORY.md" in user_msg
    assert "old entry" in user_msg
    assert "new memory" in user_msg


@pytest.mark.asyncio
async def test_consolidate_with_old_permanent(mgr):
    """旧 CHACHA_MEMORY.md 被传递给 LLM"""
    mgr.write_permanent_memory("## Old Permanent\n- old forever")
    mgr.remember("new", date_str="2026-06-18")

    llm = MockLLM(
        "===MEMORY_MD===\n- new\n\n===CHACHA_MEMORY_MD===\n## Permanent\n- old forever\n- new critical"
    )
    pipeline = DreamPipeline(llm)
    _, permanent_md = await pipeline.run(mgr)

    user_msg = llm.calls[0][1]["content"]
    assert "OLD CHACHA_MEMORY.md" in user_msg
    assert "old forever" in user_msg
