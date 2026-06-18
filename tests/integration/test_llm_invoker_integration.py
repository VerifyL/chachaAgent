"""
tests/integration/test_llm_invoker_integration.py
集成测试：Mock LLM 回放各种响应模式 + Gateway/Telemetry 联动
"""

import asyncio

import pytest

from core.llm_invoker import LLMInvoker, StreamChunk
from core.telemetry import Telemetry
from core.models.config import TelemetryConfig


class MockClient:
    def __init__(self, chunks: list):
        self._chunks = chunks

    async def stream(self, messages, tools):
        for chunk in self._chunks:
            yield chunk


# ====== 纯文本回放 ======

@pytest.mark.asyncio
async def test_text_only_replay_with_telemetry():
    """纯文本 LLM 响应 → Telemetry 记录"""
    t = Telemetry(TelemetryConfig(log_level="WARNING"))
    t.start()

    client = MockClient([
        StreamChunk(type="text", content="Hello, world!"),
        StreamChunk(type="done", finish_reason="stop",
                    usage={"input": 50, "output": 10, "model": "gpt-4"}),
    ])
    invoker = LLMInvoker(model_client=client, telemetry=t)
    resp = await invoker.invoke(
        [{"role": "user", "content": "Say hello"}]
    )

    assert resp.text == "Hello, world!"
    assert resp.finish_reason == "stop"

    t.stop()


# ====== 工具调用回放 ======

@pytest.mark.asyncio
async def test_tool_call_replay():
    """LLM 返回 read_file 工具调用 → 正确解析"""
    client = MockClient([
        StreamChunk(type="text", content="I'll read the file."),
        StreamChunk(type="tool_call_start", tool_index=0, tool_id="call-1",
                    tool_name="read_file"),
        StreamChunk(type="tool_call_end", tool_index=0),
        StreamChunk(type="done", finish_reason="tool_calls",
                    usage={"input": 100, "output": 20}),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke(
        [{"role": "user", "content": "Read main.py"}]
    )

    assert "I'll read" in resp.text
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.finish_reason == "tool_calls"


# ====== 异常回放：超时 ======

@pytest.mark.asyncio
async def test_error_replay_timeout():
    """模拟超时异常"""
    client = MockClient([
        StreamChunk(type="error", error="Request timeout after 60s"),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke(
        [{"role": "user", "content": "hi"}]
    )

    assert "Timeout" in (resp.error or "")
    assert resp.duration_ms >= 0
