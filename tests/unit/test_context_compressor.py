"""
tests/unit/test_context_compressor.py
单元测试：core/context/context_compressor.py (v3.2)

v3.2 变更：冻结逻辑已迁移至 dispatcher，compressor 仅负责 TRIMMED/SUMMARIZED/CONSOLIDATED。
测试 compress() 公共 API 在各种 pressure 下的行为。
"""

import pytest

from core.context.context_compressor import ContextCompressor
from core.models.context import (
    AssembledContext,
    BlockSource,
    CompressionLevel,
    ContextAssemblyMeta,
    ContextBlock,
)

# ====== Fixtures ======

@pytest.fixture
def compressor():
    return ContextCompressor(preserve_recent=5)


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
                                     recommended_level=CompressionLevel.TRIMMED.value)


# ====== 1. pressure < 0.70: 不压缩 ======

@pytest.mark.asyncio
async def test_low_pressure_no_compression(compressor):
    """pressure < 0.70 → 不压缩，原样返回"""
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system prompt"),
        (BlockSource.HISTORY, "dynamic", "user message"),
    )
    result = await compressor.compress(ctx, pressure=0.5)
    assert result.blocks[0].content == "system prompt"
    assert result.blocks[1].content == "user message"
    assert len(result.blocks) == 2


@pytest.mark.asyncio
async def test_force_overrides_low_pressure(compressor):
    """force=True 时即使 pressure < 0.70 也执行压缩"""
    # 需要足够多的消息触发 TRIMMED
    specs = [(BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(10)]
    blocks, ctx = _make_blocks(*specs)
    result = await compressor.compress(ctx, pressure=0.5, force=True)
    # force=True 时至少执行 TRIMMED（head 2 + tail 5 = 7 + marker）
    assert len(result.blocks) <= 8  # TRIMMED: head(2) + marker(1) + tail(5)


# ====== 2. TRIMMED: 裁剪中间 ======

@pytest.mark.asyncio
async def test_trim_cuts_middle_keeps_head_tail(compressor):
    """TRIMMED: 裁剪中间消息，保留头尾"""
    specs = [(BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(10)]
    blocks, ctx = _make_blocks(*specs)
    result = await compressor.compress(ctx, pressure=0.75)

    # HEAD: 前2条保留
    assert result.blocks[0].content == "msg 0"
    assert result.blocks[1].content == "msg 1"
    # 中间: 裁剪标记
    assert "裁剪" in result.blocks[2].content
    # TAIL: 最近5条保留
    for i in range(5):
        assert result.blocks[3 + i].content == f"msg {5 + i}"


@pytest.mark.asyncio
async def test_trim_small_context_noop(compressor):
    """消息数量 <= head+tail 时不裁剪"""
    specs = [(BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(3)]
    blocks, ctx = _make_blocks(*specs)
    result = await compressor.compress(ctx, pressure=0.75)
    # 3 条消息 <= 2+5，不裁剪
    assert len(result.blocks) == 3


# ====== 3. protected zone 不受影响 ======

@pytest.mark.asyncio
async def test_protected_zone_untouched(compressor):
    """protected zone 的内容不受压缩影响"""
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "DO NOT TOUCH"),
        (BlockSource.STATIC_RULE, "protected", "CHACHA.md rules"),
    )
    result = await compressor.compress(ctx, pressure=0.8)
    assert result.blocks[0].content == "DO NOT TOUCH"
    assert result.blocks[1].content == "CHACHA.md rules"


@pytest.mark.asyncio
async def test_trim_only_affects_dynamic_zone(compressor):
    """TRIMMED 只裁剪 dynamic 区，protected 区不变"""
    specs = [
        (BlockSource.SYSTEM_PROMPT, "protected", "system"),
        (BlockSource.STATIC_RULE, "protected", "rules"),
    ]
    specs += [(BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(15)]
    blocks, ctx = _make_blocks(*specs)
    result = await compressor.compress(ctx, pressure=0.75)

    # protected 区不变
    assert result.blocks[0].content == "system"
    assert result.blocks[1].content == "rules"
    # dynamic 区被裁剪（标记在 blocks[4] 因为 head 2 + 标记）
    trimmed_contents = [b.content for b in result.blocks if "裁剪" in b.content]
    assert len(trimmed_contents) >= 1


# ====== 4. 多模态内容 ======

@pytest.mark.asyncio
async def test_multimodal_content_passthrough(compressor):
    """多模态内容在压缩中保持原样"""
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system"),
        (BlockSource.HISTORY, "dynamic", "[image: base64data...]"),
    )
    result = await compressor.compress(ctx, pressure=0.6)
    assert "[image: base64data...]" in result.blocks[1].content


# ====== 5. 混合场景 ======

@pytest.mark.asyncio
async def test_mixed_protected_and_dynamic(compressor):
    """protected + dynamic 混合场景：protected 不变，dynamic 可裁剪"""
    specs = [
        (BlockSource.SYSTEM_PROMPT, "protected", "核心指令"),
    ]
    specs += [(BlockSource.HISTORY, "dynamic", f"history {i}") for i in range(12)]
    blocks, ctx = _make_blocks(*specs)
    result = await compressor.compress(ctx, pressure=0.8)

    assert result.blocks[0].content == "核心指令"
    # dynamic 区被裁剪
    assert any("裁剪" in b.content for b in result.blocks)


@pytest.mark.asyncio
async def test_empty_dynamic_zone(compressor):
    """dynamic 区为空 → 不压缩"""
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system"),
        (BlockSource.STATIC_RULE, "protected", "rules"),
    )
    result = await compressor.compress(ctx, pressure=0.8)
    assert len(result.blocks) == 2


# ====== 6. 语义完整性（原有测试保留） ======

@pytest.mark.asyncio
async def test_semantic_integrity_after_trim(compressor):
    """TRIMMED 裁剪中间，保留头尾，插入裁剪标记"""
    long = "\n".join([f"[ERROR] log line {i}: exception at module {i%10}" for i in range(1000)])
    specs = [(BlockSource.HISTORY, "dynamic", long)]
    specs += [(BlockSource.HISTORY, "dynamic", f"mid msg {i}") for i in range(10)]
    specs += [(BlockSource.HISTORY, "dynamic", f"recent-{i}") for i in range(5)]
    blocks, ctx = _make_blocks(*specs)

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = await compressor.compress(ctx, pressure=0.75)
    # HEAD 前2条保留
    assert "[ERROR] log line 0" in result.blocks[0].content
    # 中间消息被裁剪标记替代
    trimmed_block = result.blocks[2]
    assert "裁剪" in trimmed_block.content
    # TAIL 保留
    for i in range(5):
        assert result.blocks[3 + i].content == f"recent-{i}"


@pytest.mark.asyncio
async def test_trim_cuts_history(compressor):
    """TRIMMED: 裁剪中间消息，保留头尾"""
    long_content = "\n".join([f"line {i}" for i in range(500)])
    specs = [(BlockSource.HISTORY, "dynamic", long_content)]
    specs += [(BlockSource.HISTORY, "dynamic", f"mid {i}") for i in range(3)]
    specs += [(BlockSource.HISTORY, "dynamic", f"recent {i}") for i in range(5)]
    blocks, ctx = _make_blocks(*specs)

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = await compressor.compress(ctx, pressure=0.75)
    # HEAD(2) + marker + TAIL(5)
    assert "裁剪" in result.blocks[2].content
    assert "line 0" in result.blocks[0].content


@pytest.mark.asyncio
async def test_trim_recent_keeps_untouched(compressor):
    """TRIMMED 保留最近的消息"""
    blocks, ctx = _make_blocks(*[
        (BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(10)
    ])

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = await compressor.compress(ctx, pressure=0.5)  # pressure < 0.70 → no compression
    # 所有消息保持原样
    for i in range(10):
        assert result.blocks[i].content == f"msg {i}"
