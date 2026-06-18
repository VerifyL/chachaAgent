"""
tests/integration/test_sandbox_integration.py
集成测试：Sandbox 执行 pytest / 多命令
"""

import pytest

from capabilities.sandbox import Sandbox


@pytest.fixture
def sandbox():
    return Sandbox()


# ====== 执行测试框架命令 ======

@pytest.mark.asyncio
async def test_execute_pytest_collect(sandbox):
    """执行 pytest --collect-only 收集测试"""
    result = await sandbox.execute(
        command="cd /Users/reyn/Study/chachaAgent && python3 -m pytest tests/unit/test_sandbox.py --collect-only -q 2>&1",
        timeout=30,
    )
    assert len(result) > 0
    assert "test_function_schema" in result or "tests collected" in result.lower()


# ====== 执行 python 脚本 ======

@pytest.mark.asyncio
async def test_execute_python_script(sandbox):
    result = await sandbox.execute(command="python3 -c 'print(1+1)'")
    assert "2" in result


# ====== 多行输出 ======

@pytest.mark.asyncio
async def test_multiline_output(sandbox):
    result = await sandbox.execute(command="echo line1 && echo line2")
    assert "line1" in result
    assert "line2" in result
