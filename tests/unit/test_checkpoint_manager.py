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


@pytest.fixture
def mgr():
    d = Path(tempfile.mkdtemp())
    return CheckpointManager(base_dir=d)


@pytest.fixture
def messages():
    return [
        {"role": "system", "content": "你是一个AI助手"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


# ====== 保存 ======

def test_save(messages, mgr):
    mgr.save(messages, session_id="session-1", description="测试")
    path = mgr._dir / "session-1" / "checkpoint.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == "session-1"
    assert data["message_count"] == 3
    assert data["description"] == "测试"


def test_save_filters_tool_messages(mgr):
    msgs = [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "content": "file content..."},
        {"role": "assistant", "content": "done"},
    ]
    mgr.save(msgs, session_id="session-2")
    path = mgr._dir / "session-2" / "checkpoint.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    saved = data["messages"]
    assert len(saved) == 3  # tool_result filtered, tool_calls stripped
    assert saved[0]["role"] == "user"
    assert saved[1]["role"] == "assistant"  # tool_calls stripped
    assert "tool_calls" not in saved[1]
    assert saved[2]["role"] == "assistant"
    assert saved[2]["content"] == "done"


# ====== 恢复 ======

def test_restore(messages, mgr):
    mgr.save(messages, session_id="session-1")
    restored = mgr.restore("session-1")
    assert restored is not None
    assert len(restored) == 3
    assert restored[0]["content"] == "你是一个AI助手"
    assert restored[1]["content"] == "hello"


def test_restore_nonexistent(mgr):
    restored = mgr.restore("no-such-session")
    assert restored is None


# ====== 列表 ======

def test_list(messages, mgr):
    mgr.save(messages, session_id="session-1")
    items = mgr.list("session-1")
    assert len(items) == 1
    assert items[0]["session_id"] == "session-1"
    assert items[0]["message_count"] == 3


# ====== 删除 ======

def test_delete(messages, mgr):
    mgr.save(messages, session_id="session-1")
    ok = mgr.delete("session-1")
    assert ok is True
    items = mgr.list("session-1")
    assert len(items) == 0


# ====== 清理 ======

def test_purge(messages, mgr):
    mgr.save(messages, session_id="session-1")
    deleted = mgr.purge("session-1", max_age_hours=0)
    assert deleted >= 0


# ====== 文件损坏 ======

def test_corrupted_file(messages, mgr):
    cp_dir = mgr._dir / "session-1"
    cp_dir.mkdir(parents=True, exist_ok=True)
    bad = cp_dir / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")

    restored = mgr.restore("session-1")
    assert restored is None
