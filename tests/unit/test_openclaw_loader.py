"""
tests/unit/test_openclaw_loader.py
单元测试：capabilities/openclaw_loader.py SkillLoader 骨架
"""

import tempfile
from pathlib import Path

import pytest

from capabilities.openclaw_loader import SkillLoader, SkillPriority


def test_priority_order():
    """优先级数字越小越优先"""
    assert SkillPriority.SYSTEM < SkillPriority.BUILTIN
    assert SkillPriority.BUILTIN < SkillPriority.DISCOVERY


def test_init_creates_dir():
    d = Path(tempfile.mkdtemp()) / "skills"
    SkillLoader(skills_dir=d)
    assert d.exists()


@pytest.mark.asyncio
async def test_load_all_returns_empty():
    """阶段 7 前返回空列表"""
    d = Path(tempfile.mkdtemp())
    loader = SkillLoader(skills_dir=d)
    tools = await loader.load_all("s1")
    assert tools == []
    assert isinstance(tools, list)


def test_six_levels():
    """6 级优先级完整"""
    levels = list(SkillPriority)
    assert len(levels) == 6
    assert SkillPriority(1) == SkillPriority.SYSTEM
    assert SkillPriority(6) == SkillPriority.DISCOVERY
