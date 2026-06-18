"""
capabilities/builtins/subagent_tool.py
SubAgentTool — 派生子Agent 执行独立任务（BaseTool）。

LLM 自主调用:
  subagent(type="explore", task="找到所有循环依赖")

注册到 ToolExecutor → LLM 在工具列表中看到 → 根据 description 判断是否委托
"""

import logging
from typing import Optional

from capabilities.base import BaseTool
from core.subagent.spawner import SubAgentSpawner

logger = logging.getLogger(__name__)


class SubAgentTool(BaseTool):
    """派生子Agent：subagent(type, task)"""

    name = "subagent"
    description = (
        "派生子Agent 执行独立任务。使用时机：需要跨多文件搜索、复杂分析、或独立代码修改。"
        "type: explore（代码搜索）/ plan（规划设计）/ worker（执行修改）。"
        "task: 具体的任务描述。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["explore", "plan", "worker"],
                "description": "子Agent类型: explore=代码探索, plan=规划设计, worker=执行修改",
            },
            "task": {"type": "string", "description": "任务描述"},
        },
        "required": ["type", "task"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, spawner: Optional[SubAgentSpawner] = None):
        self._spawner = spawner

    async def execute(self, type: str, task: str) -> str:
        if not self._spawner:
            return "[错误] 子Agent 孵化器未初始化"

        result = await self._spawner.spawn(type, task)
        return (
            f"[子Agent: {result.agent_type}]\n"
            f"任务: {result.task}\n"
            f"状态: {result.status}\n"
            f"Token: {result.tokens_used}\n"
            f"耗时: {result.duration_ms}ms\n\n"
            f"{result.text}"
        )
