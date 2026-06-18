"""
tests/unit/test_context_compressor.py
单元测试：core/context/context_compressor.py
"""

import tempfile
from pathlib import Path

import pytest

from core.context.context_compressor import ContextCompressor
from core.models.context import (
    AssembledContext, ContextAssemblyMeta, ContextBlock, BlockSource, CompressionLevel,
)


# ====== Fixtures ======

@pytest.fixture
def compressor():
    d = Path(tempfile.mkdtemp())
    return ContextCompressor(base_dir=d)


def _make_blocks(*specs) -> tuple[list[ContextBlock], AssembledContext]:
    blocks = []
    for source, zone, content in specs:
        blocks.append(ContextBlock(
            source=source, role="system" if zone == "protected" else "user",
            content=content, zone=zone, priority=0,
            token_count=len(content) // 4,
        ))
    meta = ContextAssemblyMeta(
        total_tokens=sum(b.token_count for b in blocks),
        trigger="compression",
    )
    return blocks, AssembledContext(meta=meta, blocks=blocks,
                                     needs_compression=True,
                                     recommended_level=CompressionLevel.FROZEN.value)


# ====== 1. FROZEN ======

def test_freeze_replaces_tool_results(compressor):
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system prompt"),
        (BlockSource.TOOL_RESULT, "dynamic", "x" * 5000),
        (BlockSource.HISTORY, "dynamic", "user message"),
    )

    result = compressor.compress(ctx, pressure=0.6)

    # 系统提示不变
    assert result.blocks[0].content == "system prompt"
    # 工具结果 → 占位符
    assert "工具结果已缓存" in result.blocks[1].content
    assert result.blocks[1].original_token_count > 0
    # 历史消息不变（未达到 trimmed 级别）
    assert result.blocks[2].content == "user message"


def test_freeze_not_affects_protected(compressor):
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "DO NOT TOUCH"),
        (BlockSource.STATIC_RULE, "protected", "CHACHA.md rules"),
    )

    result = compressor.compress(ctx, pressure=0.6)
    assert result.blocks[0].content == "DO NOT TOUCH"
    assert result.blocks[1].content == "CHACHA.md rules"


# ====== 4. 混合压缩策略 ======

def test_mixed_compression_strategy(compressor):
    """FROZEN 同时处理工具结果 + 不碰 protected + 不碰历史"""
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "核心指令"),
        (BlockSource.TOOL_RESULT, "dynamic", "tool output " * 500),
        (BlockSource.TOOL_RESULT, "dynamic", "another tool " * 300),
        (BlockSource.HISTORY, "dynamic", "user msg"),
    )

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.FROZEN.value)

    result = compressor.compress(ctx, pressure=0.6)

    # protected 不变
    assert result.blocks[0].content == "核心指令"
    # 工具结果 → 占位符
    assert "工具结果已缓存" in result.blocks[1].content
    assert "工具结果已缓存" in result.blocks[2].content
    # 历史不变
    assert result.blocks[3].content == "user msg"


def test_semantic_integrity_after_trim(compressor):
    """裁剪后关键信息仍可读"""
    long = "\n".join([f"[ERROR] log line {i}: exception at module {i%10}" for i in range(1000)])
    specs = [(BlockSource.HISTORY, "dynamic", long)]
    specs += [(BlockSource.HISTORY, "dynamic", f"recent-{i}") for i in range(5)]
    blocks, ctx = _make_blocks(*specs)

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = compressor.compress(ctx, pressure=0.5)
    trimmed = result.blocks[0].content
    # 截断标记存在
    assert "截断" in trimmed
    # 首部关键信息可读
    assert "[ERROR]" in trimmed
    assert "line 0" in trimmed
    assert "line 999" in trimmed
    # 最近 5 个块完整保持
    for i in range(5):
        assert result.blocks[i + 1].content == f"recent-{i}"


# ====== 5. 多模态内容跳过 ======

def test_multimodal_content_passthrough(compressor):
    """多模态内容块（当前应为透传）"""
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system"),
        (BlockSource.HISTORY, "dynamic", "[image: base64data...]"),
    )

    result = compressor.compress(ctx, pressure=0.6)
    # 多模态在 FROZEN 阶段透传（不是工具结果）
    assert "[image: base64data...]" in result.blocks[1].content


def test_multimodal_block_frozen_not_affected(compressor):
    """多模态块不被冻结（非 TOOL_RESULT）"""
    blocks, ctx = _make_blocks(
        (BlockSource.TOOL_RESULT, "dynamic", "normal output"),
        (BlockSource.HISTORY, "dynamic", "[audio: sound.mp3]"),
    )

    result = compressor.compress(ctx, pressure=0.6)
    # 工具结果被冻结
    assert "工具结果已缓存" in result.blocks[0].content
    # 多模态历史不变
    assert "[audio: sound.mp3]" in result.blocks[1].content

def test_trim_cuts_history(compressor):
    """6 个历史块 → 最旧的 1 个被裁剪"""
    long_content = "\n".join([f"line {i}" for i in range(500)])
    specs = [(BlockSource.HISTORY, "dynamic", long_content)]  # 旧
    specs += [(BlockSource.HISTORY, "dynamic", f"recent {i}") for i in range(5)]  # 最近 5 个
    blocks, ctx = _make_blocks(*specs)

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = compressor.compress(ctx, pressure=0.5)
    # 第一个（最旧的）被裁剪
    trimmed = result.blocks[0].content
    assert "截断" in trimmed
    assert "line 0" in trimmed
    assert "line 499" in trimmed


def test_trim_recent_keeps_untouched(compressor):
    """最近 N 轮不会被裁剪"""
    blocks, ctx = _make_blocks(*[
        (BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(10)
    ])

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = compressor.compress(ctx, pressure=0.5)
    # 最近 5 个保持不变
    for i in range(5, 10):
        assert result.blocks[i].content == f"msg {i}"
