"""
tests/integration/test_telemetry_integration.py
集成测试：在编排流程中验证指标变更 —— LLM 调用→工具执行→钩子→会话全链路指标
"""

import json
import tempfile
from pathlib import Path

from core.telemetry import Telemetry
from core.models.config import TelemetryConfig


def test_full_orchestration_metrics_flow():
    """模拟一次完整的编排流程，验证所有指标正确变更"""
    d = Path(tempfile.mkdtemp())
    cfg = TelemetryConfig(
        log_level="INFO",
        debug_log_path=str(d / "debug.jsonl"),
        audit_log_path=str(d / "audit.jsonl"),
        enable_audit=True,
    )
    t = Telemetry(cfg)
    t.start()

    # ---- 1. 会话开始 ----
    t.logger.info("session started", session_id="s1", project_id="p1")

    # ---- 2. LLM 调用 ----
    root = t.tracer.start_span("orchestration")
    llm_span = t.tracer.start_span("llm_call", parent=root, tags={"model": "gpt-4"})
    t.agent.record_llm_call("gpt-4", input_tokens=1000, output_tokens=500,
                            latency_ms=2000, success=True)
    llm_span.finish()

    # ---- 3. 工具调用 ----
    tool_span = t.tracer.start_span("tool_exec", parent=root, tags={"tool": "read_file"})
    t.agent.record_tool_call("read_file", duration_ms=150, success=True, output_lines=100)
    tool_span.finish()

    # ---- 4. 钩子执行 ----
    hook_span = t.tracer.start_span("hook", parent=root, tags={"hook": "security"})
    t.agent.record_hook("security", duration_ms=5, action="continue")
    hook_span.finish()

    root.finish()

    # ---- 5. 成本记录 ----
    t.agent.record_cost("gpt-4", 0.015)
    t.agent.record_cost("gpt-4", 0.005)

    # ---- 6. 上下文记录 ----
    t.agent.record_context(total_tokens=50000, utilization=0.75, compression_triggered=True)

    # ---- 7. 会话结束 ----
    t.agent.record_session("s1", total_tokens=1500, total_cost=0.02, duration_ms=2155)

    # ---- 验证指标 ----
    m = t.metrics

    # LLM
    assert m.counters['chacha_llm_calls_total{model="gpt-4",status="success"}'] == 1
    assert m.counters['chacha_llm_input_tokens_total{model="gpt-4"}'] == 1000
    assert m.counters['chacha_llm_output_tokens_total{model="gpt-4"}'] == 500
    assert len(m.histograms['chacha_llm_latency_ms{model="gpt-4",status="success"}']) == 1

    # 工具
    assert m.counters['chacha_tool_calls_total{status="success",tool="read_file"}'] == 1
    hist = m.histograms['chacha_tool_duration_ms{status="success",tool="read_file"}']
    assert len(hist) == 1

    # 钩子
    assert m.counters['chacha_hook_calls_total{action="continue",hook="security"}'] == 1

    # 会话
    assert m.counters["chacha_sessions_total"] == 1

    # 成本
    assert m.gauges["chacha_cost_cumulative_usd"] == 0.02

    # 上下文
    assert m.gauges["chacha_context_tokens"] == 50000
    assert m.gauges["chacha_context_utilization"] == 0.75
    assert m.counters["chacha_context_compressions_total"] == 1

    # ---- 验证日志文件 ----
    t.stop()
    debug_lines = Path(cfg.debug_log_path).read_text().strip().split("\n")
    assert len(debug_lines) >= 2
    msgs = [json.loads(l)["msg"] for l in debug_lines]
    assert "session started" in msgs
    assert "Telemetry 已停止" in msgs

    # ---- 验证 Span 全链路 trace_id 一致 ----
    assert llm_span.trace_id == tool_span.trace_id == hook_span.trace_id == root.trace_id
    assert root.duration_ms >= 0
    assert llm_span.duration_ms >= 0
    assert tool_span.duration_ms >= 0

    # ---- 验证 Prometheus 导出格式合规 ----
    out = t.prometheus_export()
    # counter 格式: name{labels} value
    assert 'chacha_llm_calls_total{model="gpt-4",status="success"} 1' in out
    # gauge 格式: name value
    assert "chacha_cost_cumulative_usd 0.02" in out
    # histogram 格式: name_count / name_sum
    assert 'chacha_llm_latency_ms_count{model="gpt-4",status="success"}' in out
    assert 'chacha_llm_latency_ms_sum{model="gpt-4",status="success"}' in out
