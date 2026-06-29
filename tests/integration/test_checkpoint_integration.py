"""
tests/integration/test_checkpoint_integration.py
集成测试：中断后恢复继续任务
"""

import tempfile
from pathlib import Path

import pytest

from core.checkpoint_manager import CheckpointManager


def test_save_and_restore_resume_task():
    """模拟：对话中断 → 恢复 → 继续"""
    d = Path(tempfile.mkdtemp())
    mgr = CheckpointManager(base_dir=d)

    # 1. 用户对话若干轮
    messages = [
        {"role": "system", "content": "你是一个AI助手"},
        {"role": "user", "content": "帮我读 main.py"},
        {"role": "assistant", "content": "正在读取"},
        {"role": "user", "content": "再读 test.py"},
        {"role": "assistant", "content": "文件内容是 print('hello')"},
    ]
    sid = "session-abc"

    # 2. 保存检查点
    mgr.save(messages, session_id=sid, description="第5轮结束后")

    # 3. 模拟进程崩溃 → 从检查点恢复
    restored = mgr.restore(sid)
    assert restored is not None
    assert len(restored) == 5

    # 4. 恢复后继续对话（追加新消息后再次保存）
    restored.append({"role": "user", "content": "继续"})
    restored.append({"role": "assistant", "content": "好的"})
    mgr.save(restored, session_id=sid, description="恢复后继续")

    # 5. 验证最新保存有 7 条消息
    latest = mgr.restore(sid)
    assert len(latest) == 7


def test_restore_latest_after_multiple_saves():
    """多次保存 → 恢复最新"""
    d = Path(tempfile.mkdtemp())
    mgr = CheckpointManager(base_dir=d)

    sid = "session-multi"
    messages = [{"role": "system", "content": "base"}]

    # 保存 3 个版本
    for i in range(3):
        messages.append({"role": "user", "content": f"msg-{i}"})
        messages.append({"role": "assistant", "content": f"reply-{i}"})
        mgr.save(messages, session_id=sid, description=f"版本{i}")

    # 恢复最新 → 应有 7 条消息
    restored = mgr.restore(sid)
    assert restored is not None
    assert len(restored) == 7  # 1 system + 3*(user+assistant)
