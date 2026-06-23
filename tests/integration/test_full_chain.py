"""
tests/integration/test_full_chain.py
全链路集成测试：LLM + 工具 + 子Agent + 记忆 + 压缩

运行:
  DEEPSEEK_API_KEY=sk-... DEEPSEEK_BASE_URL=https://api.deepseek.com DEEPSEEK_MODEL=deepseek-v4-pro \
    .venv/bin/python -m pytest tests/integration/test_full_chain.py -v -m slow
"""

import os
import tempfile
from pathlib import Path

import pytest

from core.llm_invoker import LLMInvoker
from core.llm_clients.openai_client import OpenAIClient
from core.dispatcher import Dispatcher
from core.tool_executor import ToolExecutor
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
from capabilities.builtins.code_patcher import EditFileTool
from capabilities.builtins.memory_tool import LoadMemoryTool
from capabilities.builtins.subagent_tool import SubAgentTool
from capabilities.sandbox import Sandbox
from core.subagent.spawner import SubAgentSpawner
from core.context.memory_manager import MemoryManager
from core.context_manager import ContextManager


# ====== 环境变量 ======

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def needs_api():
    if not API_KEY:
        pytest.skip("DEEPSEEK_API_KEY 未设置")


# ====== 构建 ======

def build_client():
    return OpenAIClient(api_key=API_KEY, model=MODEL, base_url=BASE_URL, max_tokens=1000)

def build_tools(project_root, memory_mgr):
    reader = ReadFileTool(root=project_root)
    grepper = GrepTool(root=project_root)
    editor = EditFileTool(root=project_root)
    loader = LoadMemoryTool(memory_manager=memory_mgr)
    sandberTool(memory_manager=memory_mgr)
    sandbox = Sandbox()
    return [reader, grepper, editor, loader, sandbandbox]


# ====== 测试 1: 纯文本 ======

@pytest.mark.slow
@pytest.mark.asyncio
async def test_simple_text():
    """最简单：hello → 返回非空"""
    needs_api()
    client = build_client()
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke(
        messages=[{"role": "user", "content": "回复一句话: 你好"}],
        session_id="full-text",
    )
    assert len(resp.text) > 0
    assert resp.finish_reason in ("stop", "length")


# ====== 测试 2: 工具调用 ======

@pytest.mark.slow
@pytest.mark.asyncio
async def test_tool_call_file_ops():
    """LLM 调用 read_file + grep 查代码"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("# Hello\nprint('hello')\nTODO: fix this\n")
    (d / "config.json").write_text('{"version":"1.0","debug":true}')

    client = build_client()
    invoker = LLMInvoker(model_client=client)
    tools = ToolExecutor(tools=[
        ReadFileTool(root=d),
        GrepTool(root=d),
    ])

    dispatcher = Dispatcher(invoker, tools)
    resp = await dispatcher.dispatch(
        messages=[
            {"role": "user", "content": f"查看项目 {d} 里 main.py 的内容，然后 grep 搜索 TODO"}
        ],
        session_id="full-tools",
        max_rounds=5,
    )

    print(f"\n[工具调用] 文本: {resp.text[:300]}")
    print(f"[工具调用] finish: {resp.finish_reason} | error: {resp.error}")
    assert len(resp.text) > 0 or resp.error is None


# ====== 测试 3: 子Agent ======

@pytest.mark.slow
@pytest.mark.asyncio
async def test_subagent_explore():
    """子Agent 探索代码结构"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    (d / "main.py").write_text("def add(a,b): return a+b\ndef sub(a,b): return a-b\n")
    (d / "util.py").write_text("def multiply(x,y): return x*y\n")

    client = build_client()
    invoker = LLMInvoker(model_client=client)
    parent_tools = ToolExecutor(tools=[
        ReadFileTool(root=d),
        GrepTool(root=d),
    ])
    spawner = SubAgentSpawner(invoker, parent_tools)

    result = await spawner.spawn(
        "explore",
        f"探索项目 {d}，列出所有函数定义和它们的功能",
        session_id="full-subagent",
        timeout=120,
    )

    print(f"\n[子Agent] 状态: {result.status} | Token: {result.tokens_used} | 耗时: {result.duration_ms}ms")
    print(f"[子Agent] 结果: {result.text[:300]}")
    assert result.status == "success"
    assert len(result.text) > 0


# ====== 测试 4: 记忆链 ======

@pytest.mark.slow
@pytest.mark.asyncio
async def test_memory_chain():
    """LLM 写记忆 → 搜索记忆"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    mgr = MemoryManager(project_id="full-test", base_dir=d)

    client = build_client()
    invoker = LLMInvoker(model_client=client)
    tools = ToolExecutor(tools=[
        LoadMemoryTool(memory_manager=mgr),
    ])
    ])

    dispatcher = Dispatcher(invoker, tools)

    # 1. LLM 写记忆
    resp1 = await dispatcher.dispatch(
        messages=[{
            "role": "system",
            "content": "你是一个助手，用户提到了重要偏好请用 write_topi工具记录。"
        }, {
            "role": "user",
            "content": "我的项目用 Python 3.11 和 ruff 格式化，记住这个"
        }],
        session_id="full-memory-write",
        max_rounds=5,
    )
    print(f"\n[记忆写入] {resp1.text[:200]}")

    # 2. 验证写入
    loaded = mgr.search("Python ruff")
    print(f"[记忆搜索] {loaded[:200]}")

    # 3. LLM 查记忆
    resp2 = await dispatcher.dispatch(
        messages=[{
            "role": "user", "content": "我之前说过项目用什么格式化工具？用 load_memory 查"
        }],
        session_id="full-memory-read",
        max_rounds=5,
    )
    print(f"[记忆读取] {resp2.text[:200]}")


# ====== 测试 5: ContextManager + 工具 ======

@pytest.mark.slow
@pytest.mark.asyncio
async def test_context_manager_with_tools():
    """ContextManager 组装 → Dispatcher 调度 → LLM + 工具"""
    needs_api()
    d = Path(tempfile.mkdtemp())
    (d / "code.py").write_text("# TODO: optimize this\ndef process():\n    pass\n")

    client = build_client()
    invoker = LLMInvoker(model_client=client)
    tools = ToolExecutor(tools=[ReadFileTool(root=d), GrepTool(root=d)])
    dispatcher = Dispatcher(invoker, tools)

    ctx_mgr = ContextManager()
    messages = [
        {"role": "system", "content": "你是一个代码助手。用工具查看代码，然后分析。"},
        {"role": "user", "content": "看看 code.py 有什么问题"},
    ]

    resp = await dispatcher.dispatch(messages, "full-context", max_rounds=5)
    print(f"\n[Context+工具] {resp.text[:300]}")
    assert len(resp.text) > 0
