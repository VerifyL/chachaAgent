"""
tests/unit/test_builtin_tools.py
单元测试：capabilities/builtins/ 全部内置工具
"""

import tempfile
from pathlib import Path

import pytest

from capabilities.builtins.memory_tool import LoadMemoryTool, RememberTool
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
from capabilities.builtins.code_patcher import EditFileTool
from capabilities.builtins.http_tool import HttpTool

TEST_DIR = Path(tempfile.mkdtemp())


# ====== Fixtures ======

@pytest.fixture
def project_root():
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("print('hello')\ndef foo():\n    return 42\n")
    (d / "src" / "util.py").parent.mkdir(parents=True, exist_ok=True)
    (d / "src" / "util.py").write_text("import os\ndef bar():\n    return 7\n")
    return d


# ====== LoadMemoryTool ======

@pytest.mark.asyncio
async def test_load_memory_list_days():
    tool = LoadMemoryTool()
    result = await tool.execute(query="")
    assert "可用记忆日期" in result or "暂无记忆" in result


@pytest.mark.asyncio
async def test_load_memory_search():
    r = RememberTool()
    await r.execute(content="偏好 Python 3.11")
    l = LoadMemoryTool()
    result = await l.execute(query="Python")
    assert "Python" in result


# ====== RememberTool ======

@pytest.mark.asyncio
async def test_remember():
    r = RememberTool()
    result = await r.execute(content="重要记忆: 测试")
    assert "已记录" in result


# ====== ReadFileTool ======

@pytest.mark.asyncio
async def test_read_file(project_root):
    tool = ReadFileTool(root=project_root)
    result = await tool.execute(path="main.py")
    assert "hello" in result


@pytest.mark.asyncio
async def test_read_file_line_range(project_root):
    tool = ReadFileTool(root=project_root)
    result = await tool.execute(path="main.py", start_line=1, end_line=1)
    assert "hello" in result
    assert "def foo" not in result


@pytest.mark.asyncio
async def test_read_file_not_found(project_root):
    tool = ReadFileTool(root=project_root)
    result = await tool.execute(path="nope.py")
    assert "不存在" in result


# ====== GrepTool ======

@pytest.mark.asyncio
async def test_grep(project_root):
    tool = GrepTool(root=project_root)
    result = await tool.execute(pattern="print")
    assert "hello" in result


@pytest.mark.asyncio
async def test_grep_no_match(project_root):
    tool = GrepTool(root=project_root)
    result = await tool.execute(pattern="XXXXNOEXIST")
    assert "未找到" in result


@pytest.mark.asyncio
async def test_grep_invalid_regex(project_root):
    tool = GrepTool(root=project_root)
    result = await tool.execute(pattern="[invalid")
    assert "无效正则" in result


# ====== EditFileTool ======

@pytest.mark.asyncio
async def test_edit_file(project_root):
    tool = EditFileTool(root=project_root)
    result = await tool.execute(
        path="main.py",
        old_string="print('hello')",
        new_string="print('world')",
    )
    assert "已编辑" in result
    content = (project_root / "main.py").read_text()
    assert "print('world')" in content


@pytest.mark.asyncio
async def test_edit_file_backup(project_root):
    tool = EditFileTool(root=project_root)
    await tool.execute(path="main.py", old_string="return 42", new_string="return 99")
    backup = project_root / ".chacha_agent/backups" / "main.py.bak"
    assert backup.exists()
    assert "return 42" in backup.read_text()


@pytest.mark.asyncio
async def test_edit_file_not_unique(project_root):
    (project_root / "main.py").write_text("hello\nhello\n")
    tool = EditFileTool(root=project_root)
    result = await tool.execute(path="main.py", old_string="hello", new_string="hi")
    assert "匹配了 2 处" in result or "不唯一" in result


@pytest.mark.asyncio
async def test_edit_file_replace_all(project_root):
    (project_root / "main.py").write_text("hello\nhello\n")
    tool = EditFileTool(root=project_root)
    result = await tool.execute(path="main.py", old_string="hello", new_string="hi", replace_all=True)
    assert "已编辑" in result


# ====== HttpTool ======

@pytest.mark.asyncio
async def test_http_invalid_url():
    tool = HttpTool()
    result = await tool.execute(method="GET", url="ftp://example.com", timeout=5)
    assert "不支持的协议" in result


@pytest.mark.asyncio
async def test_http_invalid_method():
    tool = HttpTool()
    result = await tool.execute(method="PATCH", url="https://httpbin.org/get", timeout=5)
    assert "不支持" in result


@pytest.mark.asyncio
async def test_http_connection_error():
    tool = HttpTool()
    result = await tool.execute(method="GET", url="http://127.0.0.1:19999/", timeout=1)
    assert "连接失败" in result or "[错误]" in result
