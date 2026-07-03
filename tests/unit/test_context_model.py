"""
tests/unit/test_context_model.py
单元测试：core/models/context.py 上下文组装结果模型
覆盖：ContextBlock 构造/校验、CompressionLevel 枚举、
       AssembledContext 统计自动计算、get_messages/get_protected/get_dynamic、
       序列化往返、边界条件
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.models.context import (
    AssembledContext,
    BlockSource,
    CompressionLevel,
    ContextAssemblyMeta,
    ContextBlock,
    TriggerReason,
)


# ========== 1. 枚举测试 ==========

def test_block_source_values():
    assert BlockSource.SYSTEM_PROMPT == "system_prompt"
    assert BlockSource.STATIC_RULE == "static_rule"
    assert BlockSource.MEMORY == "memory"
    assert BlockSource.HISTORY == "history"
    assert BlockSource.TOOL_RESULT == "tool_result"
    assert BlockSource.SKILL == "skill"
    assert BlockSource.RAG_RESULT == "rag_result"
    assert BlockSource.SUBAGENT_RESULT == "subagent_result"
    assert BlockSource.ADDITIONAL_CONTEXT == "additional_context"
    assert len(BlockSource) == 9


def test_compression_level_order():
    """压缩层级应按激进程度递增"""
    levels = list(CompressionLevel)
    assert levels == [
        CompressionLevel.NONE,
        CompressionLevel.FROZEN,
        CompressionLevel.TRIMMED,
        CompressionLevel.SUMMARIZED,
        CompressionLevel.CONSOLIDATED,
    ]


def test_trigger_reason_values():
    assert TriggerReason.NONE == "none"
    assert TriggerReason.THRESHOLD == "threshold"
    assert TriggerReason.TIME_GATE == "time_gate"
    assert TriggerReason.SESSION_GATE == "session_gate"
    assert TriggerReason.MANUAL == "manual"


# ========== 2. ContextBlock 测试 ==========

class TestContextBlock:
    def test_minimal_block(self):
        b = ContextBlock(source=BlockSource.HISTORY, role="user", content="hello")
        assert b.zone == "dynamic"
        assert b.compression_level == "none"
        assert b.importance_score == 0.5
        assert b.token_count == 0
        assert b.cache_ttl is None
        assert b.original_token_count is None

    def test_protected_block(self):
        b = ContextBlock(
            source=BlockSource.SYSTEM_PROMPT,
            role="system",
            content="You are a helpful assistant.",
            zone="protected",
            priority=0,
            importance_score=1.0,
            cache_ttl=600,
        )
        assert b.zone == "protected"
        assert b.cache_ttl == 600
        assert b.importance_score == 1.0

    def test_frozen_block_with_original_tokens(self):
        b = ContextBlock(
            source=BlockSource.TOOL_RESULT,
            role="tool",
            content="[output truncated]",
            compression_level=CompressionLevel.FROZEN,
            original_token_count=5000,
            token_count=200,
            persisted_path="compressed/s1/b-abc.json",
            frozen_kept_lines=8,
            frozen_total_lines=134,
        )
        assert b.compression_level == "frozen"
        assert b.original_token_count == 5000
        assert b.token_count == 200
        assert b.persisted_path == "compressed/s1/b-abc.json"
        assert b.frozen_kept_lines == 8
        assert b.frozen_total_lines == 134

    def test_compression_history_prevents_recompression(self):
        b = ContextBlock(
            source=BlockSource.TOOL_RESULT,
            role="tool",
            content="summary",
            compression_level=CompressionLevel.SUMMARIZED,
            compression_history=["NONE→FROZEN", "FROZEN→SUMMARIZED"],
            original_token_count=5000,
            token_count=100,
        )
        assert len(b.compression_history) == 2
        # ContextCompressor 检查 compression_history 非空且已到终态 → 跳过

    def test_content_hash_for_caching(self):
        b = ContextBlock(
            source=BlockSource.TOOL_RESULT,
            role="tool",
            content="hello" * 1000,
            content_hash="abc123def456",
        )
        assert b.content_hash == "abc123def456"

    def test_new_block_sources(self):
        """新增来源类型构造正确"""
        skill_block = ContextBlock(
            source=BlockSource.SKILL, role="system",
            content="skill: code_review", zone="protected",
            priority=1, importance_score=0.9, cache_ttl=1200,
        )
        assert skill_block.source == "skill"
        assert skill_block.cache_ttl == 1200

        rag_block = ContextBlock(
            source=BlockSource.RAG_RESULT, role="tool",
            content="search result: def main()", cache_ttl=120,
        )
        assert rag_block.source == "rag_result"
        assert rag_block.cache_ttl == 120

        sub_block = ContextBlock(
            source=BlockSource.SUBAGENT_RESULT, role="assistant",
            content="子Agent 完成: 修复了 3 个 bug", importance_score=0.8,
        )
        assert sub_block.source == "subagent_result"
        assert sub_block.importance_score == 0.8

    def test_importance_score_boundary(self):
        ContextBlock(source=BlockSource.HISTORY, role="user", content="x", importance_score=0.0)
        ContextBlock(source=BlockSource.HISTORY, role="user", content="x", importance_score=1.0)
        with pytest.raises(ValidationError):
            ContextBlock(source=BlockSource.HISTORY, role="user", content="x", importance_score=1.1)
        with pytest.raises(ValidationError):
            ContextBlock(source=BlockSource.HISTORY, role="user", content="x", importance_score=-0.1)

    def test_negative_token_count_rejected(self):
        with pytest.raises(ValidationError):
            ContextBlock(source=BlockSource.HISTORY, role="user", content="x", token_count=-1)

    def test_frozen_immutable(self):
        b = ContextBlock(source=BlockSource.HISTORY, role="user", content="hello")
        with pytest.raises(ValidationError):
            b.content = "new"

    def test_serialization_roundtrip(self):
        b = ContextBlock(
            source=BlockSource.MEMORY,
            role="system",
            content="用户偏好：中文回复",
            zone="dynamic",
            priority=2,
            compression_level=CompressionLevel.TRIMMED,
            original_token_count=150,
            token_count=80,
            importance_score=0.75,
            cache_ttl=300,
        )
        j = b.model_dump_json()
        restored = ContextBlock.model_validate_json(j)
        assert restored.source == "memory"
        assert restored.compression_level == "trimmed"
        assert restored.importance_score == 0.75
        assert restored.token_count == 80

    def test_block_source_is_required(self):
        with pytest.raises(ValidationError):
            ContextBlock(role="user", content="hello")

    def test_created_at_is_utc(self):
        b = ContextBlock(source=BlockSource.HISTORY, role="user", content="hi")
        assert b.created_at.tzinfo is not None


# ========== 3. ContextAssemblyMeta 测试 ==========

class TestContextAssemblyMeta:
    def test_defaults(self):
        meta = ContextAssemblyMeta()
        assert meta.total_tokens == 0
        assert meta.utilization_ratio == 0.0
        assert meta.compression_pressure == 0.0
        assert meta.trigger_reason == "none"
        assert meta.budget_per_request == 128000
        assert meta.reasoning_budget_tokens == 0

    def test_full_meta(self):
        meta = ContextAssemblyMeta(
            session_id="s1",
            project_id="p1",
            trigger="compression",
            total_tokens=100000,
            protected_tokens=8000,
            dynamic_tokens=92000,
            budget_per_request=128000,
            budget_per_task=1000000,
            utilization_ratio=0.78,
            compression_pressure=0.72,
            trigger_reason=TriggerReason.THRESHOLD,
            reasoning_budget_tokens=32000,
            reasoning_tokens_used=15000,
            blocks_by_source={
                "system_prompt": 2000,
                "history": 50000,
                "tool_result": 48000,
            },
        )
        assert meta.utilization_ratio == 0.78
        assert meta.compression_pressure == 0.72
        assert meta.reasoning_tokens_used == 15000
        assert meta.blocks_by_source["history"] == 50000

    def test_utilization_over_limit(self):
        """utilization_ratio > 1 表示已超限"""
        meta = ContextAssemblyMeta(
            total_tokens=150000,
            budget_per_request=128000,
            utilization_ratio=1.172,
        )
        assert meta.utilization_ratio > 1.0

    def test_frozen(self):
        meta = ContextAssemblyMeta()
        with pytest.raises(ValidationError):
            meta.total_tokens = 100

    def test_negative_budget_rejected(self):
        with pytest.raises(ValidationError):
            ContextAssemblyMeta(budget_per_request=-1)

    def test_serialization(self):
        meta = ContextAssemblyMeta(
            session_id="s1",
            total_tokens=50000,
            budget_per_request=128000,
            utilization_ratio=0.39,
            compression_pressure=0.3,
            trigger_reason=TriggerReason.NONE,
        )
        j = meta.model_dump_json()
        restored = ContextAssemblyMeta.model_validate_json(j)
        assert restored.total_tokens == 50000
        assert restored.utilization_ratio == 0.39
        assert restored.trigger_reason == "none"


# ========== 4. AssembledContext 测试 ==========

class TestAssembledContext:
    @pytest.fixture
    def sample_blocks(self):
        return [
            ContextBlock(
                source=BlockSource.SYSTEM_PROMPT,
                role="system",
                content="You are ChaChaAgent.",
                zone="protected",
                priority=0,
                importance_score=1.0,
                token_count=50,
            ),
            ContextBlock(
                source=BlockSource.HISTORY,
                role="user",
                content="帮我读一下 main.py",
                zone="dynamic",
                priority=3,
                importance_score=0.8,
                token_count=30,
            ),
            ContextBlock(
                source=BlockSource.HISTORY,
                role="assistant",
                content="好的，正在读取...",
                zone="dynamic",
                priority=3,
                importance_score=0.7,
                token_count=25,
            ),
            ContextBlock(
                source=BlockSource.TOOL_RESULT,
                role="tool",
                content="print('hello')",
                zone="dynamic",
                priority=4,
                importance_score=0.6,
                token_count=15,
                compression_level=CompressionLevel.FROZEN,
                original_token_count=500,
            ),
        ]

    @pytest.fixture
    def sample_meta(self):
        return ContextAssemblyMeta(
            session_id="s1",
            total_tokens=120,
            protected_tokens=50,
            dynamic_tokens=70,
            blocks_by_source={
                "system_prompt": 50,
                "history": 55,
                "tool_result": 15,
            },
        )

    def test_empty_context(self):
        ctx = AssembledContext.empty()
        assert ctx.blocks == []
        assert ctx.needs_compression is False
        assert ctx.recommended_level == CompressionLevel.NONE
        assert ctx.meta.total_tokens == 0

    def test_get_messages_order(self, sample_blocks, sample_meta):
        ctx = AssembledContext(blocks=sample_blocks, meta=sample_meta)
        msgs = ctx.get_messages()
        # 按 priority 排序：system(0) → history(3) → history(3) → tool(4)
        assert msgs[0]["role"] == "system"
        assert msgs[3]["role"] == "tool"
        assert len(msgs) == 4

    def test_get_messages_skips_empty_content(self, sample_meta):
        blocks = [
            ContextBlock(source=BlockSource.SYSTEM_PROMPT, role="system", content="   ", priority=0),
            ContextBlock(source=BlockSource.HISTORY, role="user", content="hello", priority=3),
        ]
        ctx = AssembledContext(blocks=blocks, meta=sample_meta)
        msgs = ctx.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    def test_get_protected_slice(self, sample_blocks, sample_meta):
        ctx = AssembledContext(blocks=sample_blocks, meta=sample_meta)
        protected = ctx.get_protected_slice()
        assert len(protected) == 1
        assert protected[0].source == "system_prompt"

    def test_get_dynamic_slice_order(self, sample_blocks, sample_meta):
        ctx = AssembledContext(blocks=sample_blocks, meta=sample_meta)
        dynamic = ctx.get_dynamic_slice()
        assert len(dynamic) == 3
        # 按 importance_score 降序：0.8 → 0.7 → 0.6
        assert dynamic[0].importance_score == 0.8
        assert dynamic[-1].importance_score == 0.6

    def test_needs_compression_false(self, sample_meta):
        ctx = AssembledContext(blocks=[], meta=sample_meta)
        assert ctx.needs_compression is False
        assert ctx.recommended_level == CompressionLevel.NONE

    def test_needs_compression_true(self):
        meta = ContextAssemblyMeta(
            total_tokens=110000,
            budget_per_request=128000,
            utilization_ratio=0.86,
            compression_pressure=0.75,
            trigger_reason=TriggerReason.THRESHOLD,
        )
        ctx = AssembledContext(blocks=[], meta=meta, needs_compression=True,
                               recommended_level=CompressionLevel.TRIMMED)
        assert ctx.needs_compression is True
        assert ctx.recommended_level == "trimmed"

    def test_recommended_level_by_pressure(self):
        """compression_pressure 驱动压缩层级跃迁"""
        test_cases = [
            (0.3, CompressionLevel.NONE),
            (0.5, CompressionLevel.FROZEN),
            (0.7, CompressionLevel.TRIMMED),
            (0.85, CompressionLevel.SUMMARIZED),
            (0.95, CompressionLevel.CONSOLIDATED),
        ]
        for pressure, expected_level in test_cases:
            meta = ContextAssemblyMeta(
                compression_pressure=pressure,
                trigger_reason=TriggerReason.THRESHOLD,
            )
            ctx = AssembledContext(
                meta=meta,
                needs_compression=(pressure >= 0.5),
                recommended_level=expected_level,
            )
            assert ctx.recommended_level == expected_level, f"pressure={pressure}"

    def test_get_statistics(self, sample_blocks, sample_meta):
        ctx = AssembledContext(blocks=sample_blocks, meta=sample_meta)
        stats = ctx.get_statistics()
        assert "总 Token" in stats
        assert "利用率" in stats
        assert "系统提示" in stats or "system_prompt" in stats

    def test_get_statistics_with_reasoning(self):
        meta = ContextAssemblyMeta(
            total_tokens=50000,
            budget_per_request=128000,
            reasoning_budget_tokens=32000,
            reasoning_tokens_used=8000,
        )
        ctx = AssembledContext(meta=meta)
        stats = ctx.get_statistics()
        assert "思考" in stats
        assert "8000" in stats

    def test_get_statistics_empty(self):
        ctx = AssembledContext.empty()
        stats = ctx.get_statistics()
        assert "总 Token: 0" in stats

    def test_frozen_immutable(self):
        ctx = AssembledContext.empty()
        with pytest.raises(ValidationError):
            ctx.blocks = []

    def test_serialization_minimal(self):
        ctx = AssembledContext.empty()
        j = ctx.model_dump_json()
        restored = AssembledContext.model_validate_json(j)
        assert restored.blocks == []
        assert restored.needs_compression is False

    def test_serialization_full(self, sample_blocks, sample_meta):
        ctx = AssembledContext(
            blocks=sample_blocks,
            meta=sample_meta,
            needs_compression=True,
            recommended_level=CompressionLevel.TRIMMED,
        )
        j = ctx.model_dump_json()
        parsed = json.loads(j)
        assert parsed["needs_compression"] is True
        assert parsed["recommended_level"] == "trimmed"
        assert len(parsed["blocks"]) == 4

        restored = AssembledContext.model_validate_json(j)
        assert len(restored.blocks) == 4
        assert restored.meta.session_id == "s1"


# ========== 5. 场景测试 ==========

def test_full_assembly_workflow():
    """模拟完整的上下文组装 → 压缩决策 → LLM 格式转换流程"""
    # 1. 组装结果：系统提示 + 对话历史 + 工具结果
    blocks = [
        ContextBlock(
            source=BlockSource.SYSTEM_PROMPT,
            role="system",
            content="You are ChaChaAgent.",
            zone="protected",
            priority=0,
            importance_score=1.0,
            token_count=50,
            cache_ttl=600,
        ),
        ContextBlock(
            source=BlockSource.STATIC_RULE,
            role="system",
            content="项目使用 Python 3.11+，代码风格 Black",
            zone="protected",
            priority=1,
            importance_score=0.9,
            token_count=40,
            cache_ttl=600,
        ),
        ContextBlock(
            source=BlockSource.HISTORY,
            role="user",
            content="帮我修复 main.py 的 bug",
            zone="dynamic",
            priority=3,
            importance_score=0.85,
            token_count=15,
        ),
        ContextBlock(
            source=BlockSource.TOOL_RESULT,
            role="tool",
            content="def main():\n" + "    pass\n" * 100,
            zone="dynamic",
            priority=4,
            importance_score=0.5,
            token_count=200,
        ),
    ]

    # 2. 计算统计
    total = sum(b.token_count for b in blocks)
    protected = sum(b.token_count for b in blocks if b.zone == "protected")
    dynamic = total - protected

    meta = ContextAssemblyMeta(
        session_id="s1",
        total_tokens=total,
        protected_tokens=protected,
        dynamic_tokens=dynamic,
        budget_per_request=128000,
        utilization_ratio=total / 128000,
        compression_pressure=0.6,
        trigger_reason=TriggerReason.THRESHOLD,
        blocks_by_source={
            "system_prompt": 50,
            "static_rule": 40,
            "history": 15,
            "tool_result": 200,
        },
    )

    ctx = AssembledContext(
        blocks=blocks,
        meta=meta,
        needs_compression=True,
        recommended_level=CompressionLevel.FROZEN,
    )

    # 3. 验证
    assert ctx.meta.total_tokens == 305
    assert ctx.meta.protected_tokens == 90
    assert ctx.meta.dynamic_tokens == 215
    assert ctx.needs_compression is True

    # 4. 压缩决策：按 importance_score 升序删除最低分块
    dynamic_blocks = ctx.get_dynamic_slice()
    assert dynamic_blocks[0].importance_score == 0.85  # 对话历史
    assert dynamic_blocks[-1].importance_score == 0.5   # 工具结果 → 最先被删

    # 5. 生成 LLM 消息
    msgs = ctx.get_messages()
    assert msgs[0]["role"] == "system"
    assert len(msgs) == 4

    # 6. 验证保护区不受影响
    protected_slice = ctx.get_protected_slice()
    assert len(protected_slice) == 2
    assert all(b.importance_score >= 0.9 for b in protected_slice)
