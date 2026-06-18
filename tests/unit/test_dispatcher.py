"""
tests/unit/test_dispatcher.py
单元测试：core/dispatcher.py Dispatcher
"""

import pytest

from core.dispatcher import Dispatcher
from core.llm_invoker import LLMResponse, ToolCall


# ====== Mock 实现 ======

class MockLLM:
    def __init__(self, responses=None):
        self._responses = responses or []
        self._idx = 0

    async def invoke(self, messages, tools=None, session_id=""):
        resp = self._responses[self._idx] if self._idx < len(self._responses) else LLMResponse(text="done")
        self._idx += 1
        return resp


class MockTools:
    def __init__(self):
        self.executed: list[tuple] = []

    def get_schemas(self):
        return [{"type": "function", "function": {"name": "read_file"}}]

    async def execute(self, tool_name, arguments, session_id="", tool_use_id=""):
        self.executed.append((tool_name, arguments))
        from core.tool_executor import ToolResult
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output=f"read {arguments.get('path', '')}", status="success",
        )


# ====== 1. 纯文本（无工具调用） ======

@pytest.mark.asyncio
async def test_text_only_no_tools():
    llm = MockLLM([LLMResponse(text="Hello, world!", finish_reason="stop")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "Hello" in resp.text
    assert len(tools.executed) == 0


# ====== 2. 工具调用链 ======

@pytest.mark.asyncio
async def test_dispatch_with_tool_call():
    llm = MockLLM([
        LLMResponse(
            text="Let me read that",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "main.py"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(text="File contents: print('hi')", finish_reason="stop"),
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "read main.py"}], "s1")
    assert "File contents" in resp.text
    assert len(tools.executed) == 1
    assert tools.executed[0][0] == "read_file"


# ====== 3. schema 属性 ======

def test_tool_count():
    d = Dispatcher(MockLLM(), MockTools())
    assert d.tool_count == 1
    assert len(d.schemas) == 1


# ====== 4. 错误处理 ======

@pytest.mark.asyncio
async def test_dispatch_llm_error():
    llm = MockLLM([LLMResponse(text="", error="API error")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert resp.finish_reason == "error"


# ====== 5. 无 schema（空工具列表） ======

@pytest.mark.asyncio
async def test_dispatch_no_tools():
    t = MockTools()
    t.get_schemas = lambda: []
    d = Dispatcher(MockLLM([LLMResponse(text="no tools needed")]), t)
    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "no tools needed" in resp.text
