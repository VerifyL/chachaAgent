"""
tests/integration/test_checkpoint_integration.py
集成测试：中断后恢复继续任务
"""

import tempfile
from pathlib import Path

import pytest

from core.checkpoint_manager import CheckpointManager
from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent, ObservationEvent,
)


def test_save_and_restore_resume_task():
    """模拟：对话中断 → 恢复 → 继续"""
    d = Path(tempfile.mkdtemp())
    mgr = CheckpointManager(base_dir=d)

    # 1. 用户对话 3 轮
    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)
    state.add_event(MessageEvent(source="user", role="user", content="帮我读 main.py"))
    state.add_event(MessageEvent(source="agent", role="assistant", content="正在读取"))
    state.add_event(ObservationEvent(
        source="tool", tool_use_id="c1",
        content="print('hello')", status="success",
    ))
    state.add_event(MessageEvent(source="agent", role="assistant", content="文件内容是 print('hello')"))

    sid = state.metadata.session_id

    # 2. 保存检查点
    cp = mgr.save(state, "第3轮结束后")
    assert cp.event_index == 4

    # 3. 模拟进程崩溃 → 从检查点恢复
    restored = mgr.restore(sid, cp.checkpoint_id)
    assert restored is not None
    assert len(restored.events) == 4

    # 4. 恢复后继续对话
    restored.add_event(MessageEvent(source="user", role="user", content="再读 test.py"))
    assert len(restored.events) == 5

    # 5. 再次保存
    cp2 = mgr.save(restored, "恢复后继续")
    assert cp2.event_index == 5

    # 6. 列出检查点（应有 2 个）
    items = mgr.list(sid)
    assert len(items) == 2


def test_restore_latest_after_multiple_saves():
    """多次保存 → 恢复最新"""
    d = Path(tempfile.mkdtemp())
    mgr = CheckpointManager(base_dir=d)

    meta = SessionMetadata(project_id="p1")
    state = ConversationState(metadata=meta)

    # 保存 3 个版本
    for i in range(3):
        state.add_event(MessageEvent(source="user", role="user", content=f"msg-{i}"))
        mgr.save(state, f"版本{i}")

    # 恢复最新 → 应有 3 条消息
    restored = mgr.restore(state.metadata.session_id)
    assert restored is not None
    assert len(restored.events) == 3
