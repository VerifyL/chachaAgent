"""
tests/integration/test_builtin_tools.py
集成测试：内置工具多工具联调
"""

import tempfile
from pathlib import Path

import pytest

from capabilities.builtins.memory_tool import LoadMemoryTool, RememberTool
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
from capabilities.builtins.code_patcher import EditFileTool


@pytest.fixture
def project_root():
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("def hello():\n    return 'hi'\n\ndef world():\n    return 'earth'\n")
    (d / "config.json").write_text('{"version": "1.0"}')
    return d


# ====== 链式操作 ======

@pytest.mark.asyncio
async def test_read_then_grep(project_root):
    """读取文件 → grep 验证 → 编辑 → 再读确认"""
    reader = ReadFileTool(root=project_root)
    grepper = GrepTool(root=project_root)
    editor = EditFileTool(root=project_root)

    # 1. 读取
    content = await reader.execute(path="main.py")
    assert "hello" in content

    # 2. grep
    result = await grepper.execute(pattern="def hello")
    assert "hello" in result

    # 3. 编辑
    r = await editor.execute(path="main.py", old_string="return 'hi'", new_string="return 'hello world'")
    assert "已编辑" in r

    # 4. 确认
    content = await reader.execute(path="main.py")
    assert "hello world" in content


# ====== 记忆链 ======

@pytest.mark.asyncio
async def test_remember_then_load():
    """写入记忆 → 搜索确认"""
    r = RememberTool()
    await r.execute(content="集成测试: 偏好 pytest 框架")

    l = LoadMemoryTool()
    result = await l.execute(query="pytest")
    assert "pytest" in result.lower()


# ====== 大文件读取 ======

@pytest.mark.asyncio
async def test_read_large_file_line_range(project_root):
    (project_root / "large.py").write_text("\n".join([f"line {i}" for i in range(5000)]))

    reader = ReadFileTool(root=project_root)
    result = await reader.execute(path="large.py", start_line=100, end_line=110)
    assert "line 99" in result or "line 100" in result or "line 109" in result or "line 110" in result


# ====== 空操作 ======

@pytest.mark.asyncio
async def test_load_memory_empty():
    l = LoadMemoryTool()
    result = await l.execute(query="")
    assert isinstance(result, str)
    assert len(result) > 0
