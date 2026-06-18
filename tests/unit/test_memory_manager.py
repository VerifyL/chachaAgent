"""
tests/unit/test_memory_manager.py
单元测试：core/context/memory_manager.py
"""

import tempfile
from pathlib import Path

import pytest

from core.context.memory_manager import MemoryManager


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d)


# ====== 读写 ======

def test_remember_and_read_today(mgr):
    mgr.remember("用户偏好 Python 3.11")
    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    content = mgr.read_day(today)
    assert "Python 3.11" in content


def test_read_specific_date(mgr):
    mgr.remember("消息A", date_str="2026-01-01")
    content = mgr.read_day("2026-01-01")
    assert "消息A" in content


def test_list_days(mgr):
    mgr.remember("day1", date_str="2026-01-01")
    mgr.remember("day2", date_str="2026-01-02")
    days = mgr.list_days()
    assert "2026-01-02" in days


# ====== 搜索 ======

def test_search_finds_across_dates(mgr):
    mgr.remember("Python 项目配置", date_str="2026-01-10")
    mgr.remember("Node.js 依赖更新", date_str="2026-01-15")
    mgr.remember("Python 环境变量", date_str="2026-01-20")

    result = mgr.search("Python")
    assert "Python 项目配置" in result
    assert "Python 环境变量" in result


def test_search_no_match(mgr):
    mgr.remember("JavaScript 相关", date_str="2026-01-01")
    result = mgr.search("Python")
    assert result == ""


def test_search_respects_limit(mgr):
    for i in range(20):
        mgr.remember(f"Python 记忆 {i}", date_str="2026-01-01")
    result = mgr.search("Python", limit=3, max_chars=10000)
    matched = [line for line in result.split("\n") if line and not line.startswith("[")]
    assert len(matched) <= 3


# ====== 去重 ======

def test_deduplicate_removes_duplicates(mgr):
    date = "2026-01-01"
    mgr.remember("偏好 Python", date_str=date)
    mgr.remember("偏好 Python", date_str=date)
    mgr.remember("项目路径 /home/user", date_str=date)

    removed = mgr.deduplicate(date_str=date)
    assert removed >= 1
    content = mgr.read_day(date)
    assert content.count("偏好 Python") == 1


# ====== 裁剪 ======

def test_trim_reduces_lines(mgr):
    date = "2026-01-01"
    for i in range(200):
        mgr.remember(f"行 {i}", date_str=date)

    removed = mgr.trim(date_str=date, keep_lines=50)
    assert removed > 0
    content = mgr.read_day(date)
    assert content.count("\n") <= 50


# ====== 更新 ======

def test_update_entry(mgr):
    date = "2026-01-01"
    mgr.remember("偏好 black", date_str=date)
    ok = mgr.update_entry("black", "偏好 ruff", date_str=date)
    assert ok is True
    content = mgr.read_day(date)
    assert "ruff" in content
    assert "black" not in content


def test_update_entry_not_found(mgr):
    ok = mgr.update_entry("不存在", "new", date_str="2026-01-01")
    assert ok is False
