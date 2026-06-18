"""
tests/integration/test_memory_integration.py (v2.0)
集成测试：多轮对话 → 记忆写入 → 搜索 → 去重 → 永久记忆

v2.0 新增:
  - Session 隔离：多 session 记忆相互独立
  - CHACHA_MEMORY.md 永久记忆读写
  - tool_cache 缓存/读取/清理
  - prune_old_days (7天)
"""

import tempfile
from pathlib import Path

import pytest

from core.context.memory_manager import MemoryManager


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d)


@pytest.fixture
def session_mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-001")


def test_multiple_turns_write_and_search(mgr):
    """模拟多轮对话：写入多条记忆 → 跨日期搜索"""
    mgr.remember("偏好 Python 3.11 环境", date_str="2026-06-15")
    mgr.remember("项目使用 ruff 格式化代码", date_str="2026-06-15")
    mgr.remember("部署使用 Docker", date_str="2026-06-16")
    mgr.remember("项目使用 ruff 格式化代码", date_str="2026-06-18")

    result = mgr.search("Python")
    assert "Python 3.11" in result

    result = mgr.search("Docker")
    assert "Docker" in result

    days = mgr.list_days()
    assert "2026-06-18" in days
    assert "2026-06-16" in days
    assert "2026-06-15" in days


def test_dedup_after_multiple_saves(mgr):
    """多次保存相同内容 → 去重后只剩一条"""
    date = "2026-06-15"
    mgr.remember("偏好 ruff", date_str=date)
    mgr.remember("偏好 ruff", date_str=date)
    mgr.remember("偏好 ruff", date_str=date)

    removed = mgr.deduplicate(date_str=date)
    assert removed == 2

    content = mgr.read_day(date)
    assert content.count("偏好 ruff") == 1


def test_search_and_read_full_day(mgr):
    """搜索到摘要 → 读取完整日期文件"""
    mgr.remember("配置了 Nginx 反向代理", date_str="2026-06-10")

    result = mgr.search("Nginx")
    assert "Nginx" in result

    full = mgr.read_day("2026-06-10")
    assert "Nginx" in full
    assert len(full) > len(result.split("\n")[0])


# ====== v2.0: Session 隔离 ======

def test_session_isolation_across_sessions():
    """两个不同 session 的记忆相互独立"""
    d = Path(tempfile.mkdtemp())
    s1 = MemoryManager(project_id="test", base_dir=d, session_id="sess-A")
    s2 = MemoryManager(project_id="test", base_dir=d, session_id="sess-B")

    s1.remember("sess-A 的记忆", date_str="2026-06-18")
    s2.remember("sess-B 的记忆", date_str="2026-06-18")

    # 各自只能看到自己的
    assert "sess-A 的记忆" in s1.read_day("2026-06-18")
    assert "sess-B 的记忆" not in s1.read_day("2026-06-18")
    assert "sess-B 的记忆" in s2.read_day("2026-06-18")
    assert "sess-A 的记忆" not in s2.read_day("2026-06-18")


def test_project_memory_not_in_session():
    """项目级记忆不在 session 目录"""
    d = Path(tempfile.mkdtemp())
    pmgr = MemoryManager(project_id="test", base_dir=d)
    smgr = MemoryManager(project_id="test", base_dir=d, session_id="sess-A")

    pmgr.remember("项目配置", date_str="2026-06-18")
    smgr.remember("会话对话", date_str="2026-06-18")

    # session 搜索不会搜到项目级记忆（除非 across_sessions）
    assert "项目配置" not in smgr.search("项目配置", across_sessions=False)
    assert "会话对话" not in pmgr.search("会话对话", across_sessions=False)


# ====== v2.0: 永久记忆 ======

def test_permanent_memory_full_cycle(mgr):
    """永久记忆完整生命周期：写 → 读 → 覆盖 → 不随 prune 删除"""
    # 写
    mgr.write_permanent_memory("## 关键决策\n- 使用 Python 3.11")

    # 读
    assert "Python 3.11" in mgr.read_permanent_memory()

    # 覆盖
    mgr.write_permanent_memory("## 关键决策\n- 升级到 Python 3.12")

    assert "Python 3.12" in mgr.read_permanent_memory()
    assert "Python 3.11" not in mgr.read_permanent_memory()

    # 添加旧记忆并 prune
    mgr.remember("旧记忆", date_str="2000-01-01")
    mgr.prune_old_days()

    # 永久记忆仍在
    assert "Python 3.12" in mgr.read_permanent_memory()


def test_permanent_memory_shared_across_sessions():
    """永久记忆是项目级的，跨 session 共享"""
    d = Path(tempfile.mkdtemp())
    mgr1 = MemoryManager(project_id="test", base_dir=d)
    mgr1.write_permanent_memory("## 共享\n- 项目使用 ruff")

    # 不同 session 也能读到
    mgr2 = MemoryManager(project_id="test", base_dir=d, session_id="session-X")
    assert "ruff" in mgr2.read_permanent_memory()


# ====== v2.0: tool_cache ======

def test_tool_cache_write_and_read(session_mgr):
    """缓存工具结果 → 可读取"""
    session_mgr.cache_tool_result(
        tool_use_id="c1", tool_name="read_file",
        result="print('hello')",
    )
    session_mgr.cache_tool_result(
        tool_use_id="c2", tool_name="grep",
        result="found 42 matches",
    )

    # 缓存目录非空
    files = list(session_mgr.tool_cache_dir.iterdir())
    assert len(files) >= 2


def test_tool_cache_cleanup(session_mgr):
    """清理后目录为空"""
    session_mgr.cache_tool_result("c1", "read", "data1")
    session_mgr.cache_tool_result("c2", "grep", "data2")

    count = session_mgr.cleanup_tool_cache()
    assert count == 2
    assert not any(session_mgr.tool_cache_dir.iterdir())


# ====== v2.0: prune_old_days (7天) ======

def test_prune_7_days_keeps_recent(session_mgr):
    """7天内的文件保留"""
    from datetime import datetime, timezone, timedelta
    for days_ago in range(1, 6):
        date = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        session_mgr.remember(f"day {days_ago}", date_str=date)

    deleted = session_mgr.prune_old_days()
    assert deleted == 0  # 都在 7 天内


def test_prune_7_days_removes_old(session_mgr):
    """超过 7 天的文件删除"""
    session_mgr.remember("very old", date_str="2020-01-01")
    session_mgr.remember("also old", date_str="2021-06-15")

    deleted = session_mgr.prune_old_days()
    # 至少删除了这两个
    assert deleted >= 2


def test_prune_keeps_memory_and_permanent(session_mgr):
    """prune 不影响 MEMORY.md 和 CHACHA_MEMORY.md"""
    # 通过项目级 mgr 写永久记忆
    d = session_mgr._project_dir.parents[2]  # root
    pmgr = MemoryManager(project_id="test", base_dir=d)
    pmgr.update_index("## Index\n- preserved")
    pmgr.write_permanent_memory("## Permanent\n- forever")

    pmgr.remember("old day", date_str="2000-01-01")
    pmgr.prune_old_days()

    assert "preserved" in pmgr.read()
    assert "forever" in pmgr.read_permanent_memory()
