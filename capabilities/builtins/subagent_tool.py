"""
capabilities/builtins/subagent_tool.py
SubAgentTool — 派生子Agent 执行独立任务（BaseTool）。

LLM 自主调用:
  subagent(type="explore", task="找到所有循环依赖")

注册到 ToolExecutor → LLM 在工具列表中看到 → 根据 description 判断是否委托。
运行时依赖由 AgentBridge 通过 configure() 注入。
"""

import logging
import uuid
from typing import Any, Optional

from capabilities.base import BaseTool

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
    risk = "medium"
    requires_approval = False

    def __init__(self):
        self._spawner = None
        self._llm = None
        self._parent_executor = None
        self._project_root = None
        self._telemetry = None

    def configure(self, llm_invoker, parent_tool_executor,
                  project_root=None, telemetry=None) -> None:
        """由 AgentBridge.initialize() 调用，注入运行时依赖。"""
        self._llm = llm_invoker
        self._parent_executor = parent_tool_executor
        self._project_root = project_root
        self._telemetry = telemetry

    async def execute(self, type: str, task: str) -> str:
        if self._spawner is None:
            if self._llm is None or self._parent_executor is None:
                return "[错误] 子Agent 未配置（AgentBridge 未调用 configure()）"
            from core.subagent.spawner import SubAgentSpawner
            self._spawner = SubAgentSpawner(
                self._llm, self._parent_executor,
                project_root=self._project_root,
                telemetry=self._telemetry,
            )

        subagent_id = f"sub-{type}-{uuid.uuid4().hex[:8]}"
        result = await self._spawner.spawn(type, task)
        # 缓存完整结果供 expand_subagent 展开
        from capabilities.builtins.expand_subagent import ExpandSubAgentTool
        ExpandSubAgentTool.cache_result(subagent_id, result.text)

        preview = result.text[:500] + ("..." if len(result.text) > 500 else "")
        return (
            f"[子Agent: {result.agent_type}] [ID: {subagent_id}]\n"
            f"任务: {result.task}\n"
            f"状态: {result.status}\n"
            f"Token: {result.tokens_used} | 工具调用: {result.tool_calls_made}次\n"
            f"耗时: {result.duration_ms}ms\n\n"
            f"{preview}\n\n"
            f"[使用 expand_subagent(\"{subagent_id}\") 查看详情]"
        )
