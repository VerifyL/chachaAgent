"""
tests/unit/test_summarizer.py
单元测试：core/context/summarizer.py — prompt 模板 + 无 LLM 透传
"""

import pytest

from core.context.summarizer import Summarizer, _PROMPTS


def test_prompts_exist():
    assert "brief" in _PROMPTS
    assert "detailed" in _PROMPTS
    assert "关键决策" in _PROMPTS["brief"]
    assert "用户偏好" in _PROMPTS["detailed"]


def test_prompts_different():
    assert _PROMPTS["brief"] != _PROMPTS["detailed"]


@pytest.mark.asyncio
async def test_summarize_without_llm_returns_original():
    """无 LLM 注入时原文透传"""
    s = Summarizer()  # llm_invoker=None
    result = await s.summarize("hello world", style="brief")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_summarize_unknown_style_falls_back_to_brief():
    s = Summarizer()
    result = await s.summarize("text", style="unknown")
    assert result == "text"


# ====== 摘要长度与关键信息保留（Mock LLM） ======

class MockLLM:
    async def invoke(self, messages, session_id=""):
        text = messages[1]["content"]
        from core.llm_invoker import LLMResponse
        return LLMResponse(text=f"总结: {text[:50]}...")


@pytest.mark.asyncio
async def test_summarize_length_brief():
    long_text = "line1\nline2\nline3\n" * 100
    s = Summarizer(llm_invoker=MockLLM())
    result = await s.summarize(long_text, style="brief")
    # 摘要应远短于原文
    assert len(result) < len(long_text)


@pytest.mark.asyncio
async def test_summarize_retains_keywords():
    """关键信息 'FILE NOT FOUND' 应出现在摘要中"""
    text = "ERROR: FILE NOT FOUND at line 500\n" + ("log line\n" * 100)
    s = Summarizer(llm_invoker=MockLLM())
    result = await s.summarize(text, style="brief")
    assert "FILE NOT FOUND" in result
