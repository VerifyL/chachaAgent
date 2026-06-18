"""
tests/unit/test_mcp_client.py
单元测试：capabilities/mcp_client.py MCPClient 骨架
"""

import pytest

from capabilities.mcp_client import MCPClient


def test_init_empty():
    client = MCPClient()
    assert client.server_names == []
    assert client.is_connected is False


def test_init_with_servers():
    client = MCPClient([{"name": "filesystem"}, {"name": "github"}])
    assert client.server_names == ["filesystem", "github"]


@pytest.mark.asyncio
async def test_connect_returns_false():
    """阶段 8 前 connect 返回 False"""
    client = MCPClient()
    assert await client.connect() is False


def test_get_tools_empty():
    client = MCPClient()
    assert client.get_tools() == []


@pytest.mark.asyncio
async def test_disconnect_noop():
    client = MCPClient()
    await client.disconnect()  # 不应抛异常
