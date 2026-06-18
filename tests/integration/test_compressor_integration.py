"""
tests/integration/test_compressor_integration.py
集成测试：压缩长工具输出后继续对话
"""

import tempfile
from pathlib import Path

import pytest

from core.context.context_compressor import ContextCompressor
from core.context_manager import ContextManager
from core.models.context import (
    AssembledContext, ContextAssemblyMeta, ContextBlock, BlockSource, CompressionLevel,
)
from core.models.session import ConversationState, SessionMetadata, MessageEvent, ObservationEvent


# ====== Fixtures ======

@pytest.fixture
def compressor():
    d = Path(tempfile.mkdtemp())
    return ContextCompressor(base_dir=d)


# ====== 测试 ======

def test_compress_long_tool_output_then_chat(compressor):
    """模拟：长工具输出 → 压缩 → 注入上下文 → 继续对话"""
    # 1. 构建有长工具输出的 ConversationState
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="列出所有文件"))
    state.add_event(ObservationEvent(
        source="tool", tool_use_id="c1",
        content="\n".join([f"file_{i:04d}.py" for i in range(10000)]),
        status="success",
    ))

    # 2. ContextManager 组装
    mgr = ContextManager()
    ctx = mgr.assemble(state, session_id="s1")

    # 检查：工具结果 block 存在且很大
    tool_blocks = [b for b in ctx.blocks if b.source == BlockSource.TOOL_RESULT]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].token_count > 5000

    # 3. 压缩
    ctx2 = AssembledContext(
        meta=ctx.meta, blocks=list(ctx.blocks),
        needs_compression=True,
        recommended_level=CompressionLevel.FROZEN.value,
    )
    compressed = compressor.compress(ctx2, pressure=0.85)

    # 4. 验证压缩效果
    compressed_tool = [b for b in compressed.blocks if b.source == BlockSource.TOOL_RESULT]
    assert len(compressed_tool) == 1
    assert compressed_tool[0].token_count < 100  # 占位符很短
    assert "工具结果已缓存" in compressed_tool[0].content
    assert compressed_tool[0].original_token_count > 0  # 原始大小被记录

    # 5. 压缩后的上下文可用于构造消息
    messages = compressed.get_messages()
    assert len(messages) > 0
    # 系统提示 + 用户消息 + 工具结果占位
    roles = [m["role"] for m in messages]
    assert "system" in roles
    assert "user" in roles
    assert "tool" in roles


def test_compress_progressive_levels(compressor):
    """渐进压缩：FROZEN → TRIMMED → 验证各层"""
    # 构建大量历史消息
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

    # Level: FROZEN
    ctx = AssembledContext(meta=meta, blocks=list(blocks),
                           needs_compression=True,
                           recommended_level=CompressionLevel.FROZEN.value)
    result = compressor.compress(ctx, pressure=0.6)
    frozen_tools = [b for b in result.blocks if b.source == BlockSource.TOOL_RESULT]
    assert all("工具结果已缓存" in b.content for b in frozen_tools)

    # Level: TRIMMED
    ctx = AssembledContext(meta=meta, blocks=list(blocks),
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)
    result = compressor.compress(ctx, pressure=0.6)
    # 工具冻结 + 历史裁剪
    frozen_tools = [b for b in result.blocks if b.source == BlockSource.TOOL_RESULT]
    assert all("工具结果已缓存" in b.content for b in frozen_tools)
