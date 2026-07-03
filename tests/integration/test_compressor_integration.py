"""
tests/integration/test_compressor_integration.py (v3.2)
集成测试：压缩长工具输出后继续对话

v3.2: 冻结逻辑已迁移至 dispatcher，compressor 仅负责 TRIMMED/SUMMARIZED/CONSOLIDATED。
"""

import pytest

from core.context.context_compressor import ContextCompressor
from core.context_manager import ContextManager
from core.models.context import (
    AssembledContext, ContextAssemblyMeta, ContextBlock, BlockSource, CompressionLevel,
)
from core.models.session import ConversationState, SessionMetadata, MessageEvent, ObservationEvent


@pytest.fixture
def compressor():
    return ContextCompressor(preserve_recent=5)


# ====== 长工具输出压缩 ======

@pytest.mark.asyncio
async def test_compress_long_tool_output_then_chat(compressor):
    """模拟：长工具输出 → 压缩 → 注入上下文 → 继续对话"""
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="列出所有文件"))
    state.add_event(ObservationEvent(
        source="tool", tool_use_id="c1",
        content="\n".join([f"file_{i:04d}.py" for i in range(10000)]),
        status="success",
    ))

    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")

    tool_blocks = [b for b in ctx.blocks if b.source == BlockSource.TOOL_RESULT]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].token_count > 5000

    # v3.2: 使用 compress() 在 pressure=0.8 触发 TRIMMED
    ctx2 = AssembledContext(
        meta=ctx.meta, blocks=list(ctx.blocks),
        needs_compression=True,
        recommended_level=CompressionLevel.TRIMMED.value,
    )
    compressed = await compressor.compress(ctx2, pressure=0.8)

    # protected zone 不变
    protected = [b for b in compressed.blocks if b.zone == "protected"]
    assert len(protected) >= 1

    messages = compressed.get_messages()
    assert len(messages) > 0
    roles = [m["role"] for m in messages]
    assert "system" in roles
    assert "user" in roles


# ====== 渐进压缩级别 ======

@pytest.mark.asyncio
async def test_compress_progressive_levels(compressor):
    """渐进压缩：不同 pressure 触发不同级别"""
    specs = [(BlockSource.SYSTEM_PROMPT, "protected", "system")]
    specs += [(BlockSource.TOOL_RESULT, "dynamic", "tool " * 2000)]
    specs += [(BlockSource.HISTORY, "dynamic", f"history {i}" * 50) for i in range(10)]

    blocks = []
    for source, zone, content in specs:
        blocks.append(ContextBlock(
            source=source, role="system" if zone == "protected" else "user",
            content=content, zone=zone, priority=0,
            token_count=len(content) // 4,
        ))

    meta = ContextAssemblyMeta(total_tokens=sum(b.token_count for b in blocks))

    # pressure < 0.70: 不压缩
    ctx = AssembledContext(meta=meta, blocks=list(blocks),
                           needs_compression=True,
                           recommended_level=CompressionLevel.NONE.value)
    result = await compressor.compress(ctx, pressure=0.5)
    assert len(result.blocks) == len(blocks)  # 不压缩，块数不变

    # pressure >= 0.70: TRIMMED
    ctx = AssembledContext(meta=meta, blocks=list(blocks),
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)
    result = await compressor.compress(ctx, pressure=0.8)
    # TRIMMED 会裁剪 dynamic 区消息
    assert len(result.blocks) <= len(blocks)


# ====== 压缩不影响永久记忆 ======

@pytest.mark.asyncio
async def test_compression_skips_permanent_memory_blocks(compressor):
    """永久记忆在 protected zone，压缩跳过"""
    blocks = [
        ContextBlock(
            source=BlockSource.SYSTEM_PROMPT, role="system",
            content="[Permanent Memory]\n## 永久\n- 关键信息", zone="protected",
            priority=2, token_count=20,
        ),
        ContextBlock(
            source=BlockSource.TOOL_RESULT, role="tool",
            content="huge tool result " * 5000, zone="dynamic",
            priority=30, token_count=1250,
        ),
    ]

    meta = ContextAssemblyMeta(total_tokens=sum(b.token_count for b in blocks))
    ctx = AssembledContext(
        meta=meta, blocks=blocks,
        needs_compression=True,
        recommended_level=CompressionLevel.TRIMMED.value,
    )

    result = await compressor.compress(ctx, pressure=0.8)

    # 永久记忆不变
    assert "关键信息" in result.blocks[0].content
    # protected block 保持
    assert result.blocks[0].zone == "protected"
