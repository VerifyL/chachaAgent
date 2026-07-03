"""
tests/integration/test_static_rule_integration.py
集成测试：真实 CHACHA.md 文件加载 → ContextManager.assemble()
"""

import tempfile
from pathlib import Path

import pytest

from core.context.static_rule_loader import StaticRuleLoader
from core.context_manager import ContextManager
from core.models.session import ConversationState, SessionMetadata, MessageEvent
from core.models.context import BlockSource


# ====== Fixtures ======

@pytest.fixture
def project():
    d = Path(tempfile.mkdtemp())
    (d / "CHACHA.md").write_text(
        "# 项目规则\n使用 Python 3.11+\n@import ./rules/tools.md",
        encoding="utf-8",
    )
    (d / "rules" / "tools.md").parent.mkdir(parents=True, exist_ok=True)
    (d / "rules" / "tools.md").write_text("# 工具规则\n使用 ruff 格式化", encoding="utf-8")
    (d / "src" / "CHACHA.md").parent.mkdir(parents=True, exist_ok=True)
    (d / "src" / "CHACHA.md").write_text("# src 规则\n禁止 print", encoding="utf-8")
    return d


def test_real_chacha_load(project):
    """真实文件结构 → 分层加载含 @import"""
    loader = StaticRuleLoader(project)
    rules = loader.load(sub_dir="src")

    assert "Python 3.11+" in rules  # 项目级
    assert "ruff 格式化" in rules   # @import
    assert "禁止 print" in rules    # src 子目录


def test_context_manager_uses_static_rules(project):
    """加载的规则注入 ContextManager"""
    loader = StaticRuleLoader(project)
    rules = loader.load()

    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="hello"))

    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules=rules)

    # v3.0: static_rules 合并到 SYSTEM_PROMPT block
    system_blocks = [b for b in ctx.blocks if b.source == BlockSource.SYSTEM_PROMPT]
    assert len(system_blocks) == 1
    assert "Python 3.11+" in system_blocks[0].content
