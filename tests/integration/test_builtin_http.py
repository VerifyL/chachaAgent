"""
tests/integration/test_builtin_http.py
集成测试：HttpTool 真实 HTTP 请求（依赖外网，可选运行）
"""

import pytest

from capabilities.builtins.http_tool import HttpTool


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_get_real():
    tool = HttpTool()
    result = await tool.execute(method="GET", url="https://httpbin.org/get?test=1", timeout=15)
    assert "200" in result or "HTTP 200" in result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_404_real():
    tool = HttpTool()
    result = await tool.execute(method="GET", url="https://httpbin.org/status/404", timeout=15)
    assert "404" in result
