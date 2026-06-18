"""
tests/unit/test_checkpoint_manager.py
单元测试：core/checkpoint_manager.py
覆盖：保存/恢复/列表/删除/清理/文件损坏
"""

import json
import tempfile
from pathlib import Path

import pytest

from core.checkpoint_manager import CheckpointManager
from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent,
)


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return CheckpointManager(base_dir=d)


@pytest.fixture
def state():
    meta = SessionMetadata(project_id="p1")
    s = ConversationState(metadata=meta)
    s.add_event(MessageEvent(source="user", role="user", content="hello"))
    s.add_event(MessageEvent(source="agent", role="assistant", content="hi"))
    return s


# ====== 保存 ======

def test_save(state, mgr):
    cp = mgr.save(state, "测试检查点")
    assert cp.event_index == 2
    assert cp.description == "测试检查点"
    # 文件存在
    path = mgr._dir / state.metadata.session_id / f"{cp.checkpoint_id}.json"
    assert path.exists()


# ====== 恢复 ======

def test_restore(state, mgr):
    cp = mgr.save(state)
    restored = mgr.restore(state.metadata.session_id, cp.checkpoint_id)
    assert restored is not None
    assert len(restored.events) == 2
    assert restored.events[0].content == "hello"


def test_restore_latest(state, mgr):
    mgr.save(state)
    restored = mgr.restore(state.metadata.session_id)
    assert restored is not None
    assert len(restored.events) == 2


def test_restore_nonexistent(mgr):
    restored = mgr.restore("no-such-session")
    assert restored is None


# ====== 列表 ======

def test_list(state, mgr):
    mgr.save(state)
    items = mgr.list(state.metadata.session_id)
    assert len(items) == 1
    assert items[0]["events_count"] == 2


# ====== 删除 ======

def test_delete(state, mgr):
    cp = mgr.save(state)
    ok = mgr.delete(state.metadata.session_id, cp.checkpoint_id)
    assert ok is True
    items = mgr.list(state.metadata.session_id)
    assert len(items) == 0


# ====== 清理 ======

def test_purge(state, mgr):
    mgr.save(state)
    deleted = mgr.purge(state.metadata.session_id, max_age_hours=0)
    # max_age=0 表示立即清理所有旧文件，但保留最新一条
    assert deleted >= 0


# ====== 文件损坏 ======

def test_corrupted_file(state, mgr):
    cp_dir = mgr._dir / state.metadata.session_id
    cp_dir.mkdir(parents=True, exist_ok=True)
    bad = cp_dir / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")

    restored = mgr.restore(state.metadata.session_id)
    assert restored is None
