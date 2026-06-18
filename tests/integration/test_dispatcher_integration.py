"""
tests/integration/test_dispatcher_integration.py
集成测试：Dispatcher + ToolExecutor + 真实工具
"""

import tempfile
from pathlib import Path

import pytest

from core.dispatcher import Dispatcher
from core.llm_invoker import LLMResponse, ToolCall
from core.tool_executor import ToolExecutor
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool


# ====== Mock LLM（模拟工具调用） ======

class MockLLM:
    async def invoke(self, messages, tools=None, session_id=""):
        # 第一轮：请求 read_file
        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                text="I'll read the file",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "main.py"})],
                finish_reason="tool_calls",
            )
        # 第二轮：看到结果后回复
        return LLMResponse(
            text=f"文件内容已读取完毕",
            finish_reason="stop",
        )


@pytest.fixture
def project_root():
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("print('hello')\nx = 42\n")
    return d


# ====== 测试 ======

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
