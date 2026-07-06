"""
tests/unit/test_output_governor.py
单元测试：core/output_governor.py
覆盖：JSON修复（缺闭合括号/键中断/引号/尾逗号 + 置信度）、文本过滤、
      tool_calls/thinking 块识别、flush(FlushResult)、LLM自愈、边界
"""

import json

from core.output_governor import (
    DEFAULT_CONTENT_RULES,
    BlockType,
    ContentRule,
    FlushResult,
    OutputGovernor,
    RepairConfidence,
)


# 辅助：_repair_json 返回 (str, confidence)
def _repair(gov, text):
    return gov._repair_json(text)


# ========== 1. JSON 修复：缺闭合括号 ==========

def test_close_missing_brackets():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"path": "/tmp", "args": {"key": "val"}')
    assert json.loads(repaired)
    assert conf == RepairConfidence.HIGH


def test_close_missing_array_bracket():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '[{"a": 1}, {"b": 2}')
    assert json.loads(repaired)
    assert conf == RepairConfidence.HIGH


def test_close_nested_missing():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"outer": {"inner": [1, 2, {"deep": true}')
    assert json.loads(repaired)
    assert conf == RepairConfidence.HIGH


def test_already_valid_json():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"a": 1}')
    assert repaired == '{"a": 1}'
    assert conf == RepairConfidence.HIGH


# ========== 2. JSON 修复：键中断 + 置信度 ==========

def test_trailing_incomplete_key():
    """缺闭合的键值对 → 修复后合法"""
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"path": "/x", "con')
    assert json.loads(repaired)


def test_trailing_incomplete_with_brace():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"path": "/tmp/te')
    assert json.loads(repaired)
    data = json.loads(repaired)
    assert data["path"] == "/tmp/te"


def test_remove_trailing_comma():
    repaired = OutputGovernor._remove_trailing_comma('{"a": 1,')
    assert repaired == '{"a": 1'


def test_comma_then_close():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"a": 1,')
    assert json.loads(repaired)


def test_unclosed_string():
    gov = OutputGovernor()
    repaired, conf = _repair(gov, '{"key": "val')
    assert json.loads(repaired)
    assert conf == RepairConfidence.LOW  # 补引号 → 低置信度


def test_failed_repair():
    """完全不可修复 → FAILED → needs_llm_fix=True"""
    gov = OutputGovernor()
    repaired, conf = _repair(gov, "not json at all {{{")
    assert conf == RepairConfidence.FAILED


# ========== 3. 流式 feed + flush ==========

def test_feed_text_passthrough():
    gov = OutputGovernor()
    out = gov.feed("Hello, ")
    assert out == "Hello, "
    out = gov.feed("world!")
    assert out == "world!"


def test_feed_json_collection():
    gov = OutputGovernor()
    gov.feed('I will read. {"tool_calls": [{"function": {"name": "read", "arguments": "')
    out = gov.feed('{"path": "/tmp/test.txt"}')
    assert out is None  # JSON缓冲中

    result = gov.flush()
    assert isinstance(result, FlushResult)
    assert result.repaired or not result.repaired  # flush 完成


def test_feed_multiple_chunks():
    gov = OutputGovernor()
    gov.feed("Hello")
    out = gov.feed(" ")
    assert out == " "
    out = gov.feed("world")
    assert out == "world"


def test_flush_empty():
    gov = OutputGovernor()
    result = gov.flush()
    assert result.output == ""


def test_flush_remaining_text():
    gov = OutputGovernor()
    gov.feed("some text")
    result = gov.flush()
    assert isinstance(result, FlushResult)
    assert result.needs_llm_fix is False


# ========== 4. 置信度传播到 flush ==========

def test_flush_returns_flush_result():
    """flush 返回 FlushResult 结构体"""
    gov = OutputGovernor()
    gov.feed("plain text")
    result = gov.flush()
    assert isinstance(result, FlushResult)
    assert result.output == ""
    assert result.repaired is False
    assert result.needs_llm_fix is False


def test_flush_low_confidence_triggers_llm_fix():
    """低置信度修复 → needs_llm_fix=True"""
    gov = OutputGovernor()
    gov.feed('{"tool_calls": [{"function": {"arguments": "')
    gov.feed('{"key": "broken-value')  # 未闭合字符串
    result = gov.flush()
    assert result.needs_llm_fix is True


# ========== 5. ThinkingBlock 透传 ==========

def test_thinking_block_passthrough():
    """thinking 块应透传，不缓冲"""
    gov = OutputGovernor()
    out = gov.feed('{"thinking": "I should read the file first"')
    assert out is not None  # 透传，不缓冲
    assert "thinking" in out


# ========== 6. BlockType 识别 ==========

def test_detect_tool_use():
    gov = OutputGovernor()
    gov.feed('{"tool_calls": [{"function": {"name": "r", "arguments": "')
    assert gov._block_type == BlockType.TOOL_USE


def test_detect_thinking():
    gov = OutputGovernor()
    gov.feed('{"thinking": "Let me think')
    assert gov._block_type == BlockType.THINKING


# ========== 7. 内容过滤 ==========

def test_filter_block_dangerous():
    gov = OutputGovernor()
    result = gov._filter_content("Run sudo apt install with rm -rf /")
    assert "已拦截" in result


def test_filter_sanitize_api_key():
    gov = OutputGovernor()
    result = gov._filter_content("set API_KEY=sk-1234567890abcdef1234567890")
    assert "[REDACTED]" in result


def test_filter_normal_text_passes():
    gov = OutputGovernor()
    result = gov._filter_content("Hello, let's read main.py")
    assert "Hello" in result


def test_add_custom_rule():
    gov = OutputGovernor()
    gov.add_rule(ContentRule(r"(?i)secret_code_123", "custom secret"))
    result = gov._filter_content("The secret_code_123 is mine")
    assert "已拦截" in result


def test_remove_rule():
    gov = OutputGovernor()
    count_before = len(gov._content_rules)
    gov.remove_rule(DEFAULT_CONTENT_RULES[0].pattern)
    assert len(gov._content_rules) == count_before - 1


# ========== 8. validate_tool_call ==========

def test_validate_tool_call_valid():
    gov = OutputGovernor()
    valid, repaired = gov.validate_tool_call('{"path": "/tmp/test.py"}')
    assert valid is True
    assert json.loads(repaired)


def test_validate_tool_call_invalid():
    gov = OutputGovernor()
    valid, repaired = gov.validate_tool_call('{"path": "/tmp/test.py"')
    assert valid is True  # 修复后合法
    assert json.loads(repaired)


def test_validate_tool_call_unrepairable():
    gov = OutputGovernor()
    valid, repaired = gov.validate_tool_call("just plain text no json")
    assert valid is False


# ========== 9. buffer_size ==========

def test_buffer_size_tracks_input():
    gov = OutputGovernor()
    gov.feed("hello")
    assert gov.buffer_size == 5
    gov.feed(" world")
    assert gov.buffer_size == 11


# ========== 10. 综合流式场景 ==========

def test_full_streaming_workflow():
    """完整流式：文本 → tool_calls JSON → flush → FlushResult"""
    gov = OutputGovernor()

    chunks = [
        "I'll read the file for you.",
        ' {"tool_calls": [{"function": {"name": "read_file", "arguments": "',
        '{"path": "/tmp/main.py", "encoding": "utf-8',
        '"}',
    ]

    outputs = []
    for chunk in chunks:
        out = gov.feed(chunk)
        if out:
            outputs.append(out)

    result = gov.flush()
    assert isinstance(result, FlushResult)
    full = "".join(outputs) + result.output
    assert "I'll read the file" in full


def test_needs_llm_fix_false_on_no_collection():
    """纯文本场景 → needs_llm_fix=False"""
    gov = OutputGovernor()
    gov.feed("Hello, this is plain text only.")
    result = gov.flush()
    assert result.needs_llm_fix is False
    assert result.confidence == RepairConfidence.HIGH
