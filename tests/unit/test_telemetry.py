"""
tests/unit/test_telemetry.py
单元测试：core/telemetry.py
覆盖：结构化日志（级别过滤、JSONL 写入）、指标（counter/gauge/histogram/P50/P99）、
      领域指标（LLM/工具/钩子/会话/成本/上下文）、Span 追踪、Prometheus 导出
"""

import json
import tempfile
from pathlib import Path

import pytest

from core.telemetry import (
    Telemetry, StructuredLogger, MetricsCollector, AgentMetrics,
    LogLevel, Tracer, Span,
)
from core.models.config import TelemetryConfig


# ========== 1. 结构化日志 ==========

class TestStructuredLogger:
    @pytest.fixture
    def tmp_config(self):
        d = Path(tempfile.mkdtemp())
        return TelemetryConfig(
            log_level="DEBUG",
            debug_log_path=str(d / "debug.jsonl"),
            audit_log_path=str(d / "audit.jsonl"),
            enable_audit=True,
        )

    def test_write_debug_log(self, tmp_config):
        logger = StructuredLogger(tmp_config)
        logger.info("test message", key="value")
        lines = Path(tmp_config.debug_log_path).read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["level"] == "INFO"
        assert entry["msg"] == "test message"
        assert entry["key"] == "value"

    def test_level_filtering(self, tmp_config):
        cfg = TelemetryConfig(
            log_level="WARNING",
            debug_log_path=str(tmp_config.debug_log_path.parent / "filtered.jsonl"),
            audit_log_path=str(tmp_config.audit_log_path),
        )
        logger = StructuredLogger(cfg)
        logger.debug("should be filtered")
        logger.info("should be filtered")
        logger.warning("should appear")
        logger.error("should appear")
        lines = Path(cfg.debug_log_path).read_text().strip().split("\n")
        assert len(lines) == 2

    def test_audit_log_disabled(self, tmp_config):
        cfg = TelemetryConfig(
            log_level="INFO",
            debug_log_path=str(tmp_config.debug_log_path),
            audit_log_path=str(tmp_config.audit_log_path),
            enable_audit=False,
        )
        logger = StructuredLogger(cfg)
        from core.models.audit import AuditEvent, AuditEventCategory
        logger.audit(AuditEvent(category=AuditEventCategory.SYSTEM))
        assert not Path(tmp_config.audit_log_path).exists()

    def test_convenience_methods(self, tmp_config):
        logger = StructuredLogger(tmp_config)
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        logger.critical("c")
        lines = Path(tmp_config.debug_log_path).read_text().strip().split("\n")
        levels = [json.loads(l)["level"] for l in lines]
        assert levels == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


# ========== 2. 指标收集器 ==========

class TestMetricsCollector:
    @pytest.fixture
    def m(self):
        return MetricsCollector()

    def test_counter(self, m):
        m.inc("requests", 3)
        m.inc("requests", 2)
        assert m.counters["requests"] == 5

    def test_counter_with_tags(self, m):
        m.inc("llm_calls", 1, {"model": "gpt-4"})
        m.inc("llm_calls", 1, {"model": "claude"})
        assert m.counters['llm_calls{model="claude"}'] == 1
        assert m.counters['llm_calls{model="gpt-4"}'] == 1

    def test_gauge(self, m):
        m.gauge("memory_mb", 256.0)
        assert m.gauges["memory_mb"] == 256.0
        m.gauge("memory_mb", 128.0)
        assert m.gauges["memory_mb"] == 128.0

    def test_histogram(self, m):
        for v in [10, 20, 30, 40, 50]:
            m.observe("latency", v)
        assert m.percentile("latency", 50) == 30.0
        assert m.percentile("latency", 99) == 50.0

    def test_empty_percentile(self, m):
        assert m.percentile("nonexistent", 50) == 0.0

    def test_summary(self, m):
        m.inc("requests", 5)
        m.gauge("cpu", 80.0)
        for v in [10, 20, 30]:
            m.observe("latency", v)
        s = m.summary()
        assert s["counters"]["requests"] == 5
        assert s["gauges"]["cpu"] == 80.0
        assert s["histograms"]["latency"]["count"] == 3

    def test_prometheus_export(self, m):
        m.inc("test_counter", 42)
        m.gauge("test_gauge", 3.14)
        m.observe("test_hist", 5)
        out = m.to_prometheus()
        assert "test_counter 42" in out
        assert "test_gauge 3.14" in out
        assert "test_hist_count 1" in out
        assert "test_hist_sum 5" in out


# ========== 3. Span 追踪 ==========

class TestTracer:
    @pytest.fixture
    def tracer(self):
        return Tracer()

    def test_start_and_finish(self, tracer):
        span = tracer.start_span("llm_call")
        assert span.operation == "llm_call"
        assert span.span_id in tracer._spans
        span.finish()
        assert span.end_ns is not None
        assert span.duration_ms >= 0

    def test_parent_child(self, tracer):
        parent = tracer.start_span("orchestration")
        child = tracer.start_span("tool_exec", parent=parent)
        assert child.trace_id == parent.trace_id
        assert child.parent_span_id == parent.span_id

    def test_error_span(self, tracer):
        span = tracer.start_span("llm_call")
        span.finish(error="timeout")
        assert span.error == "timeout"

    def test_duration_without_finish(self, tracer):
        span = tracer.start_span("test")
        assert span.duration_ms >= 0


# ========== 4. 领域指标 ==========

class TestAgentMetrics:
    @pytest.fixture
    def agent(self):
        return AgentMetrics(MetricsCollector())

    def test_record_llm_call(self, agent):
        agent.record_llm_call("gpt-4", 1000, 500, 2000, True)
        assert agent._m.counters['chacha_llm_calls_total{model="gpt-4",status="success"}'] == 1

    def test_record_tool_call(self, agent):
        agent.record_tool_call("read_file", 150, True, 100)
        # tags 按字母序：status 在 tool 之后
        key = 'chacha_tool_calls_total{status="success",tool="read_file"}'
        assert agent._m.counters[key] == 1
        hist_key = 'chacha_tool_duration_ms{status="success",tool="read_file"}'
        assert len(agent._m.histograms.get(hist_key, [])) == 1

    def test_record_hook(self, agent):
        agent.record_hook("security", 50, "block")
        assert agent._m.counters['chacha_hook_calls_total{action="block",hook="security"}'] == 1

    def test_record_session(self, agent):
        agent.record_session("s1", 5000, 0.15, 300000)
        assert agent._m.counters["chacha_sessions_total"] == 1

    def test_record_cost(self, agent):
        agent.record_cost("gpt-4", 0.015)
        assert agent._m.gauges["chacha_cost_cumulative_usd"] == 0.015
        agent.record_cost("gpt-4", 0.005)
        assert agent._m.gauges["chacha_cost_cumulative_usd"] == 0.02

    def test_record_context(self, agent):
        agent.record_context(50000, 0.75, compression_triggered=True)
        assert agent._m.gauges["chacha_context_tokens"] == 50000
        assert agent._m.counters["chacha_context_compressions_total"] == 1


# ========== 5. Telemetry 集成 ==========

class TestTelemetry:
    @pytest.fixture
    def telemetry(self):
        d = Path(tempfile.mkdtemp())
        cfg = TelemetryConfig(
            log_level="INFO",
            debug_log_path=str(d / "debug.jsonl"),
            audit_log_path=str(d / "audit.jsonl"),
        )
        t = Telemetry(cfg)
        t.start()
        yield t
        t.stop()

    def test_start_stop(self, telemetry):
        assert telemetry is not None

    def test_logger_accessible(self, telemetry):
        telemetry.logger.info("from telemetry")
        path = Path(telemetry._config.debug_log_path)
        lines = path.read_text().strip().split("\n")
        msgs = [json.loads(l)["msg"] for l in lines]
        assert "from telemetry" in msgs

    def test_agent_accessible(self, telemetry):
        telemetry.agent.record_llm_call("test", 100, 50, 500, True)
        assert telemetry.metrics.counters['chacha_llm_calls_total{model="test",status="success"}'] == 1

    def test_prometheus_export(self, telemetry):
        telemetry.metrics.inc("hello", 1)
        out = telemetry.prometheus_export()
        assert "hello 1" in out

    def test_hash_id(self, telemetry):
        h = telemetry.hash_id("session-abc")
        assert len(h) == 12
        assert telemetry.hash_id("session-abc") == h  # 确定性
