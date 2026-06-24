"""
capabilities/builtins/expand_subagent.py
ExpandSubAgentTool — 展开子Agent 结果查看详情（BaseTool）。

SubAgentTool 返回结果中引用此工具，LLM 用于查看子Agent 完整输出。
"""

import logging
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)


class ExpandSubAgentTool(BaseTool):
    """展开查看子Agent 详细结果：expand_subagent(subagent_id)"""

    name = "expand_subagent"
    description = "展开查看之前子Agent 的完整执行结果。在 subagent 返回中看到 ID 时调用此工具获取详情。"
    parameters = {
        "type": "object",
        "properties": {
            "subagent_id": {
                "type": "string",
                "description": "子Agent ID（subagent 返回结果中的 [ID: xxx] 值）",
            },
        },
        "required": ["subagent_id"],
    }
    risk = "low"
    requires_approval = False

    _results_cache: dict = {}

    @classmethod
    def cache_result(cls, subagent_id: str, full_text: str) -> None:
        """由 SubAgentTool 在返回前调用，缓存完整结果。"""
        cls._results_cache[subagent_id] = full_text

    async def execute(self, subagent_id: str) -> str:
        text = self._results_cache.get(subagent_id)
        if not text:
            return f"[错误] 未找到子Agent: {subagent_id}（可能已过期）"
        return text
