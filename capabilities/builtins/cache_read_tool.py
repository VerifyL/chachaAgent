"""
capabilities/builtins/cache_read_tool.py
CacheReadTool — 续读被截断工具的完整缓存输出。

当任意工具输出超过 200,000 字符被截断时，tool_executor 会将完整输出
缓存在内存中（10 分钟过期），并返回 cache_key。LLM 通过此工具凭
cache_key 分页读取完整输出。
"""

import logging
from typing import Any

from capabilities.base import BaseTool
from capabilities.result import ToolResult

logger = logging.getLogger(__name__)


class CacheReadTool(BaseTool):
    """续读被截断的缓存输出。Tier 3 工具，仅在截断发生时有意义。"""

    name = "cache_read"
    description = (
        "续读被截断工具的完整输出。当工具返回 truncated=true 且带有 cache_key 时，"
        "使用此工具按页读取缓存的完整内容。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "cache_key": {
                "type": "string",
                "description": "截断时返回的 cache_key",
            },
            "offset": {
                "type": "integer",
                "description": "起始字符偏移（0-based），默认 0",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "最大读取字符数，默认 500",
                "default": 500,
            },
        },
        "required": ["cache_key"],
    }

    risk = "low"
    no_truncate = True  # 防止自身输出再触发截断（缓存续读不应被二次截断）

    def __init__(self):
        super().__init__()
        self._executor = None

    def configure(self, parent_tool_executor=None, **kwargs):
        """注入 ToolExecutor 依赖。由 agent_bridge.rebuild() 调用。"""
        if parent_tool_executor is not None:
            self._executor = parent_tool_executor

    async def execute(
        self,
        cache_key: str,
        offset: int = 0,
        limit: int = 500,
    ) -> ToolResult:
        """从缓存中读取指定偏移的内容。"""
        offset = int(offset)
        limit = int(limit)
        if not self._executor:
            return ToolResult(
                status="error",
                content="",
                error="缓存读取器未配置（主 Agent 初始化未完成？）",
                error_type="internal_error",
            )

        # 先清理过期缓存
        self._executor._cleanup_cache()

        try:
            cached = self._executor._get_cached_output(cache_key, offset, limit)
        except Exception as e:
            logger.error("CacheReadTool: 读取缓存失败: %s", e)
            return ToolResult(
                status="error",
                content="",
                error=f"读取缓存失败: {e}",
                error_type="internal_error",
            )

        # _get_cached_output 在缓存缺失时返回错误信息字符串
        if cached.startswith("[错误]"):
            return ToolResult(
                status="error",
                content=cached,
                error="缓存已过期或不存在",
                error_type="cache_miss",
            )

        return ToolResult(
            status="success",
            content=cached,
        )
