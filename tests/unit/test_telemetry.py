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

from core.models.config import TelemetryConfig
from core.telemetry import (
    AgentMetrics,
    MetricsCollector,
    StructuredLogger,
    Telemetry,
    Tracer,
)

# ========== 1. 结构化日志 ==========

class TestStructuredLogger:
    @pytest.fixture
    def tmp_config(self):
        d = Path(tempfile.mkdtemp())
        return TelemetryConfig(
            log_level="DEBUG",
            log_dir=d,
            enable_audit=True,
            enabled=True,
        )

    def _debug_path(self, cfg: TelemetryConfig) -> Path:
        return cfg.log_dir / "debug.jsonl"

    def _audit_path(self, cfg: TelemetryConfig) -> Path:
        return cfg.log_dir / "audit.jsonl"

    def test_write_debug_log(self, tmp_config):
        logger = StructuredLogger(tmp_config)
        logger.info("test message", key="value")
        lines = self._debug_path(tmp_config).read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["level"] == "INFO"
        assert entry["msg"] == "test message"
        assert entry["key"] == "value"

    def test_level_filtering(self, tmp_config):
        d = Path(tempfile.mkdtemp())
        cfg = TelemetryConfig(
            log_level="WARNING",
            log_dir=d,
            enable_audit=True,
            enabled=True,
        )
        logger = StructuredLogger(cfg)
        logger.debug("should be filtered")
        logger.info("should be filtered")
        logger.warning("should appear")
        logger.error("should appear")
        lines = (cfg.log_dir / "debug.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_audit_log_disabled(self, tmp_config):
        d = Path(tempfile.mkdtemp())
        cfg = TelemetryConfig(
            log_level="INFO",
            log_dir=d,
            enable_audit=False,
            enabled=True,
        )
        logger = StructuredLogger(cfg)
        from core.models.audit import AuditEvent, AuditEventCategory
        logger.audit(AuditEvent(category=AuditEventCategory.SYSTEM))
        assert not (cfg.log_dir / "audit.jsonl").exists()

    def test_convenience_methods(self, tmp_config):
        logger = StructuredLogger(tmp_config)
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        logger.critical("c")
        lines = self._debug_path(tmp_config).read_text().strip().split("\n")
        levels = [json.loads(line)["level"] for line in lines]
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
        agent.record_context(500000, 0.48)
        assert agent._m.gauges["chacha_context_tokens"] == 500000
        assert agent._m.gauges["chacha_context_utilization"] == 0.48


# ========== 5. Telemetry 总成 ==========

class TestTelemetry:
    @pytest.fixture
    def cfg(self):
        d = Path(tempfile.mkdtemp())
        return TelemetryConfig(
            log_level="INFO",
            log_dir=d,
            enable_audit=True,
            enabled=True,
        )

    def test_start_stop_lifecycle(self, cfg):
        """start/stop 不抛异常即可"""
        t = Telemetry(cfg)
        t.start()
        assert t.enabled is True
        t.stop()

    def test_logging_through_telemetry(self, cfg):
        """通过 Telemetry.logger 写入日志"""
        t = Telemetry(cfg)
        t.start()
        t.logger.info("hello")
        t.stop()
        lines = (cfg.log_dir / "debug.jsonl").read_text().strip().split("\n")
        assert len(lines) >= 2
        # start() 先写入 "Telemetry 已启动"，然后才是 "hello"
        msgs = [json.loads(line)["msg"] for line in lines]
        assert "hello" in msgs
