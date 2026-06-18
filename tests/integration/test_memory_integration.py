"""
tests/integration/test_memory_integration.py
集成测试：多轮对话 → 记忆写入 → 搜索 → 去重
"""

import tempfile
from pathlib import Path

import pytest

from core.context.memory_manager import MemoryManager


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d)


def test_multiple_turns_write_and_search(mgr):
    """模拟多轮对话：写入多条记忆 → 跨日期搜索"""
    # 第 1 轮：用户提了两个偏好
    mgr.remember("偏好 Python 3.11 环境", date_str="2026-06-15")
    mgr.remember("项目使用 ruff 格式化代码", date_str="2026-06-15")

    # 第 2 轮：用户说了部署偏好
    mgr.remember("部署使用 Docker", date_str="2026-06-16")

    # 第 3 轮：重复了之前的信息
    mgr.remember("项目使用 ruff 格式化代码", date_str="2026-06-18")

    # 验证搜索
    result = mgr.search("Python")
    assert "Python 3.11" in result

    result = mgr.search("Docker")
    assert "Docker" in result

    # 验证列表
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
    assert removed == 2  # 3 条相同 → 保留 1 条

    content = mgr.read_day(date)
    assert content.count("偏好 ruff") == 1


def test_search_and_read_full_day(mgr):
    """搜索到摘要 → 读取完整日期文件"""
    mgr.remember("配置了 Nginx 反向代理", date_str="2026-06-10")

    # 搜索找到
    result = mgr.search("Nginx")
    assert "Nginx" in result

    # 读取完整日期
    full = mgr.read_day("2026-06-10")
    assert "Nginx" in full
    assert len(full) > len(result.split("\n")[0])  # 完整 > 摘要
