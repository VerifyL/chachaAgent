"""
tests/integration/test_orchestrator.py
集成测试：端到端模拟任务（读文件→回复）
"""

import pytest

from core.orchestrator import Orchestrator, OrchResponse
from core.context_manager import ContextManager
from core.llm_invoker import LLMInvoker, StreamChunk
from core.tool_executor import ToolExecutor


# ====== Mock 模型客户端：返回 tool_call → text ======

class MockReadFileClient:
    """模拟 LLM：第一轮返回 read_file 工具调用，第二轮返回文本"""

    def __init__(self):
        self._call_count = 0

    async def stream(self, messages, tools):
        self._call_count += 1
        if self._call_count == 1:
            # 第一轮：返回工具调用
            yield StreamChunk(type="tool_call_start", tool_index=0,
                              tool_id="c1", tool_name="read_file")
            yield StreamChunk(type="tool_call_delta", tool_index=0,
                              tool_args_delta='{"path": "/tmp/main.py"}')
            yield StreamChunk(type="tool_call_end", tool_index=0)
            yield StreamChunk(type="done", finish_reason="tool_calls")
        else:
            # 第二轮：返回文本
            yield StreamChunk(type="text", content="文件内容是 print('hello')")
            yield StreamChunk(type="done", finish_reason="stop")


# ====== Mock 工具：echo ======

async def _read_file(args):
    path = args.get("path", "")
    return f"content of {path}"


# ====== 测试 ======

@pytest.mark.asyncio
async def test_read_file_task():
    """端到端：用户要求读文件 → Agent 调用工具 → 得到结果 → 回复"""
    client = MockReadFileClient()
    tools = ToolExecutor({"read_file": _read_file})
    llm = LLMInvoker(model_client=client)
    ctx_mgr = ContextManager()
    orch = Orchestrator(
        context_manager=ctx_mgr,
        llm_invoker=llm,
        tool_executor=tools,
    )

    resp = await orch.run(
        "帮我读一下 /tmp/main.py",
        session_id="s1",
        project_id="p1",
    )

    assert resp.text == "文件内容是 print('hello')"
    assert resp.iterations >= 2  # 至少一轮 LLM + 一轮工具执行
    assert resp.error is None


@pytest.mark.asyncio
async def test_empty_llm_invoker():
    """无 LLMInvoker → 返回错误"""
    orch = Orchestrator()
    resp = await orch.run("hello", session_id="s1")
    assert "No LLM invoker" in (resp.error or "")


@pytest.mark.asyncio
async def test_text_only_task():
    """纯文本对话（无工具调用）"""

    class TextOnlyClient:
        async def stream(self, messages, tools):
            yield StreamChunk(type="text", content="你好，有什么可以帮助你的？")
            yield StreamChunk(type="done", finish_reason="stop")

    llm = LLMInvoker(model_client=TextOnlyClient())
    orch = Orchestrator(context_manager=ContextManager(), llm_invoker=llm)

    resp = await orch.run("你好", session_id="s1")
    assert "你好" in resp.text
    assert resp.iterations == 1
