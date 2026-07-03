"""
tests/unit/test_orchestrator.py
单元测试：core/orchestrator.py Orchestrator (v2.1)

v2.1: run() / OrchResponse 已删除，仅保留 run_stream 路径。
"""

import pytest
from core.orchestrator import Orchestrator
from core.models.stream_event import TextEvent, DoneEvent


class MockEngine:
    def __init__(self):
        self._messages = []
        self._context_window = 128000
        self._checkpoint_dir = None
        self._compress_cfg = {}

    def save_checkpoint(self):
        pass


class MockMemoryManager:
    def __init__(self):
        self.remembered = []

    def remember(self, content):
        self.remembered.append(content)
        from pathlib import Path
        return Path("/fake")



class MockDreamPipeline:
    def __init__(self):
        self.session_count = 0

    def record_session(self):
        self.session_count += 1

    def should_run(self):
        return self.session_count >= 10

    async def run(self, memory_manager):
        return "MEMORY.md", "CHACHA_MEMORY.md"


class MockDispatcher:
    """最小 dispatcher，返回几个文本 chunk 然后结束。"""
    def __init__(self):
        self.calls = []

    async def dispatch_stream(self, messages, session_id, max_rounds=200):
        self.calls.append((messages, session_id))
        yield TextEvent(content="mock reply")
        yield DoneEvent(text="mock reply", tokens=0, usage={})


# ====== run_stream 基本 ======

@pytest.mark.asyncio
async def test_run_stream_requires_engine():
    """未 set_engine 时抛出 RuntimeError。"""
    orch = Orchestrator()
    with pytest.raises(RuntimeError, match="run_stream 需要 ChatEngine"):
        async for _ in orch.run_stream("hello", session_id="s1"):
            pass


@pytest.mark.asyncio
async def test_run_stream_yields_chunks():
    """有 engine + dispatcher 时正常产出 chunk。"""
    engine = MockEngine()
    disp = MockDispatcher()
    orch = Orchestrator(dispatcher=disp)
    orch.set_engine(engine)

    chunks = []
    async for c in orch.run_stream("hello", session_id="s1"):
        chunks.append(c)

    texts = [c.content for c in chunks if isinstance(c, TextEvent)]
    assert "mock reply" in texts
    assert len(disp.calls) == 1


# ====== 会话结束清理 ======

@pytest.mark.asyncio
async def test_end_session_cleanup_via_run_stream():
    """run_stream 正常结束后记录 DreamPipeline。"""
    memory = MockMemoryManager()
    engine = MockEngine()
    disp = MockDispatcher()
    orch = Orchestrator(memory_manager=memory, dispatcher=disp)
    orch.set_engine(engine)
    # 注入 DreamPipeline mock
    class MockDream:
        def __init__(self):
            self.recorded = False
            self.should = False
        def record_session(self):
            self.recorded = True
        def should_run(self):
            return self.should
    orch._dream = MockDream()

    async for _ in orch.run_stream("hello", session_id="s-clean"):
        pass

    assert orch._dream.recorded is True


# ====== DreamPipeline ======

@pytest.mark.asyncio
async def test_dream_record_session_via_run_stream():
    """run_stream 正常结束后 DreamPipeline.record_session 被调用。"""
    dream = MockDreamPipeline()
    engine = MockEngine()
    disp = MockDispatcher()
    orch = Orchestrator(dream_pipeline=dream, dispatcher=disp)
    orch.set_engine(engine)

    async for _ in orch.run_stream("hello", session_id="s1"):
        pass

    assert dream.session_count == 1
