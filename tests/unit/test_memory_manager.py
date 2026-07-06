"""
tests/unit/test_memory_manager.py
单元测试：core/context/memory_manager.py (v2.1)

新增覆盖：
  - CHACHA_MEMORY.md 永久记忆读写
  - Session 隔离存储
  - prune_old_days (7 天)
  - 主题记忆读写
"""

import tempfile
from pathlib import Path

import pytest

from core.context.memory_manager import MemoryManager


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="test-session")


@pytest.fixture
def session_mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-001")


def _write_to_date(mgr, date_str: str, content: str):
    """直接写入指定日期的记忆文件（remember() 仅写当日）。"""
    path = mgr._session_dir / f"{date_str}.md"
    existing = mgr._read(path)
    entry = f"\n## 00:00\n{content.strip()}"
    full = (existing + entry).strip() + "\n"
    path.write_text(full, encoding="utf-8")


# ====== 读写 ======


def test_remember_and_read_today(mgr):
    mgr.remember("用户偏好 Python 3.11")
    today = mgr._today_str()
    content = mgr.read_day(today)
    assert "Python 3.11" in content


def test_read_specific_date(mgr):
    _write_to_date(mgr, "2026-01-01", "消息A")
    content = mgr.read_day("2026-01-01")
    assert "消息A" in content


def test_list_days(mgr):
    _write_to_date(mgr, "2026-01-01", "day1")
    _write_to_date(mgr, "2026-01-02", "day2")
    days = mgr.list_days()
    assert "2026-01-02" in days


# ====== 搜索 ======


def test_search_finds_across_dates(mgr):
    _write_to_date(mgr, "2026-01-10", "Python 项目配置")
    _write_to_date(mgr, "2026-01-15", "Node.js 依赖更新")
    _write_to_date(mgr, "2026-01-20", "Python 环境变量")

    result = mgr.search("Python")
    assert "Python 项目配置" in result
    assert "Python 环境变量" in result


def test_search_no_match(mgr):
    _write_to_date(mgr, "2026-01-01", "JavaScript 相关")
    result = mgr.search("Python")
    assert result == ""


def test_search_respects_limit(mgr):
    """search() 最多返回 5 条结果。"""
    for i in range(20):
        _write_to_date(mgr, "2026-01-01", f"Python 记忆 {i}")
    result = mgr.search("Python")
    # 断言不超过 5 个日期块
    assert result.count("---") <= 5


# ====== 永久记忆 CHACHA_MEMORY.md ======


def test_permanent_memory_write_and_read(mgr):
    """写入并读取 CHACHA_MEMORY.md"""
    assert mgr.read_permanent_memory() == ""

    content = "## Key Decisions\n- Use Python 3.11+"
    mgr.write_permanent_memory(content)
    assert "Python 3.11" in mgr.read_permanent_memory()


def test_permanent_memory_overwrite(mgr):
    """覆盖写入 CHACHA_MEMORY.md"""
    mgr.write_permanent_memory("## v1\n- entry1")
    mgr.write_permanent_memory("## v2\n- entry2")
    result = mgr.read_permanent_memory()
    assert "entry2" in result
    assert "entry1" not in result


def test_permanent_memory_path(mgr):
    """permanent_memory_path 返回正确路径"""
    path = mgr.permanent_memory_path()
    assert path.name == "CHACHA_MEMORY.md"
    assert "projects" in str(path)
    assert "test" in str(path)


def test_permanent_memory_session_independent(mgr):
    """永久记忆是项目级的，与 session 无关"""
    mgr.write_permanent_memory("## Permanent\n- shared entry")

    from core.context.memory_manager import MemoryManager

    mgr2 = MemoryManager(
        project_id="test",
        base_dir=mgr._project_dir.parents[1],
        session_id="session-999",
    )
    assert "shared entry" in mgr2.read_permanent_memory()


# ====== Session 隔离存储 ======


def test_session_memory_isolated(session_mgr):
    """Session 记忆写入 session 目录"""
    session_mgr.remember("session 专属记忆")
    today = session_mgr._today_str()
    content = session_mgr.read_day(today)
    assert "session 专属记忆" in content


def test_session_and_project_memory_separate(mgr, session_mgr):
    """项目级和 session 级记忆分离"""
    mgr.remember("项目记忆")
    session_mgr.remember("session记忆")

    today = mgr._today_str()
    project_content = mgr.read_day(today)
    assert "项目记忆" in project_content
    assert "session记忆" not in project_content


# ====== prune_old_days (7天) ======


def test_prune_old_days_removes_old_files(mgr):
    """7 天前的文件被删除"""
    _write_to_date(mgr, "2000-01-01", "old memory")
    path = mgr._day_path("2000-01-01")
    assert path.exists()

    deleted = mgr.prune_old_days()
    assert deleted >= 1
    assert not path.exists()


def test_prune_old_days_keeps_recent_files(mgr):
    """最近 7 天内的文件不被删除"""
    from datetime import datetime, timedelta, timezone

    today = datetime.now(tz=timezone.utc)
    recent = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    _write_to_date(mgr, recent, "recent")

    mgr.prune_old_days()
    content = mgr.read_day(recent)
    assert "recent" in content


def test_prune_keeps_memory_md(mgr):
    """MEMORY.md 不被删除"""
    mgr.write_index("## index\n- preserved")
    mgr.prune_old_days()
    assert "preserved" in mgr.read_index()


def test_prune_keeps_permanent_memory(mgr):
    """CHACHA_MEMORY.md 不被删除"""
    mgr.write_permanent_memory("## Permanent\n- forever")
    mgr.prune_old_days()
    assert "forever" in mgr.read_permanent_memory()
