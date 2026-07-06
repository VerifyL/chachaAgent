"""
tests/unit/test_usage_tracker.py
单元测试：core/model/usage_tracker.py UsageTracker
"""

from core.llm_clients.usage_tracker import UsageTracker


def test_record_accumulates():
    t = UsageTracker()
    t.record("gpt-4", 1000, 500, 0.003, 0.015)
    t.record("gpt-4", 2000, 1000, 0.003, 0.015)

    assert t.total_input == 3000
    assert t.total_output == 1500
    assert t.call_count == 2
    assert t.total_cost > 0


def test_cost_calculation():
    t = UsageTracker()
    # 1000 input × 0.003 / 1000 = 0.003
    # 500 output × 0.015 / 1000 = 0.0075
    # total = 0.0105
    t.record("gpt-4", 1000, 500, 0.003, 0.015)
    assert abs(t.total_cost - 0.0105) < 0.0001


def test_multiple_models():
    t = UsageTracker()
    t.record("gpt-4", 100, 50, 0.003, 0.015)
    t.record("deepseek", 200, 100, 0.001, 0.002)

    assert t.per_model("gpt-4")["input"] == 100
    assert t.per_model("deepseek")["input"] == 200
    assert t.call_count == 2


def test_summary():
    t = UsageTracker()
    t.record("gpt-4", 100, 50)
    s = t.summary()
    assert s["total_input"] == 100
    assert s["call_count"] == 1
    assert "gpt-4" in s["per_model"]


def test_reset():
    t = UsageTracker()
    t.record("gpt-4", 100, 50)
    t.reset()
    assert t.total_input == 0
    assert t.total_cost == 0
    assert t.call_count == 0


def test_zero_tokens():
    t = UsageTracker()
    t.record("gpt-4", 0, 0)
    assert t.total_cost == 0.0
