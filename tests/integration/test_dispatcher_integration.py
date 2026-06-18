"""
tests/integration/test_dispatcher_integration.py (v2.0)
集成测试：Dispatcher + ToolExecutor + 真实工具

v2.0 新增:
  - Stage 1 工具结果缓存 (KEEP_TOOL_RESULTS=10)
  - JSON 占位符格式验证
  - 与 MemoryManager 集成
"""

import json
import tempfile
from pathlib import Path

import pytest

from core.dispatcher import Dispatcher, KEEP_TOOL_RESULTS
from core.llm_invoker import LLMResponse, ToolCall
from core.tool_executor import ToolExecutor
from core.context.memory_manager import MemoryManager
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool


# ====== Mock LLM ======

class MockLLM:
    async def invoke(self, messages, tools=None, session_id=""):
        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                text="I'll read the file",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "main.py"})],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            text="文件内容已读取完毕",
            finish_reason="stop",
        )


@pytest.fixture
def project_root():
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("print('hello')\nx = 42\n")
    return d


@pytest.fixture
def memory():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-int")


# ====== 基本集成 ======

@pytest.mark.asyncio
async def test_dispatcher_with_real_tools(project_root):
    """Dispatcher + ReadFileTool 完整链路"""
    tools = ToolExecutor(tools=[ReadFileTool(root=project_root), GrepTool(root=project_root)])
    llm = MockLLM()
    dispatcher = Dispatcher(llm, tools)

    resp = await dispatcher.dispatch(
        [{"role": "user", "content": "读 main.py"}],
        "s1",
    )
    assert resp.finish_reason == "stop"
    assert "读取完毕" in resp.text


@pytest.mark.asyncio
async def test_dispatcher_tool_count(project_root):
    tools = ToolExecutor(tools=[ReadFileTool(root=project_root), GrepTool(root=project_root)])
    dispatcher = Dispatcher(None, tools)
    assert dispatcher.tool_count == 2
    assert len(dispatcher.schemas) == 2


# ====== v2.0: Stage 1 工具缓存 ======

@pytest.mark.asyncio
async def test_dispatcher_with_memory_manager(project_root, memory):
    """Dispatcher 与 MemoryManager 集成"""
    tools = ToolExecutor(tools=[ReadFileTool(root=project_root)])
    llm = MockLLM()
    dispatcher = Dispatcher(llm, tools, memory_manager=memory)

    resp = await dispatcher.dispatch(
        [{"role": "user", "content": "读 main.py"}],
        "s1",
    )
    assert resp.finish_reason == "stop"


def test_freeze_with_many_tools_creates_placeholders(memory):
    """多次工具调用 → 旧的变为 JSON 占位符"""
    llm = MockLLM()
    tools = ToolExecutor(tools=[])

    class DummyTool:
        def get_schemas(self): return []
        async def execute(self, **kw):
            from core.tool_executor import ToolResult
            return ToolResult(tool_use_id=kw.get("tool_use_id", ""),
                              tool_name=kw.get("tool_name", ""),
                              output="result", status="success")

    dispatcher = Dispatcher(llm, DummyTool(), memory_manager=memory)

    # 构建 20 个工具调用
    messages = [{"role": "user", "content": "do many things"}]
    for i in range(20):
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

    dispatcher._freeze_old_tool_results(messages, "s-int")

    # 前 10 个（20-10）变占位符
    frozen = 0
    for i in range(10):
        content = messages[i * 2 + 2]["content"]
        if content.startswith("{"):
            data = json.loads(content)
            assert "toolname" in data
            assert "result_summary" in data
            assert "cache_path" in data
            frozen += 1

    assert frozen == 10

    # 后 10 个保持完整
    for i in range(10, 20):
        assert messages[i * 2 + 2]["content"].startswith("file content")
