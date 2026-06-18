"""
tests/unit/test_subagent.py
单元测试：core/subagent/ 子Agent 系统
"""

import pytest

from core.subagent.definitions import SUBAGENT_DEFINITIONS, SubAgentDef
from core.subagent.spawner import SubAgentSpawner, SubAgentResult
from core.tool_executor import ToolExecutor
from capabilities.base import BaseTool


# ====== Fixtures ======

class MockBaseTool(BaseTool):
    name = "read_file"
    description = "read"
    risk = "low"

    async def execute(self, **kwargs):
        return "file content"


class MockNoNameTool:
    """没有 .name 属性的旧式工具，应被过滤"""
    pass


# ====== 1. 定义完整 ======

def test_definitions_have_three_types():
    assert "explore" in SUBAGENT_DEFINITIONS
    assert "plan" in SUBAGENT_DEFINITIONS
    assert "worker" in SUBAGENT_DEFINITIONS


def test_explore_skips_claude_md():
    assert SUBAGENT_DEFINITIONS["explore"].skip_claude_md is True


def test_explore_has_grep_and_read_file():
    tools = SUBAGENT_DEFINITIONS["explore"].tools_whitelist
    assert "read_file" in tools
    assert "grep" in tools
    assert "shell" not in tools


def test_worker_has_edit_file():
    assert "edit_file" in SUBAGENT_DEFINITIONS["worker"].tools_whitelist


# ====== 2. 工具过滤 ======

def test_build_tools_filters_by_whitelist():
    parent = ToolExecutor(tools=[MockBaseTool()])
    spawner = SubAgentSpawner(None, parent)
    definition = SUBAGENT_DEFINITIONS["explore"]

    tools = spawner._build_tools(definition)
    schemas = tools.get_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "read_file"


def test_build_tools_no_parent():
    spawner = SubAgentSpawner(None)
    definition = SUBAGENT_DEFINITIONS["explore"]
    tools = spawner._build_tools(definition)
    assert tools.get_schemas() == []


# ====== 3. 未知类型 ======

@pytest.mark.asyncio
async def test_spawn_unknown_type():
    spawner = SubAgentSpawner(None)
    result = await spawner.spawn("unknown", "task")
    assert result.status == "error"
    assert "未知" in result.text


# ====== 4. 子Agent 正常工作（Mock） ======

class MockLLM:
    async def invoke(self, messages, tools=None, session_id=""):
        from core.llm_invoker import LLMResponse
        return LLMResponse(text="探索结果: 找到 3 个循环依赖", finish_reason="stop")


@pytest.mark.asyncio
async def test_spawn_explore():
    parent = ToolExecutor(tools=[MockBaseTool()])
    llm = MockLLM()
    spawner = SubAgentSpawner(llm, parent)

    result = await spawner.spawn("explore", "梳理依赖")
    assert result.status == "success"
    assert "循环依赖" in result.text
    assert result.duration_ms >= 0
