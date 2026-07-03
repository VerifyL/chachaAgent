"""
tests/unit/test_mcp_client.py
单元测试：capabilities/mcp_client.py MCPClient 骨架
"""

import pytest

from capabilities.mcp_client import MCPClient


def test_init_empty():
    client = MCPClient()
    assert client._server_configs == {}
    assert client._connected is False


def test_init_with_servers():
    client = MCPClient({"filesystem": {}, "github": {}})
    assert list(client._server_configs.keys()) == ["filesystem", "github"]


@pytest.mark.asyncio
async def test_connect_returns_true_on_empty():
    """空 configs 时 connect() 返回 True（没有配置 MCP server，跳过）"""
    client = MCPClient()
    assert await client.connect() is True


@pytest.mark.asyncio
async def test_get_tools_empty():
    client = MCPClient()
    assert await client.get_tools() == []


@pytest.mark.asyncio
async def test_disconnect_noop():
    client = MCPClient()
    await client.disconnect()  # 不应抛异常
