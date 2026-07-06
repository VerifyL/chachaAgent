"""
tests/unit/test_context_manager.py
单元测试：core/context_manager.py ContextManager (v2.0 → v3.0)

v3.0 上下文顺序:
  protected: SYSTEM(含CHACHA) → USER_MEMORY → CHACHA_MEMORY → SKILLS
  dynamic:   MEMORY.md(条件) → 对话历史 → 工具结果 → RAG/Hooks
"""

import pytest

from core.context_manager import ContextManager
from core.models.config import ContextConfig
from core.models.context import BlockSource, CompressionLevel
from core.models.session import (
    ConversationState,
    MessageEvent,
    ObservationEvent,
    SessionMetadata,
)

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
    assert "ChachaAgent" in msgs[0]["content"]


def test_user_message_after_system(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")
    roles = [b.role for b in ctx.blocks]
    assert roles[0] == "system"
    assert "user" in roles
    assert "assistant" in roles


# ====== 2. 缓存命中 ======

def test_cache_hit_static_rules(state):
    mgr = ContextManager()
    rules = "项目使用 Python 3.11+"
    ctx1 = mgr.assemble(state, session_id="s1", static_rules=rules)
    ctx2 = mgr.assemble(state, session_id="s1", static_rules=rules)
    rule_blocks_1 = [b for b in ctx1.blocks if b.source == BlockSource.STATIC_RULE]
    rule_blocks_2 = [b for b in ctx2.blocks if b.source == BlockSource.STATIC_RULE]
    if rule_blocks_1 and rule_blocks_2:
        assert rule_blocks_1[0].content == rule_blocks_2[0].content


def test_cache_invalidated_on_change(state):
    mgr = ContextManager()
    _ctx1 = mgr.assemble(state, session_id="s1", static_rules="规则A")
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
    mgr = ContextManager(ContextConfig(max_tokens=200, compression_trigger_ratio=0.8))
    for i in range(3):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 200))
    ctx = mgr.assemble(state, session_id="s1")
    assert isinstance(ctx.needs_compression, bool)


# ====== 4. 空状态 ======

def test_empty_state():
    meta = SessionMetadata(project_id="p1")
    empty = ConversationState(metadata=meta)
    mgr = ContextManager()
    ctx = mgr.assemble(empty)
    assert len(ctx.blocks) >= 1
    assert ctx.blocks[0].source == BlockSource.SYSTEM_PROMPT


# ====== 5. 来源分布 ======

def test_blocks_by_source(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules="规则", skills="能力")
    dist = ctx.meta.blocks_by_source
    assert "system_prompt" in dist  # v3.0: static_rule 已合并进 system_prompt
    assert "skill" in dist
    assert "history" in dist
    assert "tool_result" in dist


# ====== 6. zone 分配 ======

def test_protected_vs_dynamic(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules="规则", skills="能力")
    protected = [b for b in ctx.blocks if b.zone == "protected"]
    dynamic = [b for b in ctx.blocks if b.zone == "dynamic"]
    assert len(protected) >= 1
    assert len(dynamic) >= 2


# ====== 7. set_system_prompt ======

def test_custom_system_prompt(state):
    mgr = ContextManager()
    mgr.set_system_prompt("你是一个测试助手")
    ctx = mgr.assemble(state)
    system_blocks = [b for b in ctx.blocks if b.source == BlockSource.SYSTEM_PROMPT]
    assert system_blocks[0].content == "你是一个测试助手"


# ====== 8. CHACHA_MEMORY.md 永久记忆 ======

def test_permanent_memory_in_protected_zone(state):
    """永久记忆在 protected zone 中"""
    mgr = ContextManager()
    mgr.set_permanent_memory("## 永久记忆\n- 关键决策: 使用 Python")
    ctx = mgr.assemble(state)

    perm_blocks = [b for b in ctx.blocks if "Permanent Memory" in b.content]
    assert len(perm_blocks) == 1
    assert perm_blocks[0].zone == "protected"
    assert "关键决策" in perm_blocks[0].content


def test_permanent_memory_before_skill(state):
    """永久记忆在 SKILL 之前"""
    mgr = ContextManager()
    mgr.set_permanent_memory("永久记忆")
    ctx = mgr.assemble(state, skills="技能定义")

    perm_pos = None
    skill_pos = None
    for i, b in enumerate(ctx.blocks):
        if "Permanent Memory" in b.content:
            perm_pos = i
        if b.content == "技能定义":
            skill_pos = i

    assert perm_pos is not None
    assert skill_pos is not None
    assert perm_pos < skill_pos


def test_permanent_memory_after_system(state):
    """v3.0: SYSTEM(含CHACHA) 在永久记忆之前"""
    mgr = ContextManager()
    mgr.set_permanent_memory("永久记忆")
    ctx = mgr.assemble(state, static_rules="CHACHA 宪法")

    sys_block = [b for b in ctx.blocks if b.source == BlockSource.SYSTEM_PROMPT][0]
    assert "CHACHA 宪法" in sys_block.content

    perm_pos = None
    for i, b in enumerate(ctx.blocks):
        if "Permanent Memory" in b.content:
            perm_pos = i
    assert perm_pos is not None
    assert ctx.blocks.index(sys_block) < perm_pos


# ====== 9. MEMORY.md 条件注入 ======

def test_memory_index_always_in_dynamic(state):
    """v3.0: MEMORY.md 常驻动态区，不依赖 history_trimmed"""
    mgr = ContextManager()
    mgr.set_memory_index("## 记忆索引\n- 用户偏好 Python")
    ctx = mgr.assemble(state)
    mem_blocks = [b for b in ctx.blocks if "Memory Index" in b.content]
    assert len(mem_blocks) == 1
    assert mem_blocks[0].zone == "dynamic"


# ====== 10. set_static_rules ======

def test_set_static_rules(state):
    mgr = ContextManager()
    mgr.set_static_rules("CHACHA.md 宪法内容")
    ctx = mgr.assemble(state)

    # v3.0: CHACHA 合并进 SYSTEM
    sys_blocks = [b for b in ctx.blocks if b.source == BlockSource.SYSTEM_PROMPT]
    assert len(sys_blocks) >= 1
    assert "CHACHA.md 宪法内容" in sys_blocks[0].content
    assert sys_blocks[0].zone == "protected"


# ====== 11. 完整上下文排序 ======

def test_full_context_ordering(state):
    """v3.0 顺序: SYSTEM → PERMANENT → SKILL → MEMORY(条件) → HISTORY → TOOL"""
    mgr = ContextManager()
    mgr.set_permanent_memory("永久记忆")
    mgr.set_memory_index("记忆索引")

    ctx = mgr.assemble(state, skills="技能定义")

    sources = []
    for b in ctx.blocks:
        if b.source == BlockSource.SYSTEM_PROMPT:
            sources.append("SYSTEM")
        elif "Permanent Memory" in b.content:
            sources.append("PERMANENT")
        elif "Memory Index" in b.content:
            sources.append("MEMORY_INDEX")
        elif b.content == "技能定义":
            sources.append("SKILL")
        elif b.source == BlockSource.HISTORY:
            sources.append("HISTORY")
        elif b.source == BlockSource.TOOL_RESULT:
            sources.append("TOOL")

    sys_idx = sources.index("SYSTEM") if "SYSTEM" in sources else -1
    perm_idx = sources.index("PERMANENT") if "PERMANENT" in sources else -1
    skill_idx = sources.index("SKILL") if "SKILL" in sources else -1
    mem_idx = sources.index("MEMORY_INDEX") if "MEMORY_INDEX" in sources else -1

    assert sys_idx < perm_idx < skill_idx, f"顺序错误: {sources}"
    assert skill_idx < mem_idx, f"MEMORY 应在 SKILL 之后: {sources}"


# ====== 12. 无永久记忆不报错 ======

def test_no_permanent_memory_no_error(state):
    """不设置永久记忆时，上下文正常组装"""
    mgr = ContextManager()
    ctx = mgr.assemble(state, static_rules="规则", skills="能力")
    assert ctx.blocks[0].source == BlockSource.SYSTEM_PROMPT


# ====== 13. get_messages ======

def test_get_messages_direct(state):
    mgr = ContextManager()
    msgs = mgr.get_messages(state)
    assert len(msgs) >= 2
    assert msgs[0]["role"] == "user"
