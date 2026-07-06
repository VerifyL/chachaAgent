"""
tests/unit/test_openai_client.py
单元测试：core/model/openai_client.py
覆盖：文本流、工具调用（单/多）、done 信号、usage 提取
"""

import pytest

from core.llm_clients.openai_client import OpenAIClient

# ====== Mock OpenAI 流式响应 ======


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


async def _patch_stream(client, events):
    """临时替换 client._client.chat.completions.create 为 async mock"""

    async def mock_create(**kw):
        return MockResponse(events)

    client._client.chat.completions.create = mock_create
    return client


# ====== 1. 纯文本流 ======


@pytest.mark.asyncio
async def test_text_stream():
    client = OpenAIClient(model="test")
    events = [
        MockEvent([MockChoice(MockDelta(content="Hello"))]),
        MockEvent([MockChoice(MockDelta(content=" world"))]),
        MockEvent(
            [MockChoice(MockDelta(), finish_reason="stop")],
            usage=MockUsage(10, 5, 15),
        ),
    ]
    await _patch_stream(client, events)

    chunks = []
    async for c in client.stream([{"role": "user", "content": "hi"}]):
        chunks.append(c)

    texts = [c.content for c in chunks if c.type == "text"]
    assert "".join(texts) == "Hello world"
    done = [c for c in chunks if c.type == "done"]
    assert len(done) == 1
    assert done[0].usage and done[0].usage["total"] == 15


# ====== 2. 工具调用 ======


@pytest.mark.asyncio
async def test_tool_call_stream():
    client = OpenAIClient(model="test")
    events = [
        MockEvent(
            [
                MockChoice(
                    MockDelta(
                        tool_calls=[
                            MockToolCall(index=0, id="call_1", function=MockFunction(name="read_file")),
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
                            MockToolCall(index=0, function=MockFunction(arguments='{"pa')),
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
                            MockToolCall(index=0, function=MockFunction(arguments='th":"/tmp"}}')),
                        ]
                    )
                )
            ]
        ),
        MockEvent([MockChoice(MockDelta(), finish_reason="tool_calls")]),
    ]
    await _patch_stream(client, events)

    chunks = []
    async for c in client.stream([{"role": "user", "content": "read"}], tools=[{}]):
        chunks.append(c)

    starts = [c for c in chunks if c.type == "tool_call_start"]
    assert len(starts) == 1
    assert starts[0].tool_name == "read_file"
    assert starts[0].tool_id == "call_1"

    deltas = [c for c in chunks if c.type == "tool_call_delta"]
    assert any('"/tmp"' in c.tool_args_delta for c in deltas)


# ====== 3. 多工具并行 ======


@pytest.mark.asyncio
async def test_multiple_parallel_tools():
    client = OpenAIClient(model="test")
    events = [
        MockEvent(
            [
                MockChoice(
                    MockDelta(
                        tool_calls=[
                            MockToolCall(index=0, id="c1", function=MockFunction(name="read_file")),
                            MockToolCall(index=1, id="c2", function=MockFunction(name="grep")),
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
                            MockToolCall(index=0, function=MockFunction(arguments='{"path":"/a"}')),
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
                            MockToolCall(index=1, function=MockFunction(arguments='{"pattern":"T"}')),
                        ]
                    )
                )
            ]
        ),
        MockEvent([MockChoice(MockDelta(), finish_reason="tool_calls")]),
    ]
    await _patch_stream(client, events)

    chunks = []
    async for c in client.stream([{"role": "user", "content": "search"}]):
        chunks.append(c)

    starts = [c for c in chunks if c.type == "tool_call_start"]
    assert len(starts) == 2
    assert starts[0].tool_name == "read_file"
    assert starts[1].tool_name == "grep"


# ====== 4. 文本 + 工具混合 ======


@pytest.mark.asyncio
async def test_text_and_tool():
    client = OpenAIClient(model="test")
    events = [
        MockEvent([MockChoice(MockDelta(content="Let me check."))]),
        MockEvent(
            [
                MockChoice(
                    MockDelta(
                        tool_calls=[
                            MockToolCall(index=0, id="c1", function=MockFunction(name="read_file")),
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
                            MockToolCall(index=0, function=MockFunction(arguments='{"path":"/x"}')),
                        ]
                    )
                )
            ]
        ),
        MockEvent([MockChoice(MockDelta(), finish_reason="tool_calls")]),
    ]
    await _patch_stream(client, events)

    chunks = []
    async for c in client.stream([{"role": "user", "content": "hi"}]):
        chunks.append(c)

    types = [c.type for c in chunks]
    assert "text" in types
    assert "tool_call_start" in types
    assert "done" in types


# ====== 5. 构造函数 ======


def test_constructor_with_base_url():
    client = OpenAIClient(model="deepseek-chat", base_url="https://api.deepseek.com/v1")
    assert client._model == "deepseek-chat"


def test_constructor_defaults():
    client = OpenAIClient()
    assert client._model == "gpt-4"
