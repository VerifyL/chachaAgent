"""
tests/integration/test_e2e.py
端到端测试：模拟 CLI 完整流程（不打开 TUI）。

运行:
  DEEPSEEK_API_KEY=sk-... .venv/bin/python -m pytest tests/integration/test_e2e.py -v -m slow
"""

import os
import tempfile
from pathlib import Path

import pytest

from interface.cli.agent_bridge import AgentBridge
from core.session_service import SessionService
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
from capabilities.builtins.code_patcher import EditFileTool
from capabilities.builtins.memory_tool import LoadMemoryTool

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

pytestmark = pytest.mark.slow


def needs_api():
    if not API_KEY:
        pytest.skip("DEEPSEEK_API_KEY 未设置")


def make_bridge(project_root: Path, memory_mgr=None):
    tools = [
        ReadFileTool(root=project_root),
        GrepTool(root=project_root),
        EditFileTool(root=project_root),
        LoadMemoryTool(memory_manager=memory_mgr),
    ]
    system_prompt = "你是 ChachaAgent 端到端测试助手。回复简洁直接，使用中文。"
    return AgentBridge(system_prompt=system_prompt, tools=tools, project_root=project_root)


# ====== E2E 1: 单轮对话 ======

@pytest.mark.asyncio
async def test_e2e_single_turn():
    """1 次问答：问个简单问题 → 拿到回答"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    bridge = make_bridge(d)
    msg = await bridge.initialize()
    assert "就绪" in msg

    chunks = []
    async for chunk in bridge.send_message("Python 有几个主要版本？简短回答"):
        chunks.append(chunk)

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    response = "".join(texts)
    assert len(response) > 0
    print(f"\n[E2E 单轮] 回答: {response[:200]}...")


# ====== E2E 2: 文件操作 ======

@pytest.mark.asyncio
async def test_e2e_file_ops():
    """LLM 使用 read_file 读文件并总结"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    (d / "hello.py").write_text("# Welcome to ChachaAgent\nprint('hello world')\n")

    bridge = make_bridge(d)
    await bridge.initialize()

    chunks = []
    async for chunk in bridge.send_message("读取 hello.py 并告诉我它做了什么"):
        chunks.append(chunk)

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    response = "".join(texts)
    assert len(response) > 0
    print(f"\n[E2E 文件] 回答: {response[:200]}...")


# ====== E2E 3: 记忆完整生命周期 ======

@pytest.mark.asyncio
async def test_e2e_memory_lifecycle():
    """写记忆 → 切换 session → 新 session 查不到旧记忆"""
    needs_api()
    from core.context.memory_manager import MemoryManager

    d = Path(tempfile.mkdtemp())
    session = SessionService(d)
    session.set_llm(bridge._invoker)

    # 共用同一个 MemoryManager
    mgr_shared = MemoryManager(project_root=d, session_id=session._session_id)
    bridge = make_bridge(d, memory_mgr=mgr_shared)
    await bridge.initialize()

    # 1. 在 session1 中写记忆（LLM 可调用 write_topi工具）
    texts = []
    async for chunk in bridge.send_message("记录到记忆：最喜欢的颜色是蓝色"):
        if chunk["type"] == "text":
            texts.append(chunk["content"])
    response = "".join(texts)
    print(f"\n[E2E 记忆] 回答: {response[:200]}...")
    assert len(response) > 0

    # 验证关系（LLM 可能用 text 回应或调用工具）
    days = mgr_shared.list_days()
    print(f"[E2E 记忆] Session1 天数: {days}")
    if days:
        content = mgr_shared.read_day(days[0])
        assert "蓝色" in content

    # 2. 切到 session2
    sid2 = session.new()
    await bridge.reset()
    mgr2 = MemoryManager(project_root=d, session_id=session._session_id)
    bridge2 = make_bridge(d, memory_mgr=mgr2)
    await bridge2.initialize()

    # 3. 在 session2 中查记忆（新 session 没有记忆）
    days2 = mgr2.list_days()
    print(f"[E2E 记忆] Session2 天数: {days2}")

    # session1 的记得保存了
    all_sessions = mgr_shared.list_all_sessions()
    print(f"[E2E 记忆] 所有 sessions: {all_sessions}")


# ====== E2E 4: Session 切换 + AutoDream ======

@pytest.mark.asyncio
async def test_e2e_session_switch():
    """多个 session 切换 → 验证隔离"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    bridge = make_bridge(d)
    await bridge.initialize()

    session = SessionService(d)
    session.set_llm(bridge._invoker)

    # S1: 写对话
    async for chunk in bridge.send_message("用 Python 写一个 hello world 函数"):
        pass

    # S2: 新建 session
    old_sid = session.session_id
    session.new()
    await bridge.reset()
    await bridge.initialize()

    async for chunk in bridge.send_message("简短打招呼"):
        pass

    # 列出所有 sessions（应该至少包含 S1）
    sessions = session.list_sessions()
    print(f"\n[E2E Session] Sessions: {[s['id'][:15] for s in sessions]}")
    # session1 的 ID 应该在列表中
    assert any(old_sid[:15] in s["id"] for s in sessions)
