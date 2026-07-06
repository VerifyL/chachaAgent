"""
tests/integration/test_memory_integration.py (v2.1)
集成测试：多轮对话 → 记忆写入 → 搜索 → 永久记忆

v2.1 更新:
  - 移除已废弃的 deduplicate/trim/update_entry 测试
  - 使用 _write_to_date 辅助函数适配 remember() 新签名
"""

import tempfile
from pathlib import Path

import pytest

from core.context.memory_manager import MemoryManager


def _write_to_date(mgr, date_str: str, content: str):
    """直接写入指定日期的记忆文件。"""
    path = mgr._session_dir / f"{date_str}.md" if mgr._session_dir else mgr._base / f"{date_str}.md"
    existing = MemoryManager._read(path)
    entry = f"\n## 00:00\n{content.strip()}"
    full = (existing + entry).strip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full, encoding="utf-8")


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="test-integration")


@pytest.fixture
def session_mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-001")


def test_multiple_turns_write_and_search(mgr):
    """模拟多轮对话：写入多条记忆 → 跨日期搜索"""
    _write_to_date(mgr, "2026-06-15", "偏好 Python 3.11 环境")
    _write_to_date(mgr, "2026-06-15", "项目使用 ruff 格式化代码")
    _write_to_date(mgr, "2026-06-16", "部署使用 Docker")
    _write_to_date(mgr, "2026-06-18", "项目使用 ruff 格式化代码")

    result = mgr.search("Python")
    assert "Python 3.11" in result

    result = mgr.search("Docker")
    assert "Docker" in result

    days = mgr.list_days()
    assert "2026-06-18" in days
    assert "2026-06-16" in days
    assert "2026-06-15" in days


def test_search_and_read_full_day(mgr):
    """搜索到摘要 → 读取完整日期文件"""
    _write_to_date(mgr, "2026-06-10", "配置了 Nginx 反向代理")

    result = mgr.search("Nginx")
    assert "Nginx" in result

    full = mgr.read_day("2026-06-10")
    assert "Nginx" in full
    assert len(full) > len(result.split("\n")[0])


# ====== Session 隔离 ======


def test_session_isolation_across_sessions():
    """两个不同 session 的记忆相互独立"""
    d = Path(tempfile.mkdtemp())
    s1 = MemoryManager(project_id="test", base_dir=d, session_id="sess-A")
    s2 = MemoryManager(project_id="test", base_dir=d, session_id="sess-B")

    _write_to_date(s1, "2026-06-18", "sess-A 的记忆")
    _write_to_date(s2, "2026-06-18", "sess-B 的记忆")

    assert "sess-A 的记忆" in s1.read_day("2026-06-18")
    assert "sess-B 的记忆" not in s1.read_day("2026-06-18")
    assert "sess-B 的记忆" in s2.read_day("2026-06-18")
    assert "sess-A 的记忆" not in s2.read_day("2026-06-18")


def test_project_memory_not_in_session():
    """项目级记忆不在 session 目录"""
    d = Path(tempfile.mkdtemp())
    pmgr = MemoryManager(project_id="test", base_dir=d, session_id="proj")
    smgr = MemoryManager(project_id="test", base_dir=d, session_id="sess-A")

    _write_to_date(pmgr, "2026-06-18", "项目配置")
    _write_to_date(smgr, "2026-06-18", "会话对话")

    # session 搜索不会搜到另一个 session 的记忆
    assert "项目配置" not in smgr.search("项目配置")
    assert "会话对话" not in pmgr.search("会话对话")


# ====== 永久记忆 ======


def test_permanent_memory_full_cycle(mgr):
    """永久记忆完整生命周期：写 → 读 → 覆盖 → 不随 prune 删除"""
    mgr.write_permanent_memory("## 关键决策\n- 使用 Python 3.11")
    assert "Python 3.11" in mgr.read_permanent_memory()

    mgr.write_permanent_memory("## 关键决策\n- 升级到 Python 3.12")
    assert "Python 3.12" in mgr.read_permanent_memory()
    assert "Python 3.11" not in mgr.read_permanent_memory()

    _write_to_date(mgr, "2000-01-01", "旧记忆")
    mgr.prune_old_days()
    assert "Python 3.12" in mgr.read_permanent_memory()


def test_permanent_memory_shared_across_sessions():
    """永久记忆是项目级的，跨 session 共享"""
    d = Path(tempfile.mkdtemp())
    mgr1 = MemoryManager(project_id="test", base_dir=d, session_id="s1")
    mgr1.write_permanent_memory("## 共享\n- 项目使用 ruff")

    mgr2 = MemoryManager(project_id="test", base_dir=d, session_id="session-X")
    assert "ruff" in mgr2.read_permanent_memory()


# ====== prune_old_days (7天) ======


def test_prune_7_days_keeps_recent(session_mgr):
    """7天内的文件保留"""
    from datetime import datetime, timedelta, timezone

    for days_ago in range(1, 6):
        date = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        _write_to_date(session_mgr, date, f"day {days_ago}")

    deleted = session_mgr.prune_old_days()
    assert deleted == 0


def test_prune_7_days_removes_old(session_mgr):
    """超过 7 天的文件删除"""
    _write_to_date(session_mgr, "2020-01-01", "very old")
    _write_to_date(session_mgr, "2021-06-15", "also old")

    deleted = session_mgr.prune_old_days()
    assert deleted >= 2


def test_prune_keeps_memory_and_permanent(mgr):
    """prune 不影响 MEMORY.md 和 CHACHA_MEMORY.md"""
    mgr.write_index("## Index\n- preserved")
    mgr.write_permanent_memory("## Permanent\n- forever")

    _write_to_date(mgr, "2000-01-01", "old day")
    mgr.prune_old_days()

    assert "preserved" in mgr.read_index()
    assert "forever" in mgr.read_permanent_memory()
