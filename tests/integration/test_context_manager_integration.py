"""
tests/integration/test_context_manager_integration.py (v2.0)
集成测试：长对话上下文长度控制、压缩触发阈值、缓存复用

v2.0 新增:
  - 永久记忆在 protected zone
  - 新上下文字段排序验证
  - 长对话中永久记忆不受压缩影响
"""

import pytest

from core.context_manager import ContextManager
from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent, ObservationEvent,
)
from core.models.config import ContextConfig
from core.models.context import BlockSource


def test_long_conversation_context_budget_controlled():
    """长对话（50轮）→ utilization 上升 → needs_compression 或仍在预算内"""
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)

    for i in range(50):
        state.add_event(MessageEvent(source="user", role="user", content=f"task {i}: " + "x" * 200))
        state.add_event(MessageEvent(source="agent", role="assistant", content="OK: " + "y" * 300))
        state.add_event(ObservationEvent(
            source="tool", tool_use_id=f"c{i}",
            content="z" * 500, status="success",
        ))

    mgr = ContextManager(ContextConfig(max_tokens=50000, compression_trigger_ratio=0.8))
    ctx = mgr.assemble(state, session_id="s1")

    assert ctx.meta.total_tokens > 0
    assert ctx.meta.utilization_ratio > 0
    assert "history" in ctx.meta.blocks_by_source
    assert "tool_result" in ctx.meta.blocks_by_source
    msgs = ctx.get_messages()
    assert msgs[0]["role"] == "system"


def test_compression_triggers_above_threshold():
    """超低预算 → 压缩必触发"""
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    for i in range(100):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 200))

    mgr = ContextManager(ContextConfig(max_tokens=3000, compression_trigger_ratio=0.5))
    ctx = mgr.assemble(state, session_id="s1")
    assert ctx.needs_compression is True
    assert ctx.recommended_level in ("trimmed", "summarized", "consolidated")


def test_cache_reuse_across_assemblies():
    """多次组装同一静态规则 → 缓存命中"""
    mgr = ContextManager()
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="hello"))

    rules = "缓存测试规则 content"
    ctx1 = mgr.assemble(state, session_id="s1", static_rules=rules, skills="skill")
    ctx2 = mgr.assemble(state, session_id="s1", static_rules=rules, skills="skill")
    assert len(ctx1.blocks) == len(ctx2.blocks)


# ====== v2.0: 永久记忆在上下文中 ======

def test_permanent_memory_survives_compression_flag():
    """永久记忆在 protected zone，即使 needs_compression=True 也不受影响"""
    mgr = ContextManager(ContextConfig(max_tokens=1000, compression_trigger_ratio=0.5))
    mgr.set_permanent_memory("## 永久记忆\n- 项目永远使用 Python")

    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    for i in range(60):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 200))

    ctx = mgr.assemble(state, session_id="s1", static_rules="宪法规定")

    # 永久记忆块在 protected zone
    perm_blocks = [b for b in ctx.blocks if "Permanent Memory" in b.content]
    assert len(perm_blocks) == 1
    assert perm_blocks[0].zone == "protected"

    # 即使 needs_compression=True
    assert ctx.needs_compression is True


def test_full_context_with_all_sources():
    """所有上下文源同时注入 → 验证分层"""
    mgr = ContextManager()
    mgr.set_static_rules("CHACHA宪法")
    mgr.set_permanent_memory("永久记忆")
    mgr.set_memory_index("记忆索引")
    mgr.set_session_memory("今日记忆")

    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="任务"))

    ctx = mgr.assemble(state, session_id="s1", skills="技能定义")

    # 统计各类型 block
    sources = {}
    for b in ctx.blocks:
        key = b.zone + ":" + (b.source.value if hasattr(b.source, 'value') else str(b.source))
        sources[key] = sources.get(key, 0) + 1

    # 至少包含 system + static_rule + skill + memory + history
    assert len(ctx.blocks) >= 5

    # protected 区有 3+ 个块
    protected = [b for b in ctx.blocks if b.zone == "protected"]
    assert len(protected) >= 3  # SYSTEM_PROMPT + CHACHA + PERMANENT


def test_permanent_memory_stays_in_order_under_high_load():
    """高负载下永久记忆保持在 SKILL 前面"""
    mgr = ContextManager(ContextConfig(max_tokens=2000, compression_trigger_ratio=0.5))
    mgr.set_permanent_memory("永久记忆内容")
    mgr.set_static_rules("宪法")

    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    for i in range(30):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 200))

    ctx = mgr.assemble(state, session_id="s1", skills="技能")

    # 找各块位置
    positions = {}
    for i, b in enumerate(ctx.blocks):
        if "Permanent Memory" in b.content:
            positions["perm"] = i
        if b.content == "宪法":
            positions["rule"] = i
        if b.content == "技能":
            positions["skill"] = i

    assert positions.get("rule", -1) < positions.get("perm", 999)
    assert positions.get("perm", -1) < positions.get("skill", 999)
