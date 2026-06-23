"""
tests/unit/test_builtin_tools.py
单元测试：capabilities/builtins/ 全部内置工具
"""

import tempfile
from pathlib import Path

import pytest

from capabilities.builtins.memory_tool import LoadMemoryTool, WriteTopicTool, ReadTopicTool
from capabilities.builtins.chunk_streamer import ReadFileTool, ReadFilesTool, GrepTool
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
    """未注入 MemoryManager → 返回未初始化"""
    tool = LoadMemoryTool()
    result = await tool.execute(query="")
    assert "记忆系统未初始化" in result


@pytest.mark.asyncio
async def test_load_memory_search():
    """写入主题后搜索 → 返回包含关键词"""
    import tempfile
    from pathlib import Path as P
    from core.context.memory_manager import MemoryManager
    base = P(tempfile.mkdtemp())
    mgr = MemoryManager(base_dir=base, session_id="test-search")
    w = WriteTopicTool(memory_manager=mgr)
    await w.execute(topic='user-preferences', content='偏好 Python 3.11')
    l = LoadMemoryTool(memory_manager=mgr)
    result = await l.execute(query='Python')
    assert 'Python' in result


async def test_read_file(project_root):
    tool = ReadFileTool(root=project_root)
    result = await tool.execute(path="main.py")
    assert "hello" in result


@pytest.mark.asyncio
async def test_read_file_line_range(project_root):
    tool = ReadFileTool(root=project_root)
    result = await tool.execute(path="main.py", offset=1, limit=1)
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
    """编辑后验证备份文件产生（非git目录走 .bak 备份）"""
    tool = EditFileTool(root=project_root)
    await tool.execute(path="main.py", old_string="return 42", new_string="return 99")
    # 备份路径: .chacha_agent/backups/{filename}/{timestamp}.bak
    backup_dir = project_root / ".chacha_agent/backups" / "main.py"
    backups = sorted(backup_dir.glob("*.bak"))
    assert backups, f"未找到备份文件于 {backup_dir}"
    assert "return 42" in backups[0].read_text()


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


# ====== ReadFilesTool ======

@pytest.mark.asyncio
async def test_read_files(project_root):
    """批量读取多个文件：验证每个文件内容出现在结果中"""
    from capabilities.builtins.chunk_streamer import ReadFilesTool
    tool = ReadFilesTool(root=project_root)
    result = await tool.execute(paths=["main.py", "src/util.py"])
    assert "hello" in result
    assert "util.py" in result


@pytest.mark.asyncio
async def test_read_files_not_found(project_root):
    """批量读取：某个文件不存在时应返回错误提示"""
    from capabilities.builtins.chunk_streamer import ReadFilesTool
    tool = ReadFilesTool(root=project_root)
    result = await tool.execute(paths=["nope.py"])
    assert "错误" in result or "不存在" in result or "not_found" in result


# ====== WriteTopicTool ======

@pytest.mark.asyncio
async def test_write_topic_valid():
    """写入合法主题 → 返回已记录"""
    import tempfile
    from pathlib import Path as P
    from core.context.memory_manager import MemoryManager
    base = P(tempfile.mkdtemp())
    mgr = MemoryManager(base_dir=base, session_id="test-write")
    tool = WriteTopicTool(memory_manager=mgr)
    result = await tool.execute(topic="user-preferences", content="偏好 Python 3.11")
    assert "已记录" in result


@pytest.mark.asyncio
async def test_write_topic_invalid_topic():
    """写入不在白名单中的主题 → 返回无效主题提示"""
    import tempfile
    from pathlib import Path as P
    from core.context.memory_manager import MemoryManager
    base = P(tempfile.mkdtemp())
    mgr = MemoryManager(base_dir=base, session_id="test-invalid-topic")
    tool = WriteTopicTool(memory_manager=mgr)
    result = await tool.execute(topic="fantasy-topic", content="test")
    assert "无效" in result


@pytest.mark.asyncio
async def test_write_topic_no_manager():
    """未注入 MemoryManager → 返回未初始化"""
    tool = WriteTopicTool()
    result = await tool.execute(topic="user-preferences", content="test")
    assert "未初始化" in result


# ====== ReadTopicTool ======

@pytest.mark.asyncio
async def test_read_topic_roundtrip():
    """写入后读取 → 内容一致"""
    import tempfile
    from pathlib import Path as P
    from core.context.memory_manager import MemoryManager
    base = P(tempfile.mkdtemp())
    mgr = MemoryManager(base_dir=base, session_id="test-roundtrip")
    # 先写
    w = WriteTopicTool(memory_manager=mgr)
    await w.execute(topic="project-decisions", content="使用 mmap 优化文件读取")
    # 再读
    r = ReadTopicTool(memory_manager=mgr)
    result = await r.execute(topic="project-decisions")
    assert "mmap" in result


@pytest.mark.asyncio
async def test_read_topic_list_empty():
    """无参数读取 → 列出可用主题（含空列表提示）"""
    import tempfile
    from pathlib import Path as P
    from core.context.memory_manager import MemoryManager
    base = P(tempfile.mkdtemp())
    mgr = MemoryManager(base_dir=base, session_id="test-list")
    r = ReadTopicTool(memory_manager=mgr)
    result = await r.execute(topic="")
    assert "暂无主题" in result or "可用主题" in result


@pytest.mark.asyncio
async def test_read_topic_not_exist():
    """读取不存在的主题 → 返回空/不存在提示"""
    import tempfile
    from pathlib import Path as P
    from core.context.memory_manager import MemoryManager
    base = P(tempfile.mkdtemp())
    mgr = MemoryManager(base_dir=base, session_id="test-missing")
    r = ReadTopicTool(memory_manager=mgr)
    result = await r.execute(topic="errors-fixed")
    assert "暂无内容" in result or "不存在" in result


@pytest.mark.asyncio
async def test_read_topic_no_manager():
    """未注入 MemoryManager → 返回未初始化"""
    r = ReadTopicTool()
    result = await r.execute(topic="user-preferences")
    assert "未初始化" in result
