"""
tests/unit/test_sandbox.py
单元测试：capabilities/sandbox.py Sandbox
"""

import pytest

from capabilities.sandbox import Sandbox


@pytest.fixture
def sandbox():
    return Sandbox()


# ====== 1. 基本执行 ======

@pytest.mark.asyncio
async def test_execute_echo(sandbox):
    result = await sandbox.execute(command="echo hello")
    assert "hello" in result


@pytest.mark.asyncio
async def test_execute_ls(sandbox):
    result = await sandbox.execute(command="ls -la")
    assert len(result) > 0


# ====== 2. 超时 ======

@pytest.mark.asyncio
async def test_execute_timeout(sandbox):
    result = await sandbox.execute(command="sleep 3", timeout=0.1)
    assert "超时" in result


# ====== 3. 失败命令 ======

@pytest.mark.asyncio
async def test_execute_nonexistent_command(sandbox):
    result = await sandbox.execute(command="nonexistent_command_xyz 2>&1")
    assert len(result) > 0  # 有错误输出


# ====== 4. Schema ======

def test_function_schema(sandbox):
    schema = sandbox.to_function_schema()
    assert schema["function"]["name"] == "bash"
    assert "command" in schema["function"]["parameters"]["required"]


# ====== 5. 风险级别 ======

def test_high_risk():
    assert Sandbox.risk == "high"
    assert Sandbox.requires_approval is True


# ====== 6. ANSI 清洗 ======

def test_clean_ansi_colors():
    result = Sandbox._clean_ansi("\x1b[31m红色\x1b[0m 正常")
    assert "红色" in result
    assert "\x1b" not in result


def test_clean_ansi_cursor():
    result = Sandbox._clean_ansi("\x1b[2J\x1b[Hhello")
    assert result == "hello"


def test_clean_ansi_no_change():
    assert Sandbox._clean_ansi("plain text") == "plain text"


# ====== 7. 输出截断 ======

@pytest.mark.asyncio
async def test_output_truncation(sandbox):
    # 生成超长输出
    long_cmd = f"python3 -c \"print('x' * 200000)\""
    result = await sandbox.execute(command=long_cmd)
    assert len(result) <= 100_020  # MAX_OUTPUT_CHARS + 截断标记


# ====== 8. 命令白名单（PolicyEngine 集成） ======

def test_risk_level_for_policy():
    """沙箱的高风险级别供 PolicyEngine 审批决策"""
    assert Sandbox.risk == "high"
    assert Sandbox.requires_approval is True
    meta = Sandbox.to_context_metadata(Sandbox)
    assert meta["risk"] == "high"
    assert meta["requires_approval"] is True
