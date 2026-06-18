"""
tests/integration/test_context_manager_integration.py
集成测试：长对话上下文长度控制、压缩触发阈值、缓存复用
"""

import pytest

from core.context_manager import ContextManager
from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent, ObservationEvent,
)
from core.models.config import ContextConfig


def test_long_conversation_context_budget_controlled():
    """长对话（50轮）→ utilization 上升 → needs_compression 或仍在预算内"""
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)

    # 50 轮对话，每轮大量输出
    for i in range(50):
        state.add_event(MessageEvent(source="user", role="user", content=f"task {i}: " + "x" * 200))
        state.add_event(MessageEvent(source="agent", role="assistant", content="OK: " + "y" * 300))
        state.add_event(ObservationEvent(
            source="tool", tool_use_id=f"c{i}",
            content="z" * 500, status="success",
        ))

    mgr = ContextManager(ContextConfig(max_tokens=50000, compression_trigger_ratio=0.8))
    ctx = mgr.assemble(state, session_id="s1")

    # 验证上下文总 token 被统计
    assert ctx.meta.total_tokens > 0
    assert ctx.meta.utilization_ratio > 0
    # 验证来源分布
    assert "history" in ctx.meta.blocks_by_source
    assert "tool_result" in ctx.meta.blocks_by_source
    # 验证消息排序
    msgs = ctx.get_messages()
    assert msgs[0]["role"] == "system"  # system 始终在第一


def test_compression_triggers_above_threshold():
    """超低预算 → 压缩必触发"""
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    # 添加大量消息
    for i in range(100):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 200))

    mgr = ContextManager(ContextConfig(max_tokens=500, compression_trigger_ratio=0.5))
    ctx = mgr.assemble(state, session_id="s1")
    assert ctx.needs_compression is True
    assert ctx.recommended_level in ("frozen", "trimmed", "summarized", "consolidated")


def test_cache_reuse_across_assemblies():
    """多次组装同一静态规则 → 缓存命中"""
    mgr = ContextManager()
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="hello"))

    rules = "缓存测试规则 content"

    # 第一次组装
    ctx1 = mgr.assemble(state, session_id="s1", static_rules=rules, skills="skill")
    # 第二次组装（应命中缓存）
    ctx2 = mgr.assemble(state, session_id="s1", static_rules=rules, skills="skill")

    # 两次组装产生的 system_prompt、static_rule、skill 块数相同
    assert len(ctx1.blocks) == len(ctx2.blocks)
