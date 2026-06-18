"""
tests/unit/test_session_manager.py
单元测试：interface/cli/session_manager.py SessionManager (v2.0)

覆盖：
  - 会话生命周期（new / save / add_round）
  - 审计报告（audit_report / trace_last / status_report）
  - should_dream 触发条件（10 次 / 24h）
  - _save_round_memory 记忆保存
  - cleanup_tool_cache 清理
"""

import tempfile
import time
from pathlib import Path

import pytest

from interface.cli.session_manager import SessionManager


@pytest.fixture
def project_root():
    return Path(tempfile.mkdtemp()) / "test-project"


@pytest.fixture
def sm(project_root):
    project_root.mkdir(parents=True, exist_ok=True)
    return SessionManager(project_root=project_root)


# ====== 会话 ======

def test_new_session_generates_id(sm):
    sid = sm.new()
    assert len(sid) > 0
    assert sm.total_tokens == 0
    assert sm.rounds == 0


def test_current_id_format(sm):
    cid = sm.current_id
    # 格式: YYYYMMDD-HHMMSS
    parts = cid.split("-")
    assert len(parts) == 2
    assert len(parts[0]) == 8


def test_add_round_increments(sm):
    sm.add_round(tokens=100, duration_ms=500, user_input="hello", assistant_text="world")
    assert sm.total_tokens == 100
    assert sm.rounds == 1
    assert len(sm._history) == 1
    assert sm._history[0]["round"] == 1


def test_add_round_multiple(sm):
    for i in range(5):
        sm.add_round(tokens=50, duration_ms=100)
    assert sm.rounds == 5
    assert sm.total_tokens == 250


def test_save_returns_session_id(sm):
    sid = sm.save()
    assert sid == sm._session_id


# ====== 审计 ======

def test_audit_report_basic(sm):
    sm.add_round(tokens=200, duration_ms=300)
    report = sm.audit_report()
    assert "会话:" in report
    assert "200" in report
    assert "1" in report


def test_trace_last_empty(sm):
    assert sm.trace_last() == "暂无追踪记录"


def test_trace_last_with_history(sm):
    sm.add_round(tokens=150, duration_ms=250)
    trace = sm.trace_last()
    assert "150" in trace
    assert "250ms" in trace


def test_status_report(sm):
    sm.add_round(tokens=1000, duration_ms=100)
    report = sm.status_report()
    assert "会话:" in report
    assert "1000" in report
    assert "Dream计数:" in report


# ====== should_dream ======

def test_should_dream_first_time_false(sm):
    """首次：不足 10 次 → False"""
    assert sm.should_dream() is False


def test_should_dream_after_10_hints(sm):
    """10 次提示 → True"""
    for _ in range(10):
        sm.record_dream_hint()
    assert sm.should_dream() is True


def test_should_dream_after_5_hints_false(sm):
    """5 次 → False"""
    for _ in range(5):
        sm.record_dream_hint()
    assert sm.should_dream() is False


def test_should_dream_after_24h(sm):
    """超过 24h 未运行 → True"""
    sm.mark_dream_run()  # 先标记一次运行
    sm._last_dream_at = 0  # 模拟很久之前
    assert sm.should_dream() is True


def test_mark_dream_run_resets_counter(sm):
    """mark_dream_run 重置计数"""
    for _ in range(10):
        sm.record_dream_hint()
    sm.mark_dream_run()
    assert sm._dream_hints == 0
    assert sm.should_dream() is False


# ====== add_round 错误跟踪 ======

def test_add_round_with_errors(sm):
    sm.add_round(tokens=50, duration_ms=100, errors=["timeout"])
    assert sm._history[0]["errors"] == ["timeout"]
    report = sm.audit_report()
    assert "1/1" in report  # 错误轮次


# ====== 压缩 ======

@pytest.mark.asyncio
async def test_compact_no_bridge(sm):
    result = await sm.compact()
    assert "失败" in result


# ====== 边际情况 ======

def test_multiple_new_session_cycles(sm):
    for _ in range(3):
        sid = sm.new()
        assert len(sid) > 0
        sm.add_round(tokens=10, duration_ms=10)
        sm.save()
    assert sm.rounds == 1  # 每次 new 重置
