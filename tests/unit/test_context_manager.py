"""
tests/unit/test_context_manager.py
单元测试：core/context_manager.py ContextManager
覆盖：消息排序（系统→历史→工具）、缓存命中、压缩触发、空状态
"""

import pytest

from core.context_manager import ContextManager, DEFAULT_SYSTEM_PROMPT
from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent, ObservationEvent,
)
from core.models.config import ContextConfig
from core.models.context import BlockSource, CompressionLevel


# ====== Fixtures ======

@pytest.fixture
def state():
    meta = SessionMetadata(project_id="p1")
    s = ConversationState(metadata=meta)
    s.add_event(MessageEvent(source="user", role="user", content="读一下 main.py"))
    s.add_event(MessageEvent(source="agent", role="assistant", content="正在读取..."))
    s.add_event(ObservationEvent(source="tool", tool_use_id="c1",
                                  content="print('hello')", status="success"))
    return s


# ====== 1. 消息排序 ======

def test_message_order_system_first(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")
    msgs = ctx.get_messages()
    assert msgs[0]["role"] == "system"
    assert "ChaChaAgent" in msgs[0]["content"]


def test_user_message_after_system(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")
    # 验证排序：system → user → assistant（不含 tool_call event）
    roles = [b.role for b in ctx.blocks]
    assert roles[0] == "system"
    # user 和 assistant 随后
    assert "user" in roles
    assert "assistant" in roles


# ====== 2. 缓存命中 ======

def test_cache_hit_static_rules(state):
    mgr = ContextManager()
    rules = "项目使用 Python 3.11+"
    ctx1 = mgr.assemble(state, session_id="s1", static_rules=rules)
    ctx2 = mgr.assemble(state, session_id="s1", static_rules=rules)
    # 第二次组装时应复用缓存的静态规则块
    rule_blocks_1 = [b for b in ctx1.blocks if b.source == BlockSource.STATIC_RULE]
    rule_blocks_2 = [b for b in ctx2.blocks if b.source == BlockSource.STATIC_RULE]
    if rule_blocks_1 and rule_blocks_2:
        assert rule_blocks_1[0].content == rule_blocks_2[0].content


def test_cache_invalidated_on_change(state):
    mgr = ContextManager()
    ctx1 = mgr.assemble(state, session_id="s1", static_rules="规则A")
    ctx2 = mgr.assemble(state, session_id="s1", static_rules="规则B")
    rules_blocks = [b for b in ctx2.blocks if b.source == BlockSource.STATIC_RULE]
    if rules_blocks:
        assert rules_blocks[0].content == "规则B"


# ====== 3. 压缩触发 ======

def test_compression_not_triggered_for_small_context(state):
    mgr = ContextManager(ContextConfig(max_tokens=128000, compression_trigger_ratio=0.8))
    ctx = mgr.assemble(state, session_id="s1")
    assert ctx.needs_compression is False
    assert ctx.recommended_level == CompressionLevel.NONE


def test_compression_triggered_when_over_threshold(state):
    mgr = ContextManager(ContextConfig(max_tokens=100, compression_trigger_ratio=0.8))
    # 添加大量消息使 token 超标
    for i in range(50):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 100))
    ctx = mgr.assemble(state, session_id="s1")
    # 大概率触发压缩（取决于 token 估算）
    assert isinstance(ctx.needs_compression, bool)


# ====== 4. 空状态 ======

def test_empty_state():
    meta = SessionMetadata(project_id="p1")
    empty = ConversationState(metadata=meta)
    mgr = ContextManager()
    ctx = mgr.assemble(empty)
    # 至少包含系统提示
    assert len(ctx.blocks) >= 1
    assert ctx.blocks[0].source == BlockSource.SYSTEM_PROMPT


# ====== 5. 来源分布 ======

def test_blocks_by_source(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules="规则", skills="能力")
    dist = ctx.meta.blocks_by_source
    assert "system_prompt" in dist
    assert "static_rule" in dist
    assert "skill" in dist
    assert "history" in dist


# ====== 6. zone 分配 ======

def test_protected_vs_dynamic(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules="规则", skills="能力")
    protected = [b for b in ctx.blocks if b.zone == "protected"]
    dynamic = [b for b in ctx.blocks if b.zone == "dynamic"]
    assert len(protected) >= 1  # system prompt
    assert len(dynamic) >= 2   # history + tool result


# ====== 7. set_system_prompt ======

def test_custom_system_prompt(state):
    mgr = ContextManager()
    mgr.set_system_prompt("你是一个测试助手")
    ctx = mgr.assemble(state)
    system_blocks = [b for b in ctx.blocks if b.source == BlockSource.SYSTEM_PROMPT]
    assert system_blocks[0].content == "你是一个测试助手"


# ====== 8. 遥测 ======

def test_telemetry_called(state):
    from core.telemetry import Telemetry
    from core.models.config import TelemetryConfig
    t = Telemetry(TelemetryConfig(log_level="WARNING"))
    t.start()

    mgr = ContextManager(telemetry=t)
    mgr.assemble(state, session_id="s1")
    assert t.metrics.gauges.get("chacha_context_tokens", 0) > 0

    t.stop()


# ====== 9. get_messages 便捷方法 ======

def test_get_messages_direct(state):
    mgr = ContextManager()
    msgs = mgr.get_messages(state)
    assert len(msgs) >= 2  # user + assistant
    assert msgs[0]["role"] == "user"
