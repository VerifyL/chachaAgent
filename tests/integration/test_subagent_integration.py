"""
tests/integration/test_subagent_integration.py
集成测试：主Agent 分配任务给子Agent + 汇总结果
"""

import tempfile
from pathlib import Path

import pytest

from core.subagent.spawner import SubAgentSpawner
from core.tool_executor import ToolExecutor
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool


class MockLLM:
    async def invoke(self, messages, tools=None, session_id=""):
        from core.llm_invoker import LLMResponse
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")

        if "梳理" in user_msg or "探索" in user_msg:
            return LLMResponse(
                text="发现 main.py 定义了 hello(), world()；util.py 导入 os 并定义了 bar()",
                finish_reason="stop",
            )
        return LLMResponse(text="done", finish_reason="stop")


@pytest.fixture
def project_root():
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("def hello():\n    return 'hi'\n\ndef world():\n    return 'earth'\n")
    (d / "src" / "util.py").parent.mkdir(parents=True, exist_ok=True)
    (d / "src" / "util.py").write_text("import os\ndef bar():\n    return 7\n")
    return d


@pytest.mark.asyncio
async def test_main_delegates_to_explore(project_root):
    """主Agent 分配探索任务 → 子Agent 返回结果"""
    parent_tools = ToolExecutor(tools=[
        ReadFileTool(root=project_root),
        GrepTool(root=project_root),
    ])
    llm = MockLLM()
    spawner = SubAgentSpawner(llm, parent_tools)

    result = await spawner.spawn("explore", "梳理项目代码结构")
    assert result.status == "success"
    assert "hello" in result.text.lower()


@pytest.mark.asyncio
async def test_subagent_timeout():
    """超时 → status=timeout"""
    import asyncio

    class HangingLLM:
        async def invoke(self, messages, tools=None, session_id=""):
            await asyncio.sleep(999)  # 挂起超时

    spawner = SubAgentSpawner(HangingLLM())
    result = await spawner.spawn("explore", "无限任务", timeout=0.05)
    assert result.status == "timeout"


@pytest.mark.asyncio
async def test_all_three_types():
    """三种子Agent 类型都存在"""
    from core.subagent.definitions import SUBAGENT_DEFINITIONS

    assert "explore" in SUBAGENT_DEFINITIONS
    assert "plan" in SUBAGENT_DEFINITIONS
    assert "worker" in SUBAGENT_DEFINITIONS

    explore = SUBAGENT_DEFINITIONS["explore"]
    assert explore.max_rounds == 25
    assert explore.tools_whitelist == ["read_file", "grep", "read_cached_output"]
