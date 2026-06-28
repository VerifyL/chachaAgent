"""
capabilities/mcp_client.py
MCPClient — MCP 协议客户端骨架。

TODO(阶段8): 实现 JSON-RPC 传输层（stdio + Streamable HTTP）
TODO(阶段8): 实现工具动态发现与 list_tools()
TODO(阶段8): 实现双层 Schema 缓存（TTL + LRU）
TODO(阶段8): 实现进程生命周期管理（启动/心跳/优雅关闭）
TODO(阶段8): 实现 MCPToolAdapter（MCP tool → BaseTool）
TODO(阶段8): 实现权限与审计集成

参考: Harness MiniHarness MCP
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP 协议客户端骨架。

    连接外部 MCP server，动态发现工具并注册到 ToolExecutor。

    示例配置:
        servers = [{
            "name": "filesystem",
            "command": "npx",
            "args": ["-y", "@anthropic/mcp-server-filesystem", "/tmp"],
            "transport": "stdio",
        }]
    """

    def __init__(self, server_configs: Optional[List[Dict[str, Any]]] = None):
        self._servers = server_configs or []
        self._connected = False

    # ====== 生命周期（阶段 8 实现） ======

    async def connect(self) -> bool:
        """连接所有 MCP servers（启动子进程/建立 HTTP 连接）。

        TODO(阶段8): 遍历 self._servers，启动对应 transport 的连接。
        """
        logger.warning("MCPClient.connect() 尚未实现（阶段 8）")
        return False

    async def disconnect(self) -> None:
        """断开所有连接，清理子进程。

        TODO(阶段8): 发送 shutdown 通知 → 等待退出 → 强制 kill。
        """
        pass

    # ====== 工具发现（阶段 8 实现） ======

    def get_tools(self) -> List:
        """获取所有 MCP server 暴露的工具（BaseTool 列表）。

        TODO(阶段8): 调用 tools/list → 解析 JSON Schema → 返回 MCPToolAdapter 列表。
        """
        return []

    async def refresh_tools(self) -> int:
        """刷新工具列表（强制重新发现）。

        TODO(阶段8): 清空 Schema 缓存 → 重新调用 tools/list。
        """
        return 0

    # ====== 状态 ======

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def server_names(self) -> List[str]:
        return [s.get("name", "unnamed") for s in self._servers]
