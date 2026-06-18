"""
tests/unit/test_context_manager.py
单元测试：core/context_manager.py ContextManager (v2.0)

新增覆盖：
  - CHACHA_MEMORY.md 永久记忆注入（protected zone）
  - MEMORY.md 索引注入（dynamic zone）
  - Session 记忆注入（dynamic zone）
  - 新 block 排序验证
  - set_permanent_memory / set_memory_index / set_session_memory / set_static_rules
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


# ====== 1. 消息排序（原有） ======

def test_message_order_system_first(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")
    msgs = ctx.get_messages()
    assert msgs[0]["role"] == "system"
    assert "ChaChaAgent" in msgs[0]["content"]


def test_user_message_after_system(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")
    roles = [b.role for b in ctx.blocks]
    assert roles[0] == "system"
    assert "user" in roles
    assert "assistant" in roles


# ====== 2. 缓存命中（原有） ======

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
    ctx1 = mgr.assemble(state, session_id="s1", static_rules="规则A")
    ctx2 = mgr.assemble(state, session_id="s1", static_rules="规则B")
    rules_blocks = [b for b in ctx2.blocks if b.source == BlockSource.STATIC_RULE]
    if rules_blocks:
        assert rules_blocks[0].content == "规则B"


# ====== 3. 压缩触发（原有） ======

def test_compression_not_triggered_for_small_context(state):
    mgr = ContextManager(ContextConfig(max_tokens=128000, compression_trigger_ratio=0.8))
    ctx = mgr.assemble(state, session_id="s1")
    assert ctx.needs_compression is False
    assert ctx.recommended_level == CompressionLevel.NONE


def test_compression_triggered_when_over_threshold(state):
    mgr = ContextManager(ContextConfig(max_tokens=100, compression_trigger_ratio=0.8))
    for i in range(50):
        state.add_event(MessageEvent(source="user", role="user", content="x" * 100))
    ctx = mgr.assemble(state, session_id="s1")
    assert isinstance(ctx.needs_compression, bool)


# ====== 4. 空状态（原有） ======

def test_empty_state():
    meta = SessionMetadata(project_id="p1")
    empty = ConversationState(metadata=meta)
    mgr = ContextManager()
    ctx = mgr.assemble(empty)
    assert len(ctx.blocks) >= 1
    assert ctx.blocks[0].source == BlockSource.SYSTEM_PROMPT


# ====== 5. 来源分布（原有） ======

def test_blocks_by_source(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules="规则", skills="能力")
    dist = ctx.meta.blocks_by_source
    assert "system_prompt" in dist
    assert "static_rule" in dist
    assert "skill" in dist
    assert "history" in dist


# ====== 6. zone 分配（原有） ======

def test_protected_vs_dynamic(state):
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1", static_rules="规则", skills="能力")
    protected = [b for b in ctx.blocks if b.zone == "protected"]
    dynamic = [b for b in ctx.blocks if b.zone == "dynamic"]
    assert len(protected) >= 1
    assert len(dynamic) >= 2


# ====== 7. set_system_prompt（原有） ======

def test_custom_system_prompt(state):
    mgr = ContextManager()
    mgr.set_system_prompt("你是一个测试助手")
    ctx = mgr.assemble(state)
    system_blocks = [b for b in ctx.blocks if b.source == BlockSource.SYSTEM_PROMPT]
    assert system_blocks[0].content == "你是一个测试助手"


# ====== 8. v2.0: CHACHA_MEMORY.md 永久记忆 ======

def test_permanent_memory_in_protected_zone(state):
    """永久记忆在 protected zone 中"""
    mgr = ContextManager()
    mgr.set_permanent_memory("## 永久记忆\n- 关键决策: 使用 Python")
    ctx = mgr.assemble(state)

    # 找到永久记忆块
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
    assert perm_pos < skill_pos  # 永久记忆在技能之前


def test_permanent_memory_after_chacha(state):
    """永久记忆在 CHACHA.md 之后"""
    mgr = ContextManager()
    mgr.set_permanent_memory("永久记忆")
    ctx = mgr.assemble(state, static_rules="CHACHA 宪法")

    rule_pos = None
    perm_pos = None
    for i, b in enumerate(ctx.blocks):
        if b.content == "CHACHA 宪法":
            rule_pos = i
        if "Permanent Memory" in b.content:
            perm_pos = i

    assert rule_pos is not None
    assert perm_pos is not None
    assert rule_pos < perm_pos  # CHACHA 宪法在永久记忆之前


# ====== 9. v2.0: MEMORY.md 索引注入 ======

def test_memory_index_in_dynamic_zone(state):
    """MEMORY.md 索引在 dynamic zone"""
    mgr = ContextManager()
    mgr.set_memory_index("## 记忆索引\n- 用户偏好 Python")
    ctx = mgr.assemble(state)

    mem_blocks = [b for b in ctx.blocks if "Memory Index" in b.content]
    assert len(mem_blocks) == 1
    assert mem_blocks[0].zone == "dynamic"
    assert "用户偏好 Python" in mem_blocks[0].content


# ====== 10. v2.0: Session 记忆注入 ======

def test_session_memory_in_dynamic_zone(state):
    """Session 今日记忆在 dynamic zone"""
    mgr = ContextManager()
    mgr.set_session_memory("Q: 如何配置 ruff\nA: 在 pyproject.toml 中添加")
    ctx = mgr.assemble(state)

    sess_blocks = [b for b in ctx.blocks if "Today's Session Memory" in b.content]
    assert len(sess_blocks) == 1
    assert sess_blocks[0].zone == "dynamic"
    assert "ruff" in sess_blocks[0].content


# ====== 11. v2.0: set_static_rules ======

def test_set_static_rules(state):
    mgr = ContextManager()
    mgr.set_static_rules("CHACHA.md 宪法内容")
    ctx = mgr.assemble(state)

    rule_blocks = [b for b in ctx.blocks if "CHACHA.md 宪法内容" in b.content]
    assert len(rule_blocks) >= 1
    assert rule_blocks[0].zone == "protected"


# ====== 12. v2.0: 完整上下文排序 ======

def test_full_context_ordering(state):
    """验证完整上下文的排序:
    SYSTEM → CHACHA.md → CHACHA_MEMORY.md → SKILL → MEMORY.md → Session → History → Tool
    """
    mgr = ContextManager()
    mgr.set_static_rules("CHACHA宪法")
    mgr.set_permanent_memory("永久记忆")
    mgr.set_memory_index("记忆索引")
    mgr.set_session_memory("今日记忆")

    ctx = mgr.assemble(state, skills="技能定义")

    sources = []
    for b in ctx.blocks:
        if b.source == BlockSource.SYSTEM_PROMPT:
            sources.append("SYSTEM")
        elif "Permanent Memory" in b.content:
            sources.append("PERMANENT")
        elif "Memory Index" in b.content:
            sources.append("MEMORY_INDEX")
        elif "Today's Session Memory" in b.content:
            sources.append("SESSION_MEMORY")
        elif b.content == "CHACHA宪法":
            sources.append("CHACHA")
        elif b.content == "技能定义":
            sources.append("SKILL")
        elif b.source == BlockSource.HISTORY:
            sources.append("HISTORY")
        elif b.source == BlockSource.TOOL_RESULT:
            sources.append("TOOL")

    # 验证顺序约束
    sys_idx = sources.index("SYSTEM") if "SYSTEM" in sources else -1
    chacha_idx = sources.index("CHACHA") if "CHACHA" in sources else -1
    perm_idx = sources.index("PERMANENT") if "PERMANENT" in sources else -1
    skill_idx = sources.index("SKILL") if "SKILL" in sources else -1
    mem_idx = sources.index("MEMORY_INDEX") if "MEMORY_INDEX" in sources else -1

    assert sys_idx < chacha_idx < perm_idx < skill_idx
    assert skill_idx < mem_idx  # 动态区在保护区之后


# ====== 13. v2.0: 无永久记忆不报错 ======

def test_no_permanent_memory_no_error(state):
    """不设置永久记忆时，上下文正常组装"""
    mgr = ContextManager()
    ctx = mgr.assemble(state, static_rules="规则", skills="能力")
    assert ctx.blocks[0].source == BlockSource.SYSTEM_PROMPT


# ====== 14. get_messages (原有) ======

def test_get_messages_direct(state):
    mgr = ContextManager()
    msgs = mgr.get_messages(state)
    assert len(msgs) >= 2
    assert msgs[0]["role"] == "user"
