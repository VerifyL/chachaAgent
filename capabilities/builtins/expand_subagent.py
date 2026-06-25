"""
capabilities/builtins/expand_subagent.py
ExpandSubAgentTool — 展开子Agent 结果查看详情（BaseTool）。

SubAgentTool 返回结果中引用此工具，LLM 用于查看子Agent 完整输出。
"""

import logging
import time
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
    _results_timestamps: dict = {}
    _RESULTS_CACHE_TTL = 600  # 10 分钟，与 ToolExecutor._output_cache TTL 独立

    @classmethod
    def _cleanup(cls) -> None:
        """清理过期缓存条目。"""
        now = time.time()
        expired = [k for k, ts in cls._results_timestamps.items()
                   if now - ts > cls._RESULTS_CACHE_TTL]
        for k in expired:
            cls._results_cache.pop(k, None)
            cls._results_timestamps.pop(k, None)

    @classmethod
    def cache_result(cls, subagent_id: str, full_text: str) -> None:
        """由 SubAgentTool 在返回前调用，缓存完整结果。写入前先清理过期条目。"""
        cls._cleanup()
        cls._results_cache[subagent_id] = full_text
        cls._results_timestamps[subagent_id] = time.time()

    async def execute(self, subagent_id: str) -> str:
        self._cleanup()
        text = self._results_cache.get(subagent_id)
        if not text:
            return f"[错误] 未找到子Agent: {subagent_id}（可能已过期）"
        return text
