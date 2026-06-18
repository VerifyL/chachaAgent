"""
tests/integration/test_compressor_integration.py (v2.0)
集成测试：压缩长工具输出后继续对话

v2.0 新增:
  - Stage 2 二次压缩 JSON 占位符
  - 两阶段协作：Dispatcher JSON → Compressor 最小化
  - 压缩后上下文仍可对话
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


@pytest.fixture
def compressor():
    d = Path(tempfile.mkdtemp())
    return ContextCompressor(base_dir=d)


def test_compress_long_tool_output_then_chat(compressor):
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

    ctx2 = AssembledContext(
        meta=ctx.meta, blocks=list(ctx.blocks),
        needs_compression=True,
        recommended_level=CompressionLevel.FROZEN.value,
    )
    compressed = compressor.compress(ctx2, pressure=0.85)

    compressed_tool = [b for b in compressed.blocks if b.source == BlockSource.TOOL_RESULT]
    assert len(compressed_tool) == 1
    assert compressed_tool[0].token_count < 100
    assert "工具结果已缓存" in compressed_tool[0].content
    assert compressed_tool[0].original_token_count > 0

    messages = compressed.get_messages()
    assert len(messages) > 0
    roles = [m["role"] for m in messages]
    assert "system" in roles
    assert "user" in roles
    assert "tool" in roles


# ====== v2.0: 两阶段压缩协作 ======

def test_two_stage_compression_pipeline(compressor):
    """Stage 1 (Dispatcher) JSON 占位符 → Stage 2 (Compressor) key 最小化"""
    # Stage 1 产物: JSON 占位符
    json_placeholder = '{"toolname": "read_file", "result_summary": "读取了 main.py 前200行，包含 import os, sys, json 等模块导入声明", "cache_path": "tool_cache/read_file_c1.json"}'

    blocks = [
        ContextBlock(
            source=BlockSource.SYSTEM_PROMPT, role="system",
            content="system", zone="protected", priority=0,
            token_count=2,
        ),
        ContextBlock(
            source=BlockSource.TOOL_RESULT, role="tool",
            content=json_placeholder, zone="dynamic", priority=30,
            token_count=len(json_placeholder) // 4,
        ),
        ContextBlock(
            source=BlockSource.HISTORY, role="user",
            content="帮我看一下 main.py", zone="dynamic", priority=20,
            token_count=5,
        ),
    ]

    meta = ContextAssemblyMeta(
        total_tokens=sum(b.token_count for b in blocks),
        trigger="compression",
    )

    ctx = AssembledContext(
        meta=meta, blocks=blocks,
        needs_compression=True,
        recommended_level=CompressionLevel.FROZEN.value,
    )

    result = compressor.compress(ctx, pressure=0.85)

    # protected 不变
    assert result.blocks[0].content == "system"

    # Stage 2: JSON key 最小化
    tool_block = result.blocks[1]
    assert '"t"' in tool_block.content
    assert '"s"' in tool_block.content
    assert '"toolname"' not in tool_block.content

    # 历史不变
    assert result.blocks[2].content == "帮我看一下 main.py"


def test_compress_progressive_levels(compressor):
    """渐进压缩：FROZEN → TRIMMED → 验证各层"""
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
    assert all(b.original_token_count > 0 for b in frozen_tools)

    # Level: TRIMMED
    ctx = AssembledContext(meta=meta, blocks=list(blocks),
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)
    result = compressor.compress(ctx, pressure=0.6)
    frozen_tools = [b for b in result.blocks if b.source == BlockSource.TOOL_RESULT]
    assert all("工具结果已缓存" in b.content for b in frozen_tools)


# ====== v2.0: 压缩不影响永久记忆 ======

def test_compression_skips_permanent_memory_blocks(compressor):
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
        recommended_level=CompressionLevel.FROZEN.value,
    )

    result = compressor.compress(ctx, pressure=0.85)

    # 永久记忆不变
    assert "关键信息" in result.blocks[0].content
    # 工具结果被压缩
    assert "工具结果已缓存" in result.blocks[1].content
