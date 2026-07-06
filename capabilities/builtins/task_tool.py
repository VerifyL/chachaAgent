"""
capabilities/builtins/task_tool.py
TaskTool — 委派子Agent 执行独立任务。

LLM 调用 task 工具孵化 explore/plan/worker 子Agent，
在隔离上下文中执行，结果压缩回传。
"""

import logging
from typing import Any, Dict, Optional

from capabilities.base import BaseTool
from capabilities.result import ToolResult

logger = logging.getLogger(__name__)


class TaskTool(BaseTool):
    """委派子Agent 执行独立任务。

    参数:
        subagent_type: "explore" | "plan" | "worker"
        description: 任务描述
        prompt: 详细 prompt（可选，默认用 description）
    """

    name = "task"
    description = (
        "委派子Agent 执行独立任务。explore=只读探索代码库，plan=分析并制定计划，"
        "worker=执行实际修改。子Agent 在隔离上下文中运行，结果回传。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "subagent_type": {
                "type": "string",
                "enum": ["explore", "plan", "worker"],
                "description": "子Agent 类型：explore（只读探索）、plan（分析计划）、worker（执行修改）",
            },
            "description": {
                "type": "string",
                "description": "委派任务描述",
            },
            "prompt": {
                "type": "string",
                "description": "传给子Agent 的详细 prompt（可选，默认用 description）",
            },
        },
        "required": ["subagent_type", "description"],
    }

    def __init__(self):
        super().__init__()
        self._spawner = None

    def configure(self, subagent_spawner=None, **kwargs):
        """注入 SubAgentSpawner 依赖。由 agent_bridge.rebuild() 调用。"""
        if subagent_spawner is not None:
            self._spawner = subagent_spawner

    async def execute(
        self,
        subagent_type: str,
        description: str,
        prompt: Optional[str] = None,
        timeout: float = 300,
    ) -> ToolResult:
        """孵化子Agent 并等待结果。"""
        if not self._spawner:
            return ToolResult(
                status="error",
                content="",
                error="子Agent spawner 未配置（主 Agent 初始化未完成？）",
                error_type="internal_error",
            )

        task_prompt = prompt or description

        logger.info("TaskTool: 委派 %s 子Agent → %s", subagent_type, description[:120])

        try:
            result = await self._spawner.spawn(
                agent_type=subagent_type,
                task=task_prompt,
                timeout=timeout,
            )
        except Exception as e:
            logger.error("TaskTool: 子Agent 异常: %s", e)
            return ToolResult(
                status="error",
                content=f"[子Agent 异常] {type(e).__name__}: {e}",
                error=str(e),
                error_type="subagent_error",
            )

        # 子Agent 返回空文本 → 追加提示
        content = result.text
        if not content or not content.strip():
            content = f"[子Agent {subagent_type} 返回了空结果（执行了 {result.tool_calls_made} 次工具调用，耗时 {result.duration_ms}ms）]"

        # 映射 SubAgentResult → ToolResult
        status = "success" if result.status == "success" else "error"
        error_type = None
        error = None

        if result.status == "timeout":
            error_type = "timeout"
            error = f"子Agent {subagent_type} 超时（>{timeout}s）"
        elif result.status == "error":
            error_type = "subagent_error"
            error = content if "[错误]" in content or "[超时]" in content else ""

        return ToolResult(
            status=status,
            content=content,
            error=error,
            error_type=error_type,
            truncated=result.truncated,
            data={
                "agent_type": result.agent_type,
                "task": result.task,
                "tool_calls": result.tool_calls_made,
                "tokens": result.tokens_used,
                "duration_ms": result.duration_ms,
            },
        )
