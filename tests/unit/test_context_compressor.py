"""
tests/unit/test_context_compressor.py
单元测试：core/context/context_compressor.py (v2.0)

新增覆盖：
  - Stage 2 激进 FROZEN：_compress_json_placeholder (JSON key 最小化)
  - Stage 2 激进 FROZEN：_freeze_full_result (150 字符摘要)
  - 工具结果占位符二次压缩 {"t":"x","s":"x","p":"x"}
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


# ====== 1. FROZEN Stage 2: JSON 占位符二次压缩 ======

def test_compress_json_placeholder(compressor):
    """Stage 2: {"toolname":"x","result_summary":"x","cache_path":"x"} → {"t":"x","s":"x","p":"x"}"""
    placeholder = '{"toolname": "read_file", "result_summary": "读取 main.py 前200行...", "cache_path": "tool_cache/t3.json"}'

    result = compressor._compress_json_placeholder(placeholder, "s1")

    assert '"t"' in result
    assert '"s"' in result
    assert '"p"' in result
    assert 'read_file' in result
    assert 'tool_cache/t3.json' in result
    assert '"toolname"' not in result
    assert '"result_summary"' not in result


def test_compress_json_placeholder_truncates_summary(compressor):
    """Stage 2: 摘要截断到 80 字符"""
    long_summary = "读取了一个非常大的文件，其中包含了很多行代码，第一行是 import os..." + "x" * 60
    placeholder = f'{{"toolname": "read_file", "result_summary": "{long_summary}", "cache_path": "t.json"}}'

    result = compressor._compress_json_placeholder(placeholder, "s1")

    import json
    data = json.loads(result)
    assert len(data["s"]) <= 80


def test_compress_json_placeholder_invalid_json(compressor):
    """无效 JSON → 截断到 150 字符"""
    invalid = "not a json string" * 50
    result = compressor._compress_json_placeholder(invalid, "s1")
    assert len(result) <= 153  # 150 + "...(truncated)"


# ====== 2. FROZEN Stage 2: 完整结果激进截断 ======

def test_freeze_full_result_short(compressor):
    """短结果不被截断"""
    short = "hello world"
    result = compressor._freeze_full_result(short, "s1")
    assert "hello world" in result


def test_freeze_full_result_long(compressor):
    """长结果截断到 150 字符摘要"""
    long_content = "x" * 5000
    result = compressor._freeze_full_result(long_content, "s1")

    assert "工具结果已缓存" in result
    assert "摘要:" in result
    assert len(result) < 500  # 远小于原始


# ====== 3. FROZEN 完整流程 ======

def test_freeze_replaces_tool_results(compressor):
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system prompt"),
        (BlockSource.TOOL_RESULT, "dynamic", "x" * 5000),
        (BlockSource.HISTORY, "dynamic", "user message"),
    )

    result = compressor.compress(ctx, pressure=0.6)

    assert result.blocks[0].content == "system prompt"
    assert "工具结果已缓存" in result.blocks[1].content
    assert result.blocks[1].original_token_count > 0
    assert result.blocks[2].content == "user message"


def test_freeze_not_affects_protected(compressor):
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "DO NOT TOUCH"),
        (BlockSource.STATIC_RULE, "protected", "CHACHA.md rules"),
    )

    result = compressor.compress(ctx, pressure=0.6)
    assert result.blocks[0].content == "DO NOT TOUCH"
    assert result.blocks[1].content == "CHACHA.md rules"


# ====== 4. Stage 2: JSON 占位符在 FROZEN 中被二次压缩 ======

def test_freeze_compresses_json_placeholders(compressor):
    """已有 JSON 占位符的工具结果在 FROZEN 中被二次压缩"""
    json_placeholder = '{"toolname": "grep_tool", "result_summary": "找到 42 个匹配项，分布在 12 个文件中...", "cache_path": "tool_cache/grep_c1.json"}'

    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system"),
        (BlockSource.TOOL_RESULT, "dynamic", json_placeholder),
    )

    result = compressor.compress(ctx, pressure=0.6)

    tool_block = result.blocks[1]
    # 二次压缩后 key 最小化
    assert '"t"' in tool_block.content
    assert '"s"' in tool_block.content
    assert '"toolname"' not in tool_block.content


# ====== 5. 混合压缩策略（原有） ======

def test_mixed_compression_strategy(compressor):
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

    assert result.blocks[0].content == "核心指令"
    assert "工具结果已缓存" in result.blocks[1].content
    assert "工具结果已缓存" in result.blocks[2].content
    assert result.blocks[3].content == "user msg"


def test_semantic_integrity_after_trim(compressor):
    long = "\n".join([f"[ERROR] log line {i}: exception at module {i%10}" for i in range(1000)])
    specs = [(BlockSource.HISTORY, "dynamic", long)]
    specs += [(BlockSource.HISTORY, "dynamic", f"recent-{i}") for i in range(5)]
    blocks, ctx = _make_blocks(*specs)

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = compressor.compress(ctx, pressure=0.5)
    trimmed = result.blocks[0].content
    assert "截断" in trimmed
    assert "[ERROR]" in trimmed
    assert "line 0" in trimmed
    assert "line 999" in trimmed
    for i in range(5):
        assert result.blocks[i + 1].content == f"recent-{i}"


# ====== 6. 多模态 ======

def test_multimodal_content_passthrough(compressor):
    blocks, ctx = _make_blocks(
        (BlockSource.SYSTEM_PROMPT, "protected", "system"),
        (BlockSource.HISTORY, "dynamic", "[image: base64data...]"),
    )

    result = compressor.compress(ctx, pressure=0.6)
    assert "[image: base64data...]" in result.blocks[1].content


def test_multimodal_block_frozen_not_affected(compressor):
    blocks, ctx = _make_blocks(
        (BlockSource.TOOL_RESULT, "dynamic", "normal output"),
        (BlockSource.HISTORY, "dynamic", "[audio: sound.mp3]"),
    )

    result = compressor.compress(ctx, pressure=0.6)
    assert "工具结果已缓存" in result.blocks[0].content
    assert "[audio: sound.mp3]" in result.blocks[1].content


def test_trim_cuts_history(compressor):
    long_content = "\n".join([f"line {i}" for i in range(500)])
    specs = [(BlockSource.HISTORY, "dynamic", long_content)]
    specs += [(BlockSource.HISTORY, "dynamic", f"recent {i}") for i in range(5)]
    blocks, ctx = _make_blocks(*specs)

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = compressor.compress(ctx, pressure=0.5)
    trimmed = result.blocks[0].content
    assert "截断" in trimmed
    assert "line 0" in trimmed
    assert "line 499" in trimmed


def test_trim_recent_keeps_untouched(compressor):
    blocks, ctx = _make_blocks(*[
        (BlockSource.HISTORY, "dynamic", f"msg {i}") for i in range(10)
    ])

    ctx = AssembledContext(meta=ctx.meta, blocks=blocks,
                           needs_compression=True,
                           recommended_level=CompressionLevel.TRIMMED.value)

    result = compressor.compress(ctx, pressure=0.5)
    for i in range(5, 10):
        assert result.blocks[i].content == f"msg {i}"
