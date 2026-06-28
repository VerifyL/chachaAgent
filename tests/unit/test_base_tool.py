"""
tests/unit/test_base_tool.py
单元测试：capabilities/base.py BaseTool
"""

import pytest

from capabilities.base import BaseTool


# ====== 示例工具 ======

class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取文件内容"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
        },
        "required": ["path"],
    }
    risk = "low"

    async def execute(self, path: str) -> str:
        return f"content of {path}"


class ShellTool(BaseTool):
    name = "shell"
    description = "执行命令"
    risk = "high"
    requires_approval = True

    async def execute(self, command: str) -> str:
        return f"executed: {command}"


# ====== 1. schema 生成 ======

@pytest.fixture
def read_tool():
    return ReadFileTool()


def test_to_function_schema(read_tool):
    schema = read_tool.to_function_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "read_file"
    assert "path" in str(schema["function"]["parameters"])


# ====== 2. 执行 ======

@pytest.mark.asyncio
async def test_execute(read_tool):
    result = await read_tool.execute(path="/tmp/test.py")
    assert "/tmp/test.py" in result


# ====== 3. 元数据 ======

def test_risk_level(read_tool):
    assert read_tool.risk == "low"
    assert read_tool.requires_approval is False


def test_high_risk_tool():
    shell = ShellTool()
    assert shell.risk == "high"
    assert shell.requires_approval is True


# ====== 4. 上下文元数据 ======

def test_to_context_metadata(read_tool):
    meta = read_tool.to_context_metadata()
    assert meta["name"] == "read_file"
    assert meta["risk"] == "low"


# ====== 5. 不能直接实例化 ======

def test_base_tool_is_abstract():
    with pytest.raises(TypeError):
        BaseTool()  # type: ignore


# ====== 6. ToolExecutor 集成 ======

from core.tool_executor import ToolExecutor


@pytest.mark.asyncio
async def test_tool_executor_with_base_tool():
    """ToolExecutor 接收 BaseTool 列表 → 执行 + get_schemas"""
    tools = [ReadFileTool(), ShellTool()]
    executor = ToolExecutor(tools=tools)

    # schemas
    schemas = executor.get_schemas()
    assert len(schemas) == 2
    assert schemas[0]["function"]["name"] == "read_file"
    assert schemas[1]["function"]["name"] == "shell"

    # 执行
    result = await executor.execute("read_file", {"path": "/tmp/x.py"}, "s1")
    assert result.status == "success"
    assert "/tmp/x.py" in result.content


@pytest.mark.asyncio
async def test_tool_executor_backward_compatible():
    """向后兼容：仍支持 Dict[str, Callable]"""
    async def dummy(args): return "ok"

    executor = ToolExecutor(tools={"old_tool": dummy})
    schemas = executor.get_schemas()
    assert schemas == []  # dict 模式下无 schema

    result = await executor.execute("old_tool", {}, "s1")
    assert result.status == "success"
    assert result.content == "ok"


def test_schema_validates_required():
    """模式校验：required 字段存在"""
    schema = ReadFileTool().to_function_schema()
    required = schema["function"]["parameters"].get("required", [])
    assert "path" in required


def test_schema_validates_type():
    """模式校验：type 为 function"""
    schema = ShellTool().to_function_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "shell"
