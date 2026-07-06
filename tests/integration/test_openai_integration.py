"""
tests/integration/test_openai_integration.py
集成测试：OpenAI 客户端 + LLMInvoker 联调，Mock 后端流式调用
"""

import pytest

from core.llm_clients.openai_client import OpenAIClient
from core.llm_invoker import LLMInvoker

# ====== Mock OpenAI 后端（完整流式事件） ======


class MockDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class MockChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class MockToolCall:
    def __init__(self, index=0, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


class MockFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class MockUsage:
    def __init__(self, prompt=100, completion=50, total=150):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class MockEvent:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class MockResponse:
    def __init__(self, events):
        self._events = events

    async def __aiter__(self):
        for e in self._events:
            yield e


# ====== 完整流式：文本 + 工具调用 + LLMInvoker 集成 ======


@pytest.mark.asyncio
async def test_full_stream_with_llm_invoker():
    """OpenAI 客户端 → LLMInvoker 全链路流式"""
    client = OpenAIClient(model="gpt-4")

    events = [
        MockEvent([MockChoice(MockDelta(content="I'll read that file."))]),
        MockEvent(
            [
                MockChoice(
                    MockDelta(
                        tool_calls=[
                            MockToolCall(index=0, id="call-1", function=MockFunction(name="read_file")),
                        ]
                    )
                )
            ]
        ),
        MockEvent(
            [
                MockChoice(
                    MockDelta(
                        tool_calls=[
                            MockToolCall(index=0, function=MockFunction(arguments='{"path":"/tmp/main.py"}')),
                        ]
                    )
                )
            ]
        ),
        MockEvent(
            [
                MockChoice(MockDelta(), finish_reason="tool_calls"),
            ],
            usage=MockUsage(100, 50, 150),
        ),
    ]

    async def mock_create(**kw):
        return MockResponse(events)

    client._client.chat.completions.create = mock_create

    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke(
        messages=[{"role": "user", "content": "Read main.py"}],
        session_id="s1",
    )

    assert "I'll read" in resp.text
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].id == "call-1"
    assert resp.tool_calls[0].arguments.get("path") == "/tmp/main.py"
    assert resp.finish_reason == "tool_calls"
    assert resp.usage["total"] == 150


@pytest.mark.asyncio
async def test_text_only_with_llm_invoker():
    """纯文本流式 → LLMInvoker 返回完整文本"""
    client = OpenAIClient(model="gpt-4")

    events = [
        MockEvent([MockChoice(MockDelta(content="Hello, "))]),
        MockEvent([MockChoice(MockDelta(content="world!"))]),
        MockEvent(
            [
                MockChoice(MockDelta(), finish_reason="stop"),
            ],
            usage=MockUsage(10, 5, 15),
        ),
    ]

    async def mock_create(**kw):
        return MockResponse(events)

    client._client.chat.completions.create = mock_create

    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke(
        messages=[{"role": "user", "content": "Say hello"}],
        session_id="s1",
    )

    assert resp.text == "Hello, world!"
    assert resp.finish_reason == "stop"
    assert len(resp.tool_calls) == 0
