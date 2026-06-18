"""
tests/unit/test_llm_invoker.py
单元测试：core/llm_invoker.py LLMInvoker
覆盖：流式 chunk 解析、tool_calls 增量构建、异常映射、空客户端、
      成本熔断、done 信号
"""

import pytest

from core.llm_invoker import (
    LLMInvoker, StreamChunk, ToolCall, LLMResponse,
)


# ====== Mock 模型客户端 ======

class MockClient:
    """模拟模型适配器：返回 AsyncIterator[StreamChunk]"""

    def __init__(self, chunks: list):
        self._chunks = chunks

    async def stream(self, messages, tools):
        for chunk in self._chunks:
            yield chunk


# ====== 1. 纯文本流式 ======

@pytest.mark.asyncio
async def test_text_only_stream():
    client = MockClient([
        StreamChunk(type="text", content="Hello"),
        StreamChunk(type="text", content=" world!"),
        StreamChunk(type="done", finish_reason="stop", usage={"input": 10, "output": 2}),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke([{"role": "user", "content": "hi"}])
    assert resp.text == "Hello world!"
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"
    assert resp.usage["input"] == 10


# ====== 2. 工具调用 ======

@pytest.mark.asyncio
async def test_tool_call_stream():
    client = MockClient([
        StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
        StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"pa'),
        StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='th": "/tmp/test.py"}'),
        StreamChunk(type="tool_call_end", tool_index=0),
        StreamChunk(type="done", finish_reason="tool_calls", usage={"input": 20, "output": 5}),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke([{"role": "user", "content": "read"}])
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].id == "c1"
    assert resp.tool_calls[0].arguments.get("path") == "/tmp/test.py"


# ====== 3. 多个工具调用 ======

@pytest.mark.asyncio
async def test_multiple_tool_calls():
    client = MockClient([
        StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
        StreamChunk(type="tool_call_end", tool_index=0),
        StreamChunk(type="tool_call_start", tool_index=1, tool_id="c2", tool_name="grep"),
        StreamChunk(type="tool_call_end", tool_index=1),
        StreamChunk(type="done", finish_reason="tool_calls"),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke([{"role": "user", "content": "search"}])
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[1].name == "grep"


# ====== 4. 文本 + 工具调用混合 ======

@pytest.mark.asyncio
async def test_text_and_tool_calls():
    client = MockClient([
        StreamChunk(type="text", content="Let me read that."),
        StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
        StreamChunk(type="tool_call_end", tool_index=0),
        StreamChunk(type="done", finish_reason="tool_calls"),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke([{"role": "user", "content": "read /tmp/a.py"}])
    assert "Let me read" in resp.text
    assert len(resp.tool_calls) == 1


# ====== 5. 异常映射 ======

@pytest.mark.asyncio
async def test_error_mapping_rate_limit():
    client = MockClient([
        StreamChunk(type="error", error="429 Too Many Requests"),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke([{"role": "user", "content": "hi"}])
    assert "Rate limited" in (resp.error or "")


@pytest.mark.asyncio
async def test_error_mapping_auth():
    client = MockClient([
        StreamChunk(type="error", error="401 Unauthorized"),
    ])
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke([{"role": "user", "content": "hi"}])
    assert "Authentication" in (resp.error or "")


# ====== 6. 空客户端 ======

@pytest.mark.asyncio
async def test_no_client_returns_error():
    invoker = LLMInvoker()  # 未注入 model_client
    resp = await invoker.invoke([{"role": "user", "content": "hi"}])
    assert "No model client" in (resp.error or "")


# ====== 7. 成本熔断 ======

@pytest.mark.asyncio
async def test_cost_circuit_breaker():
    from core.policy_engine import PolicyEngine
    engine = PolicyEngine()
    engine._circuit_breaker._cumulative = 100.0
    engine._circuit_breaker._state = "open"

    client = MockClient([StreamChunk(type="done")])
    invoker = LLMInvoker(model_client=client, policy_engine=engine)
    resp = await invoker.invoke([{"role": "user", "content": "hi"}])
    assert "熔断" in (resp.error or "") or "circuit" in (resp.error or "").lower()


# ====== 8. done 信号 ======

@pytest.mark.asyncio
async def test_done_finish_reason():
    for reason in ["stop", "tool_calls", "length"]:
        client = MockClient([
            StreamChunk(type="text", content="x"),
            StreamChunk(type="done", finish_reason=reason),
        ])
        invoker = LLMInvoker(model_client=client)
        resp = await invoker.invoke([{"role": "user", "content": "hi"}])
        assert resp.finish_reason == reason
