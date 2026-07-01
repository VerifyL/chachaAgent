"""
capabilities/mcp/__init__.py
MCP 客户端模块入口。

导出: MCPClient, MCPToolAdapter, MCPServerConfig
"""

from capabilities.mcp.adapter import MCPToolAdapter
from capabilities.mcp_client import MCPClient

__all__ = ["MCPClient", "MCPToolAdapter"]
