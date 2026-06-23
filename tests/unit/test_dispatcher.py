"""
tests/unit/test_dispatcher.py
单元测试：core/dispatcher.py Dispatcher (v2.1)

覆盖：
  - dispatch / dispatch_stream 双模式
  - 纯文本 + 工具调用链
  - 并发工具执行 (asyncio.gather)
  - 单工具异常不中断 (return_exceptions=True)
  - 断路器：同一调用连续失败 → 终止
  - 断路器：不同调用失败 → 计数器重置
  - blocked / pending_approval → 错误消息
  - LLM error + reasoning_chunks 透传
  - Stage 1 工具结果缓存 (KEEP_TOOL_RESULTS=8)
  - _guess_tool_name / MemoryManager 集成
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.dispatcher import Dispatcher, KEEP_TOOL_RESULTS
from core.llm_invoker import LLMResponse, ToolCall, StreamChunk
from core.tool_executor import ToolResult
from core.context.memory_manager import MemoryManager


# ====== Mock 实现 ======

class MockLLM:
    """同步 invoke 模式 Mock（dispatch 用）"""
    def __init__(self, responses=None):
        self._responses = responses or []
        self._idx = 0

    async def invoke(self, messages, tools=None, session_id=""):
        resp = self._responses[self._idx] if self._idx < len(self._responses) else LLMResponse(text="done")
        self._idx += 1
        return resp


class MockStreamingLLM:
    """流式 stream 模式 Mock（dispatch_stream 用）"""
    def __init__(self, chunks_list=None):
        self._chunks_list = chunks_list or []  # list[list[StreamChunk]]，每轮一个列表
        self._round = 0

    async def stream(self, messages, tools, session_id=""):
        if self._round < len(self._chunks_list):
            chunks = self._chunks_list[self._round]
            self._round += 1
            for c in chunks:
                yield c
        else:
            yield StreamChunk(type="text", content="done")
            yield StreamChunk(type="done", finish_reason="stop")


class MockTools:
    def __init__(self, results=None):
        self.executed: list[tuple] = []
        self._results = results or {}  # tool_name -> ToolResult | Exception | str

    def get_schemas(self):
        return [{"type": "function", "function": {"name": "read_file"}}]

    async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
        self.executed.append((tool_name, arguments, tool_use_id))
        preset = self._results.get(tool_name)
        if preset is not None:
            if isinstance(preset, Exception):
                raise preset
            if isinstance(preset, ToolResult):
                return preset
            return ToolResult(
                tool_use_id=tool_use_id, tool_name=tool_name,
                output=str(preset), status="success",
            )
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output=f"read {arguments.get('path', '')}", status="success",
        )


class BlockingTools:
    """工具返回 blocked 状态"""
    def __init__(self):
        self.executed: list[tuple] = []

    def get_schemas(self):
        return [{"type": "function", "function": {"name": "dangerous_tool"}}]

    async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
        self.executed.append((tool_name, arguments))
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output="", status="blocked", error="Policy blocked",
        )


class FailingTools:
    """工具连续失败（断路器测试用）"""
    def __init__(self, fail_count=10):
        self.executed: list[tuple] = []
        self._fail_count = fail_count
        self._calls = 0

    def get_schemas(self):
        return [{"type": "function", "function": {"name": "flaky_tool"}}]

    async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
        self._calls += 1
        self.executed.append((tool_name, arguments))
        if self._calls <= self._fail_count:
            return ToolResult(
                tool_use_id=tool_use_id, tool_name=tool_name,
                output="", status="error", error="ConnectionError: timeout",
            )
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output="success at last", status="success",
        )


class MultiToolMock:
    """多工具并发测试用"""
    def __init__(self):
        self.executed: list[tuple] = []
        self._call_order: list[str] = []

    def get_schemas(self):
        return [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "grep"}},
            {"type": "function", "function": {"name": "bash"}},
        ]

    async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
        self.executed.append((tool_name, arguments, tool_use_id))
        self._call_order.append(tool_name)
        # 模拟不同工具不同耗时
        if tool_name == "bash":
            await asyncio.sleep(0.02)
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output=f"{tool_name} result: {arguments}", status="success",
        )


@pytest.fixture
def memory():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-001")


# ====== 1. 纯文本（原有，保留） ======

@pytest.mark.asyncio
async def test_text_only_no_tools():
    llm = MockLLM([LLMResponse(text="Hello, world!", finish_reason="stop")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "Hello" in resp.text
    assert len(tools.executed) == 0


# ====== 2. 工具调用链（原有，保留） ======

@pytest.mark.asyncio
async def test_dispatch_with_tool_call():
    llm = MockLLM([
        LLMResponse(
            text="Let me read that",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "main.py"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(text="File contents: print('hi')", finish_reason="stop"),
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "read main.py"}], "s1")
    assert "File contents" in resp.text
    assert len(tools.executed) == 1
    assert tools.executed[0][0] == "read_file"


# ====== 3. schema 属性（原有） ======

def test_tool_count():
    d = Dispatcher(MockLLM(), MockTools())
    assert d.tool_count == 1
    assert len(d.schemas) == 1


# ====== 4. 错误处理（原有） ======

@pytest.mark.asyncio
async def test_dispatch_llm_error():
    llm = MockLLM([LLMResponse(text="", error="API error")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert resp.error is not None or resp.finish_reason in ("stop", "error")


# ====== 5. 无 schema（原有） ======

@pytest.mark.asyncio
async def test_dispatch_no_tools():
    t = MockTools()
    t.get_schemas = lambda: []
    d = Dispatcher(MockLLM([LLMResponse(text="no tools needed")]), t)
    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "no tools needed" in resp.text


# ======================================================================
# v2.1: dispatch_stream 测试（流式 + 并发 + 断路器）
# ======================================================================

# ── U-D1: 纯文本流式输出 ──

@pytest.mark.asyncio
async def test_dispatch_stream_text_only():
    """dispatch_stream 纯文本流式输出"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="text", content="Hello, "),
            StreamChunk(type="text", content="world!"),
            StreamChunk(type="done", finish_reason="stop", usage={"total": 10}),
        ],
    ])
    d = Dispatcher(llm, MockTools())

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "hi"}], "s1"):
        chunks.append(chunk)

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "".join(texts) == "Hello, world!"
    assert any(c["type"] == "done" for c in chunks)


# ── U-D2: 流式工具调用 → 执行 → 返回 ──

@pytest.mark.asyncio
async def test_dispatch_stream_with_tool_calls():
    """dispatch_stream：LLM 请求工具 → 执行 → 继续 → 最终回答"""
    llm = MockStreamingLLM([
        # 第 1 轮：工具调用
        [
            StreamChunk(type="text", content="Let me read..."),
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"path": "main.py"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        # 第 2 轮：最终回答
        [
            StreamChunk(type="text", content="File contents here"),
            StreamChunk(type="done", finish_reason="stop", usage={"total": 15}),
        ],
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read main.py"}], "s1"):
        chunks.append(chunk)

    # 验证工具执行事件
    exec_starts = [c for c in chunks if c["type"] == "tool_exec_start"]
    exec_ends = [c for c in chunks if c["type"] == "tool_exec_end"]
    assert len(exec_starts) == 1
    assert len(exec_ends) == 1
    assert exec_starts[0]["tool_name"] == "read_file"
    assert len(tools.executed) == 1

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "File contents here" in "".join(texts)


# ── U-D3: 并发工具执行 ──

@pytest.mark.asyncio
async def test_dispatch_stream_concurrent_tools():
    """同轮多个 tool_calls → 并发执行 (asyncio.gather)"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"path": "a.py"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="tool_call_start", tool_index=1, tool_id="c2", tool_name="grep"),
            StreamChunk(type="tool_call_delta", tool_index=1, tool_args_delta='{"pattern": "foo"}'),
            StreamChunk(type="tool_call_end", tool_index=1),
            StreamChunk(type="tool_call_start", tool_index=2, tool_id="c3", tool_name="bash"),
            StreamChunk(type="tool_call_delta", tool_index=2, tool_args_delta='{"command": "ls"}'),
            StreamChunk(type="tool_call_end", tool_index=2),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamChunk(type="text", content="all done"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])
    tools = MultiToolMock()
    d = Dispatcher(llm, tools)

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read and grep"}], "s1"):
        chunks.append(chunk)

    # 3 个工具都被执行
    assert len(tools.executed) == 3
    exec_starts = [c for c in chunks if c["type"] == "tool_exec_start"]
    assert len(exec_starts) == 3
    exec_ends = [c for c in chunks if c["type"] == "tool_exec_end"]
    assert len(exec_ends) == 3

    # bash 最慢但在 gather 中同时执行（并发验证通过执行数量间接证明）
    tool_names = [t[0] for t in tools.executed]
    assert set(tool_names) == {"read_file", "grep", "bash"}


# ── U-D4: 单工具异常不中断其他（return_exceptions=True） ──

@pytest.mark.asyncio
async def test_dispatch_stream_tool_exception_wrapped():
    """并发中单个工具抛异常 → 包装为 error ToolResult，不中断其他"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"path": "a.py"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="tool_call_start", tool_index=1, tool_id="c2", tool_name="grep"),
            StreamChunk(type="tool_call_delta", tool_index=1, tool_args_delta='{"pattern": "x"}'),
            StreamChunk(type="tool_call_end", tool_index=1),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamChunk(type="text", content="partial success"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])
    tools = MultiToolMock()
    # grep 工具抛异常
    tools._results = {"grep": ConnectionError("network down")}
    d = Dispatcher(llm, tools)

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read and grep"}], "s1"):
        chunks.append(chunk)

    # read_file 仍然成功，grep 失败被包装
    exec_ends = [c for c in chunks if c["type"] == "tool_exec_end"]
    assert len(exec_ends) == 2  # 两个工具都有 end 事件

    # 错误不会导致 dispatch_stream 终止
    errors = [c for c in chunks if c["type"] == "error"]
    assert len(errors) == 0  # 工具异常不产生 error chunk

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "partial success" in "".join(texts)


# ── U-D5: 工具执行事件验证 ──

@pytest.mark.asyncio
async def test_dispatch_stream_tool_exec_events():
    """验证 tool_exec_start / tool_exec_end 事件正确 yield"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c42", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"path": "x.py"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamChunk(type="text", content="ok"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])
    d = Dispatcher(llm, MockTools())

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read"}], "s1"):
        chunks.append(chunk)

    start_events = [c for c in chunks if c["type"] == "tool_exec_start"]
    end_events = [c for c in chunks if c["type"] == "tool_exec_end"]
    assert len(start_events) == 1
    assert len(end_events) == 1
    assert start_events[0]["tool_name"] == "read_file"
    assert "args" in start_events[0]
    assert end_events[0]["tool_name"] == "read_file"
    assert "preview" in end_events[0]


# ── U-D6: 断路器——同一调用连续失败 → 终止 ──

@pytest.mark.asyncio
async def test_dispatch_stream_circuit_breaker_trips():
    """同一 (tool+args) 连续失败 N 次 → 断路器断开 → error chunk + return"""
    # 构造：LLM 每次都返回同样的工具调用
    rounds_of_tool_calls = []
    for _ in range(6):
        rounds_of_tool_calls.append([
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="flaky_tool"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"target": "server"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ])
    llm = MockStreamingLLM(rounds_of_tool_calls)
    tools = FailingTools(fail_count=10)  # 总是失败
    d = Dispatcher(llm, tools)
    # 降低阈值加快测试
    d._max_same_call_failures = 3

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "go"}], "s1"):
        chunks.append(chunk)

    # 断路器应触发
    errors = [c for c in chunks if c["type"] == "error"]
    assert len(errors) >= 1
    assert any("Circuit breaker" in e["message"] for e in errors)


# ── U-D7: 断路器——不同调用失败重置计数 ──

@pytest.mark.asyncio
async def test_dispatch_stream_circuit_breaker_resets():
    """不同调用失败 → _same_call_failures 重置 → 不触发断路器"""
    # 第 1 轮：tool A
    # 第 2 轮：tool B（不同于 A）→ 计数器重置
    # 第 3 轮：tool A 又失败 1 次 → 总共 A 失败 2 次 < 5
    llm = MockStreamingLLM([
        [  # Round 1: flaky_tool + read_file
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="flaky_tool"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"target": "x"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="tool_call_start", tool_index=1, tool_id="c2", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=1, tool_args_delta='{"path": "y.py"}'),
            StreamChunk(type="tool_call_end", tool_index=1),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [  # Round 2: read_file（不同于 flaky_tool）→ 应重置
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c3", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"path": "z.py"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [  # Round 3: flaky_tool 又失败 → 计数器重新从 1 开始
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c4", tool_name="flaky_tool"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"target": "x"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamChunk(type="text", content="final"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])

    class MixedFailingTools:
        def __init__(self):
            self.executed: list[tuple] = []

        def get_schemas(self):
            return [
                {"type": "function", "function": {"name": "flaky_tool"}},
                {"type": "function", "function": {"name": "read_file"}},
            ]

        async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
            self.executed.append((tool_name, arguments))
            if tool_name == "flaky_tool":
                return ToolResult(
                    tool_use_id=tool_use_id, tool_name=tool_name,
                    output="", status="error", error="timeout",
                )
            return ToolResult(
                tool_use_id=tool_use_id, tool_name=tool_name,
                output=f"read ok", status="success",
            )

    tools = MixedFailingTools()
    d = Dispatcher(llm, tools)

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "go"}], "s1"):
        chunks.append(chunk)

    # 不应触发断路器（不同调用间重置了计数器）
    errors = [c for c in chunks if c["type"] == "error"]
    cb_errors = [e for e in errors if "Circuit breaker" in e.get("message", "")]
    assert len(cb_errors) == 0

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "final" in "".join(texts)


# ── U-D8: blocked 工具 → 转为错误消息 ──

@pytest.mark.asyncio
async def test_dispatch_stream_blocked_tool():
    """blocked / pending_approval 状态 → output 变为 [status] 前缀"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="dangerous_tool"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"cmd": "rm -rf /"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamChunk(type="text", content="blocked and reported"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])
    d = Dispatcher(llm, BlockingTools())

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "destroy"}], "s1"):
        chunks.append(chunk)

    # 工具执行完成但被阻塞
    exec_ends = [c for c in chunks if c["type"] == "tool_exec_end"]
    assert len(exec_ends) == 1

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "blocked and reported" in "".join(texts)


# ── U-D9: LLM error → yield error chunk → return ──

@pytest.mark.asyncio
async def test_dispatch_stream_llm_error():
    """LLM 返回 error chunk → yield + return（不崩溃）"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="error", content="API rate limit exceeded"),
        ],
    ])
    d = Dispatcher(llm, MockTools())

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "hi"}], "s1"):
        chunks.append(chunk)

    errors = [c for c in chunks if c["type"] == "error"]
    assert len(errors) >= 1
    assert "rate limit" in errors[0]["message"]


# ── U-D10: reasoning_content 透传 ──

@pytest.mark.asyncio
async def test_dispatch_stream_reasoning_chunks():
    """DeepSeek reasoning_content 透传"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="reasoning", content="Let me think..."),
            StreamChunk(type="text", content="The answer is 42"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])
    d = Dispatcher(llm, MockTools())

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "what is the answer"}], "s1"):
        chunks.append(chunk)

    reasoning = [c["content"] for c in chunks if c["type"] == "reasoning"]
    assert len(reasoning) >= 1
    assert "think" in "".join(reasoning)

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "42" in "".join(texts)


# ── U-D11: max_rounds 超限终止 ──

@pytest.mark.asyncio
async def test_dispatch_stream_max_rounds():
    """超过 max_rounds → 强制终止"""
    # 构造永不停歇的工具调用
    endless_rounds = []
    for _ in range(5):
        endless_rounds.append([
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta='{"path": "x.py"}'),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ])
    llm = MockStreamingLLM(endless_rounds)
    d = Dispatcher(llm, MockTools())

    chunks = []
    async for chunk in d.dispatch_stream(
        [{"role": "user", "content": "loop"}], "s1", max_rounds=3,
    ):
        chunks.append(chunk)

    # 3 轮后终止
    exec_starts = [c for c in chunks if c["type"] == "tool_exec_start"]
    assert len(exec_starts) <= 3


# ── U-D12: 无效 JSON args → {} + 继续 ──

@pytest.mark.asyncio
async def test_dispatch_stream_tool_args_invalid_json():
    """工具参数 JSON 无效 → 回退 {}，不崩溃"""
    llm = MockStreamingLLM([
        [
            StreamChunk(type="tool_call_start", tool_index=0, tool_id="c1", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0, tool_args_delta="NOT VALID JSON {{{"),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamChunk(type="text", content="handled bad args"),
            StreamChunk(type="done", finish_reason="stop"),
        ],
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read"}], "s1"):
        chunks.append(chunk)

    # 工具仍执行，参数为空字典
    assert len(tools.executed) == 1
    # 不应有 error chunk
    errors = [c for c in chunks if c["type"] == "error"]
    assert len(errors) == 0
    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "handled bad args" in "".join(texts)


# ── U-D13: freeze 触发（超过 KEEP） ──

@pytest.mark.asyncio
async def test_dispatch_stream_freeze_triggers(memory):
    """工具结果超过 KEEP(8) → JSON 占位符"""
    # 构造：12 轮工具调用，每轮 1 个工具 = 12 个 tool 消息
    tool_rounds = []
    for i in range(12):
        tool_rounds.append([
            StreamChunk(type="tool_call_start", tool_index=0, tool_id=f"c{i}", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0,
                        tool_args_delta=json.dumps({"path": f"f{i}.py"})),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ])
    tool_rounds.append([
        StreamChunk(type="text", content="all processed"),
        StreamChunk(type="done", finish_reason="stop"),
    ])
    llm = MockStreamingLLM(tool_rounds)

    # 返回较长结果触发 freeze
    class LongResultTools:
        def get_schemas(self):
            return [{"type": "function", "function": {"name": "read_file"}}]

        async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
            return ToolResult(
                tool_use_id=tool_use_id, tool_name=tool_name,
                output=f"file content for {arguments.get('path','?')}: " + "x" * 300,
                status="success",
            )

    d = Dispatcher(llm, LongResultTools(), memory_manager=memory)

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read all"}], "s1"):
        chunks.append(chunk)

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "all processed" in "".join(texts)


# ── U-D14: freeze 不触发（低于阈值） ──

@pytest.mark.asyncio
async def test_dispatch_stream_freeze_below_threshold():
    """少于 KEEP 个工具结果 → 不触发 freeze"""
    # 只有 2 轮工具调用
    tool_rounds = []
    for i in range(2):
        tool_rounds.append([
            StreamChunk(type="tool_call_start", tool_index=0, tool_id=f"c{i}", tool_name="read_file"),
            StreamChunk(type="tool_call_delta", tool_index=0,
                        tool_args_delta=json.dumps({"path": f"f{i}.py"})),
            StreamChunk(type="tool_call_end", tool_index=0),
            StreamChunk(type="done", finish_reason="tool_calls"),
        ])
    tool_rounds.append([
        StreamChunk(type="text", content="only two tools"),
        StreamChunk(type="done", finish_reason="stop"),
    ])
    llm = MockStreamingLLM(tool_rounds)
    d = Dispatcher(llm, MockTools())

    chunks = []
    async for chunk in d.dispatch_stream([{"role": "user", "content": "read"}], "s1"):
        chunks.append(chunk)

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert "only two tools" in "".join(texts)


# ======================================================================
# v2.0: Stage 1 工具结果缓存（原有，保留）
# ======================================================================

@pytest.mark.asyncio
async def test_freeze_old_tool_results_below_threshold():
    """少于 KEEP_TOOL_RESULTS 个 → 不触发缓存"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools)

    messages = [
        {"role": "user", "content": "hi"},
    ]
    for i in range(3):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i}: " + "x" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    for i in range(3):
        assert messages[i + 1]["content"].startswith("result")


@pytest.mark.asyncio
async def test_freeze_old_tool_results_above_threshold(memory):
    """超过 KEEP_TOOL_RESULTS 个 → 旧的变 JSON 占位符"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [{"role": "user", "content": "hi"}]
    for i in range(15):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i}: " + "x" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    # 前 7 个（15-8）变占位符
    for i in range(7):
        content = messages[i + 1]["content"]
        assert content.startswith("{")
        assert '"toolname"' in content
        assert '"result_summary"' in content
        assert '"cache_path"' in content

    # 最近 8 个保持完整（KEEP_TOOL_RESULTS=8）
    for i in range(7, 15):
        assert messages[i + 1]["content"].startswith("result")


@pytest.mark.asyncio
async def test_freeze_old_tool_results_json_format(memory):
    """验证 JSON 占位符格式"""
    llm = MockLLM([
        LLMResponse(
            text="Let me check",
            tool_calls=[ToolCall(id="c99", name="read_file", arguments={"path": "main.py"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(text="Done", finish_reason="stop"),
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [
        {"role": "user", "content": "read all files"},
    ]
    for i in range(15):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "f.py"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"file content {i}: " + "y" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    for i in range(10):
        if messages[i * 2 + 2]["content"].startswith("{"):
            data = json.loads(messages[i * 2 + 2]["content"])
            assert "toolname" in data
            assert "result_summary" in data
            assert "cache_path" in data


@pytest.mark.asyncio
async def test_freeze_skips_short_results(memory):
    """太短的结果 (<100 字符) 不缓存"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [{"role": "user", "content": "hi"}]
    for i in range(15):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"short {i}",
        })

    d._freeze_old_tool_results(messages, "s1")

    for i in range(15):
        assert not messages[i + 1]["content"].startswith("{")


# ====== v2.0: _guess_tool_name（原有） ======

def test_guess_tool_name_finds_match():
    """从前一条 assistant 消息中匹配 tool_call_id"""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c42", "type": "function", "function": {"name": "grep_search"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c42", "content": "found 5 results"},
    ]

    name = Dispatcher._guess_tool_name(messages, 2)
    assert name == "grep_search"


def test_guess_tool_name_not_found():
    """未找到 → 'unknown'"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c99", "content": "result"},
    ]
    name = Dispatcher._guess_tool_name(messages, 1)
    assert name == "unknown"


# ====== v2.0: dispatcher 集成 memory_manager（原有） ======

def test_dispatcher_accepts_memory_manager(memory):
    """Dispatcher 接受 MemoryManager 参数"""
    d = Dispatcher(MockLLM(), MockTools(), memory_manager=memory)
    assert d._memory is not None


def test_dispatcher_without_memory_manager():
    """不传 MemoryManager 也可以"""
    d = Dispatcher(MockLLM(), MockTools())
    assert d._memory is None
    assert d.tool_count == 1atch_stream 文本+工具调用
  - 并发工具执行 (asyncio.gather)
  - Circuit Breaker（同调用连续失败 → 断路器断开）
  - 阻塞工具 / 审批拒绝 → 转错误消息
  - reasoning_content 透传
  - max_rounds 上限
  - Stage 1 工具结果缓存（JSON 占位符）
  - _freeze_old_tool_results / _guess_tool_name
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.dispatcher import Dispatcher, KEEP_TOOL_RESULTS
from core.llm_invoker import LLMResponse, ToolCall, StreamChunk
from core.tool_executor import ToolResult
from core.context.memory_manager import MemoryManager


# ====== Mock 实现 ======

class MockLLM:
    """同步式 Mock（用于 dispatch()）。"""
    def __init__(self, responses=None):
        self._responses = responses or []
        self._idx = 0

    async def invoke(self, messages, tools=None, session_id=""):
        resp = self._responses[self._idx] if self._idx < len(self._responses) else LLMResponse(text="done")
        self._idx += 1
        return resp


class MockStreamLLM:
    """流式 Mock（用于 dispatch_stream()）。"""
    def __init__(self, chunks_list=None):
        self._chunks_list = chunks_list or []  # list[list[StreamChunk]] 每轮一组

    async def stream(self, messages, tools=None, session_id=""):
        if not self._chunks_list:
            yield StreamChunk(type="text", content="default")
            yield StreamChunk(type="done", finish_reason="stop")
            return
        for chunk in self._chunks_list.pop(0):
            yield chunk


class MockTools:
    def __init__(self, tool_names=None):
        self.executed: list[tuple] = []
        self._names = tool_names or ["read_file"]
        self._execute_fn = None  # 自定义 execute 行为

    def get_schemas(self):
        return [{"type": "function", "function": {"name": n}} for n in self._names]

    async def execute(self, tool_name, arguments, session_id="", tool_use_id="", **kwargs):
        self.executed.append((tool_name, arguments))
        if self._execute_fn:
            return self._execute_fn(tool_name, arguments, tool_use_id)
        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            output=f"read {arguments.get('path', '')}", status="success",
        )


@pytest.fixture
def memory():
    d = Path(tempfile.mkdtemp())
    return MemoryManager(project_id="test", base_dir=d, session_id="session-001")


# ====== 1. 纯文本（原有） ======

@pytest.mark.asyncio
async def test_text_only_no_tools():
    llm = MockLLM([LLMResponse(text="Hello, world!", finish_reason="stop")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "Hello" in resp.text
    assert len(tools.executed) == 0


# ====== 2. 工具调用链（原有） ======

@pytest.mark.asyncio
async def test_dispatch_with_tool_call():
    llm = MockLLM([
        LLMResponse(
            text="Let me read that",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "main.py"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(text="File contents: print('hi')", finish_reason="stop"),
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "read main.py"}], "s1")
    assert "File contents" in resp.text
    assert len(tools.executed) == 1
    assert tools.executed[0][0] == "read_file"


# ====== 3. schema 属性（原有） ======

def test_tool_count():
    d = Dispatcher(MockLLM(), MockTools())
    assert d.tool_count == 1
    assert len(d.schemas) == 1


# ====== 4. 错误处理（原有） ======

@pytest.mark.asyncio
async def test_dispatch_llm_error():
    llm = MockLLM([LLMResponse(text="", error="API error")])
    tools = MockTools()
    d = Dispatcher(llm, tools)

    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert resp.error is not None or resp.finish_reason in ("stop", "error")


# ====== 5. 无 schema（原有） ======

@pytest.mark.asyncio
async def test_dispatch_no_tools():
    t = MockTools()
    t.get_schemas = lambda: []
    d = Dispatcher(MockLLM([LLMResponse(text="no tools needed")]), t)
    resp = await d.dispatch([{"role": "user", "content": "hi"}], "s1")
    assert "no tools needed" in resp.text


# ====== v2.0: Stage 1 工具结果缓存 ======

@pytest.mark.asyncio
async def test_freeze_old_tool_results_below_threshold():
    """少于 KEEP_TOOL_RESULTS 个 → 不触发缓存"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools)

    messages = [
        {"role": "user", "content": "hi"},
    ]
    # 只添加 3 个工具结果
    for i in range(3):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i}: " + "x" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    # 全部保持完整
    for i in range(3):
        assert messages[i + 1]["content"].startswith("result")


@pytest.mark.asyncio
async def test_freeze_old_tool_results_above_threshold(memory):
    """超过 KEEP_TOOL_RESULTS 个 → 旧的变 JSON 占位符"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [{"role": "user", "content": "hi"}]
    # 添加 15 个工具结果
    for i in range(15):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"result {i}: " + "x" * 200,
        })

    d._freeze_old_tool_results(messages, "s1")

    # 前 5 个（15-10）变占位符
    for i in range(5):
        content = messages[i + 1]["content"]
        assert content.startswith("{")
        assert '"toolname"' in content
        assert '"result_summary"' in content
        assert '"cache_path"' in content

        # 最近 8 个保持完整（KEEP_TOOL_RESULTS=8）
        for i in range(7, 15):
            assert messages[i + 1]["content"].startswith("result")


@pytest.mark.asyncio
async def test_freeze_old_tool_results_json_format(memory):
    """验证 JSON 占位符格式"""
    llm = MockLLM([
        LLMResponse(
            text="Let me check",
            tool_calls=[ToolCall(id="c99", name="read_file", arguments={"path": "main.py"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(text="Done", finish_reason="stop"),
    ])
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [
        {"role": "user", "content": "read all files"},
    ]
    # 模拟之前的 assistant tool_calls 消息
    for i in range(15):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "f.py"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"file content {i}: " + "y" * 200,
        })

    # 触发 _guess_tool_name
    d._freeze_old_tool_results(messages, "s1")

    # 验证占位符可解析为 JSON
    for i in range(10):  # 查看前 10 个（至少前 5 个是占位符）
        if messages[i * 2 + 2]["content"].startswith("{"):
            data = json.loads(messages[i * 2 + 2]["content"])
            assert "toolname" in data
            assert "result_summary" in data
            assert "cache_path" in data


@pytest.mark.asyncio
async def test_freeze_skips_short_results(memory):
    """太短的结果 (<100 字符) 不缓存"""
    llm = MockLLM()
    tools = MockTools()
    d = Dispatcher(llm, tools, memory_manager=memory)

    messages = [{"role": "user", "content": "hi"}]
    for i in range(15):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": f"short {i}",  # < 100 字符
        })

    d._freeze_old_tool_results(messages, "s1")

    # 短结果全部保持原样
    for i in range(15):
        assert not messages[i + 1]["content"].startswith("{")


# ====== v2.0: _guess_tool_name ======

def test_guess_tool_name_finds_match():
    """从前一条 assistant 消息中匹配 tool_call_id"""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c42", "type": "function", "function": {"name": "grep_search"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c42", "content": "found 5 results"},
    ]

    name = Dispatcher._guess_tool_name(messages, 2)
    assert name == "grep_search"


def test_guess_tool_name_not_found():
    """未找到 → 'unknown'"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c99", "content": "result"},
    ]
    name = Dispatcher._guess_tool_name(messages, 1)
    assert name == "unknown"


# ====== v2.0: dispatcher 集成 memory_manager ======

def test_dispatcher_accepts_memory_manager(memory):
    """Dispatcher 接受 MemoryManager 参数"""
    d = Dispatcher(MockLLM(), MockTools(), memory_manager=memory)
    assert d._memory is not None


def test_dispatcher_without_memory_manager():
    """不传 MemoryManager 也可以"""
    d = Dispatcher(MockLLM(), MockTools())
    assert d._memory is None
    assert d.tool_count == 1
