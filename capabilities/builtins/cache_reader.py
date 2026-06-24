"""
capabilities/builtins/cache_reader.py
CacheReaderTool — 读取工具输出缓存，支持分页续读。

当工具输出被截断且提示包含 cache_key 时，LLM 调用此工具续读后续内容。
"""

from capabilities.base import BaseTool


class CacheReaderTool(BaseTool):
    """输出缓存分页续读工具。"""

    name = "read_cached_output"
    description = (
        "Read cached tool output by cache_key. "
        "Use when a previous tool result was truncated with a cache_key hint. "
        "Parameters: cache_key (required, from the truncation hint), "
        "offset (default 0), limit (default 500)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "cache_key": {
                "type": "string",
                "description": "The cache_key from a previous truncated output hint",
            },
            "offset": {
                "type": "integer",
                "description": "Starting character offset, default 0",
            },
            "limit": {
                "type": "integer",
                "description": "Max characters to return, default 500",
            },
        },
        "required": ["cache_key"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, tool_executor=None):
        self._executor = tool_executor

    def configure(self, parent_tool_executor=None, **kwargs) -> None:
        if parent_tool_executor is not None:
            self._executor = parent_tool_executor

    async def execute(
        self,
        cache_key: str = "",
        offset: int = 0,
        limit: int = 500,
    ) -> str:
        if self._executor is None:
            return "[错误] ToolExecutor 未注入，无法读取缓存"
        return self._executor._get_cached_output(cache_key, offset, limit)
