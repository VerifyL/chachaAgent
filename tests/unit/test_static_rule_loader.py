"""
tests/unit/test_static_rule_loader.py
单元测试：core/context/static_rule_loader.py
"""

import tempfile
from pathlib import Path

import pytest

from core.context.static_rule_loader import StaticRuleLoader

# ====== Fixtures ======


@pytest.fixture
def project_dir():
    d = Path(tempfile.mkdtemp())
    (d / "CHACHA.md").write_text("项目规则: 使用 Python 3.11+", encoding="utf-8")
    (d / "src" / "CHACHA.md").parent.mkdir(parents=True, exist_ok=True)
    (d / "src" / "CHACHA.md").write_text("src 规则: 禁止 print", encoding="utf-8")
    return d


# ====== 1. 分层加载 ======


def test_load_project_only(project_dir):
    loader = StaticRuleLoader(project_dir)
    rules = loader.load()
    assert "项目规则" in rules
    assert "print" not in rules  # src 规则未加载


def test_load_with_sub_dir(project_dir):
    loader = StaticRuleLoader(project_dir)
    rules = loader.load(sub_dir="src")
    assert "项目规则" in rules
    assert "禁止 print" in rules


def test_no_chacha_file():
    d = Path(tempfile.mkdtemp())
    loader = StaticRuleLoader(d)
    rules = loader.load()
    # v2.1: 无 CHACHA.md 时自动返回默认宪法模板，不再为空
    assert "ChachaAgent" in rules
    assert len(rules) > 100


# ====== 2. @import ======


def test_import_directive():
    d = Path(tempfile.mkdtemp())
    (d / "CHACHA.md").write_text(
        "主规则\n@import ./rules/style.md\n结尾",
        encoding="utf-8",
    )
    (d / "rules" / "style.md").parent.mkdir(parents=True, exist_ok=True)
    (d / "rules" / "style.md").write_text("代码风格: Black", encoding="utf-8")

    loader = StaticRuleLoader(d)
    rules = loader.load()
    assert "主规则" in rules
    assert "代码风格: Black" in rules
    assert "结尾" in rules


def test_import_file_not_found(project_dir):
    (project_dir / "CHACHA.md").write_text("@import ./not-exist.md", encoding="utf-8")
    loader = StaticRuleLoader(project_dir)
    rules = loader.load()
    # 不应崩溃，跳过后继续
    assert isinstance(rules, str)


# ====== 3. 异常处理 ======


def test_missing_dir():
    loader = StaticRuleLoader(Path("/no/such/project"))
    rules = loader.load()
    # v2.1: 目录不存在时返回默认宪法模板
    assert "ChachaAgent" in rules
