"""
tests/unit/test_dispatcher.py
单元测试：core/dispatcher.py Dispatcher (v2.0)

新增覆盖：
  - Stage 1 工具结果缓存（JSON 占位符）
  - _freeze_old_tool_results (KEEP_TOOL_RESULTS=10)
  - _guess_tool_name
  - JSON 占位符格式验证
"""

import json
import tempfile
from pathlib import Path

import pytest

from core.dispatcher import Dispatcher, KEEP_TOOL_RESULTS
from core.llm_invoker import LLMResponse, ToolCall
from core.context.memory_manager import MemoryManager


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

    async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
        self.executed.append((tool_name, arguments))
        from core.tool_executor import ToolResult
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output=f"read {arguments.get('path', '')}", status="success",
        )


@pytest.fixture
def memory():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-001")


# ====== 1. 纯文本（原有） ======

@pytest.mark.asyncio
async def test_text_only_no_tools():
    llm = MockLLM([LLMResponse(text="Hello, world!", finish_reason="stop")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "Hello" in resp.text
    assert len(tools.executed) == 0


# ====== 2. 工具调用链（原有） ======

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


# ====== 3. schema 属性（原有） ======

def test_tool_count():
    d = Dispatcher(MockLLM(), MockTools())
    assert d.tool_count == 1
    assert len(d.schemas) == 1


# ====== 4. 错误处理（原有） ======

@pytest.mark.asyncio
async def test_dispatch_llm_error():
    llm = MockLLM([LLMResponse(text="", error="API error")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert resp.error is not None or resp.finish_reason in ("stop", "error")


# ====== 5. 无 schema（原有） ======

@pytest.mark.asyncio
async def test_dispatch_no_tools():
    t = MockTools()
    t.get_schemas = lambda: []
    d = Dispatcher(MockLLM([LLMResponse(text="no tools needed")]), t)
    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "no tools needed" in resp.text


# ====== v2.0: Stage 1 工具结果缓存 ======

@pytest.mark.asyncio
async def test_freeze_old_tool_results_below_threshold():
    """少于 KEEP_TOOL_RESULTS 个 → 不触发缓存"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools)

    messages = [
        {"role": "user", "content": "hi"},
    ]
    # 只添加 3 个工具结果
    for i in range(3):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i}: " + "x" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    # 全部保持完整
    for i in range(3):
        assert messages[i + 1]["content"].startswith("result")


@pytest.mark.asyncio
async def test_freeze_old_tool_results_above_threshold(memory):
    """超过 KEEP_TOOL_RESULTS 个 → 旧的变 JSON 占位符"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [{"role": "user", "content": "hi"}]
    # 添加 15 个工具结果
    for i in range(15):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i}: " + "x" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    # 前 5 个（15-10）变占位符
    for i in range(5):
        content = messages[i + 1]["content"]
        assert content.startswith("{")
        assert '"toolname"' in content
        assert '"result_summary"' in content
        assert '"cache_path"' in content

        # 最近 8 个保持完整（KEEP_TOOL_RESULTS=8）
        for i in range(7, 15):
            assert messages[i + 1]["content"].startswith("result")


@pytest.mark.asyncio
async def test_freeze_old_tool_results_json_format(memory):
    """验证 JSON 占位符格式"""
    llm = MockLLM([
        LLMResponse(
            text="Let me check",
            tool_calls=[ToolCall(id="c99", name="read_file", arguments={"path": "main.py"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(text="Done", finish_reason="stop"),
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [
        {"role": "user", "content": "read all files"},
    ]
    # 模拟之前的 assistant tool_calls 消息
    for i in range(15):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "f.py"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"file content {i}: " + "y" * 200,
        })

    # 触发 _guess_tool_name
    d._freeze_old_tool_results(messages, "s1")

    # 验证占位符可解析为 JSON
    for i in range(10):  # 查看前 10 个（至少前 5 个是占位符）
        if messages[i * 2 + 2]["content"].startswith("{"):
            data = json.loads(messages[i * 2 + 2]["content"])
            assert "toolname" in data
            assert "result_summary" in data
            assert "cache_path" in data


@pytest.mark.asyncio
async def test_freeze_skips_short_results(memory):
    """太短的结果 (<100 字符) 不缓存"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [{"role": "user", "content": "hi"}]
    for i in range(15):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"short {i}",  # < 100 字符
        })

    d._freeze_old_tool_results(messages, "s1")

    # 短结果全部保持原样
    for i in range(15):
        assert not messages[i + 1]["content"].startswith("{")


# ====== v2.0: _guess_tool_name ======

def test_guess_tool_name_finds_match():
    """从前一条 assistant 消息中匹配 tool_call_id"""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c42", "type": "function", "function": {"name": "grep_search"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c42", "content": "found 5 results"},
    ]

    name = Dispatcher._guess_tool_name(messages, 2)
    assert name == "grep_search"


def test_guess_tool_name_not_found():
    """未找到 → 'unknown'"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c99", "content": "result"},
    ]
    name = Dispatcher._guess_tool_name(messages, 1)
    assert name == "unknown"


# ====== v2.0: dispatcher 集成 memory_manager ======

def test_dispatcher_accepts_memory_manager(memory):
    """Dispatcher 接受 MemoryManager 参数"""
    d = Dispatcher(MockLLM(), MockTools(), memory_manager=memory)
    assert d._memory is not None


def test_dispatcher_without_memory_manager():
    """不传 MemoryManager 也可以"""
    d = Dispatcher(MockLLM(), MockTools())
    assert d._memory is None
    assert d.tool_count == 1
