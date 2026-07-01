"""
capabilities/mcp/adapter.py
MCPToolAdapter — 将 MCP 工具的 JSON Schema 包装为 BaseTool。

对 ToolExecutor 完全透明：和 ReadTool、WriteTool 等内置工具
使用相同的 BaseTool 接口，通过 function calling schema 注入 LLM。
"""

import logging
from typing import Any, Dict

from capabilities.base import BaseTool
from capabilities.result import ToolResult

logger = logging.getLogger(__name__)

# 副作用关键词：匹配到则标记为高风险 + 需审批
_SIDE_EFFECT_KEYWORDS = [
    "write", "delete", "remove", "execute", "run",
    "create", "update", "insert", "drop", "alter",
    "move", "rename", "chmod", "chown", "kill",
    "deploy", "push", "publish",
]


class MCPToolAdapter(BaseTool):
    """将单个 MCP 工具的 JSON Schema 适配为 BaseTool。

    name 自动加前缀避免与内置工具冲突: mcp__{server}__{tool_name}
    risk / requires_approval 根据工具名自动推断。
    """

    def __init__(
        self,
        mcp_client: Any,       # MCPClient（避免循环导入）
        server_name: str,
        tool_schema: Dict[str, Any],
    ):
        self._client = mcp_client
        self._server = server_name
        self._tool_name = tool_schema["name"]

        # ── BaseTool 元数据 ──
        self.name = f"mcp__{server_name}__{self._tool_name}"
        self.description = tool_schema.get("description", "")
        self.parameters = tool_schema.get("inputSchema", {})

        # ── 安全分类 ──
        name_lower = self._tool_name.lower()
        if any(kw in name_lower for kw in _SIDE_EFFECT_KEYWORDS):
            self.risk = "high"
            self.requires_approval = True
        else:
            self.risk = "medium"
            self.requires_approval = False

    # ====== 执行 ======

    async def execute(self, **kwargs: Any) -> ToolResult:
        """委托给 MCPClient.call_tool() 远程执行。"""
        return await self._client.call_tool(
            server_name=self._server,
            tool_name=self._tool_name,
            arguments=kwargs,
        )

    # ====== 辅助 ======

    def __repr__(self) -> str:
        return f"<MCPToolAdapter mcp__{self._server}__{self._tool_name}>"
