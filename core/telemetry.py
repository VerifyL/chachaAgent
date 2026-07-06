"""
core/telemetry.py
Telemetry — 统一可观测性：结构化日志、指标收集、审计写入、Prometheus（可选）。

设计理念（融合 Harness MetricsCollector + StructuredLogger）：
1. 双轨日志：debug.jsonl（研发调试）+ audit.jsonl（安全审计，接入 AuditRecord）
2. 指标收集：counter / gauge / histogram + P50/P99 百分位
3. 领域指标（AgentMetrics）：工具调用、LLM 延迟、token 消耗、钩子耗时、会话统计
4. Prometheus：可开关（enable_prometheus 配置），暴露 /metrics 端点
5. 单进程 Span 追踪：trace_id 关联一次 LLM 调用→工具执行→响应的全链路

用法:
    telemetry = Telemetry(config.telemetry)
    telemetry.start()
    telemetry.agent.record_llm_call("gpt-4", 2000, 500, 150, True)
    telemetry.stop()
"""

import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models.audit import AuditRecord
from core.models.config import TelemetryConfig

# ========================= 日志级别 =========================

class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ========================= 结构化日志记录器 =========================

class StructuredLogger:
    """双轨结构化日志记录器（参考 Harness StructuredLogger）"""

    def __init__(self, config: TelemetryConfig):
        self._config = config
        self._session_id = ""
        self._level_rank = {
            LogLevel.DEBUG: 10, LogLevel.INFO: 20,
            LogLevel.WARNING: 30, LogLevel.ERROR: 40, LogLevel.CRITICAL: 50,
        }
        self._min_level = self._level_rank.get(LogLevel(config.log_level), 20)
        log_dir = Path(config.log_dir)
        self._debug_path = log_dir / "debug.jsonl"
        self._audit_path = log_dir / "audit.jsonl"
        self._lock = threading.Lock()
        self._enable_audit = config.enable_audit

    def _should_log(self, level: LogLevel) -> bool:
        return self._level_rank.get(level, 20) >= self._min_level

    def _write_line(self, path: Path, entry: Dict[str, Any]) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def log(self, level: LogLevel, message: str, **kwargs) -> None:
        if not self._should_log(level):
            return
        entry = {
            "ts": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
            "level": level.value,
            "session": self._session_id or "",
            "msg": message,
            **kwargs,
        }
        self._write_line(self._debug_path, entry)

    def audit(self, record: AuditRecord) -> None:
        """写入审计日志（== audit.jsonl）"""
        if not self._enable_audit:
            return
        self._write_line(self._audit_path, record.model_dump())

    def debug(self, msg: str, **kw) -> None: self.log(LogLevel.DEBUG, msg, **kw)
    def info(self, msg: str, **kw) -> None: self.log(LogLevel.INFO, msg, **kw)
    def warning(self, msg: str, **kw) -> None: self.log(LogLevel.WARNING, msg, **kw)
    def error(self, msg: str, **kw) -> None: self.log(LogLevel.ERROR, msg, **kw)
    def critical(self, msg: str, **kw) -> None: self.log(LogLevel.CRITICAL, msg, **kw)


# ========================= 指标收集器 =========================

class MetricsCollector:
    """指标收集器：counter / gauge / histogram """

    def __init__(self):
        self.counters: Dict[str, int] = defaultdict(int)
        self.gauges: Dict[str, float] = {}
        self.histograms: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._start_time = time.time()

    def inc(self, name: str, value: int = 1, tags: Optional[Dict[str, str]] = None) -> None:
        self.counters[self._key(name, tags)] += value

    def gauge(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        self.gauges[self._key(name, tags)] = value

    def observe(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        self.histograms[self._key(name, tags)].append({
            "value": value, "ts": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
        })

    def percentile(self, name: str, pct: int) -> float:
        """P50 / P99 百分位"""
        values = sorted([v["value"] for v in self.histograms.get(name, [])])
        if not values:
            return 0.0
        idx = min(int(len(values) * pct / 100), len(values) - 1)
        return values[idx]

    def summary(self) -> Dict[str, Any]:
        hist_summary = {}
        for name, vals in self.histograms.items():
            nums = [v["value"] for v in vals]
            hist_summary[name] = {
                "count": len(nums), "min": min(nums), "max": max(nums),
                "avg": sum(nums) / len(nums) if nums else 0,
                "p50": self.percentile(name, 50), "p99": self.percentile(name, 99),
            }
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "histograms": hist_summary,
            "uptime_seconds": time.time() - self._start_time,
        }

    def to_prometheus(self) -> str:
        """导出为 Prometheus 文本格式"""
        lines = []
        for key, v in self.counters.items():
            name, tags_str = self._split_key(key)
            lines.append(f"{name}{tags_str} {v}")
        for key, v in self.gauges.items():
            name, tags_str = self._split_key(key)
            lines.append(f"{name} {v}")
        for key, vals in self.histograms.items():
            name, tags_str = self._split_key(key)
            lines.append(f"{name}_count{tags_str} {len(vals)}")
            if vals:
                nums = [v["value"] for v in vals]
                lines.append(f"{name}_sum{tags_str} {sum(nums)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, tags: Optional[Dict[str, str]] = None) -> str:
        if not tags:
            return name
        tg = ",".join(f'{k}="{v}"' for k, v in sorted(tags.items()))
        return f"{name}{{{tg}}}"

    @staticmethod
    def _split_key(key: str):
        if "{" not in key:
            return key, ""
        name, rest = key.split("{", 1)
        return name, "{" + rest


# ========================= 单进程 Span 追踪 =========================

@dataclass
class Span:
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    operation: str = ""
    start_ns: int = field(default_factory=time.time_ns)
    end_ns: Optional[int] = None
    tags: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def finish(self, error: Optional[str] = None) -> "Span":
        self.end_ns = time.time_ns()
        self.error = error
        return self

    @property
    def duration_ms(self) -> float:
        if self.end_ns is None:
            return (time.time_ns() - self.start_ns) / 1e6
        return (self.end_ns - self.start_ns) / 1e6


class Tracer:
    """单进程 Span 追踪器（参考 Harness Tracer，当前无分布式需求）"""

    def __init__(self):
        self._spans: Dict[str, Span] = {}

    def start_span(self, operation: str, parent: Optional["Span"] = None,
                   tags: Optional[Dict[str, str]] = None) -> Span:
        span = Span(
            trace_id=parent.trace_id if parent else uuid.uuid4().hex[:32],
            parent_span_id=parent.span_id if parent else None,
            operation=operation,
            tags=tags or {},
        )
        self._spans[span.span_id] = span
        return span

    def finish_span(self, span: Span, error: Optional[str] = None) -> Span:
        span.finish(error)
        return span

    def get_span(self, span_id: str) -> Optional[Span]:
        return self._spans.get(span_id)


# ========================= 领域指标 =========================

class AgentMetrics:
    """Agent 领域指标（参考 Harness AgentMetrics，扩展 LLM/钩子/会话维度）"""

    def __init__(self, collector: MetricsCollector):
        self._m = collector

    # ---- LLM 调用 ----
    def record_llm_call(self, model: str, input_tokens: int, output_tokens: int,
                        latency_ms: float, success: bool) -> None:
        tags = {"model": model, "status": "success" if success else "error"}
        self._m.inc("chacha_llm_calls_total", tags=tags)
        self._m.inc("chacha_llm_input_tokens_total", input_tokens, tags={"model": model})
        self._m.inc("chacha_llm_output_tokens_total", output_tokens, tags={"model": model})
        self._m.observe("chacha_llm_latency_ms", latency_ms, tags=tags)
        self._m.gauge("chacha_llm_last_latency_ms", latency_ms, tags={"model": model})

    # ---- 工具调用 ----
    def record_tool_call(self, tool_name: str, duration_ms: float,
                         success: bool, output_lines: int = 0,
                         _logger: Optional[StructuredLogger] = None) -> None:
        tags = {"tool": tool_name, "status": "success" if success else "error"}
        self._m.inc("chacha_tool_calls_total", tags=tags)
        self._m.observe("chacha_tool_duration_ms", duration_ms, tags=tags)
        self._m.observe("chacha_tool_output_lines", output_lines, tags={"tool": tool_name})
        if _logger:
            _logger.info("工具调用", tool=tool_name, duration_ms=int(duration_ms),
                         success=success, output_lines=output_lines)

    # ---- 钩子 ----
    def record_hook(self, hook_name: str, duration_ms: float, action: str) -> None:
        self._m.inc("chacha_hook_calls_total", tags={"hook": hook_name, "action": action})
        self._m.observe("chacha_hook_duration_ms", duration_ms, tags={"hook": hook_name})

    # ---- 会话 ----
    def record_session(self, session_id: str, total_tokens: int,
                       total_cost: float, duration_ms: int) -> None:
        self._m.inc("chacha_sessions_total")
        self._m.observe("chacha_session_tokens", total_tokens)
        self._m.observe("chacha_session_cost_usd", total_cost)
        self._m.observe("chacha_session_duration_ms", duration_ms)

    # ---- 成本 ----
    def record_cost(self, model: str, cost_usd: float) -> None:
        self._m.inc("chacha_cost_total_usd", int(cost_usd * 1000), tags={"model": model})
        current = self._m.gauges.get("chacha_cost_cumulative_usd", 0.0)
        self._m.gauge("chacha_cost_cumulative_usd", current + cost_usd)

    # ---- 上下文 ----
    def record_context(self, total_tokens: int, utilization: float,
                       compression_triggered: bool = False) -> None:
        self._m.gauge("chacha_context_tokens", total_tokens)
        self._m.gauge("chacha_context_utilization", utilization)
        if compression_triggered:
            self._m.inc("chacha_context_compressions_total")


# ========================= 统一可观测性 =========================

class Telemetry:
    """
    统一可观测性入口。

    用法:
        t = Telemetry(TelemetryConfig(log_level="INFO"))
        t.start()
        t.agent.record_llm_call("gpt-4", 1000, 500, 2000, True)
        t.logger.info("session started", session_id="s1")
        t.stop()
    """

    def __init__(self, config: Optional[TelemetryConfig] = None):
        cfg = config if config and config.enabled else TelemetryConfig(enabled=False)
        self._config = cfg
        self.enabled = cfg.enabled
        self.logger = StructuredLogger(cfg) if cfg.enabled else None
        self.metrics = MetricsCollector() if cfg.enabled else None
        self.tracer = Tracer() if cfg.enabled else None
        self.agent = AgentMetrics(self.metrics) if cfg.enabled and self.metrics else None

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id
        if self.logger:
            self.logger._session_id = session_id

    def start(self) -> None:
        self.logger.info("Telemetry 已启动",
                         prometheus=self._config.enable_prometheus,
                         audit=self._config.enable_audit)

    def stop(self) -> None:
        """停止遥测，导出最终指标摘要"""
        if self.logger and self.metrics:
            summary = self.metrics.summary()
            self.logger.info("Telemetry 已停止", metrics_summary=summary)

    def toggle(self, enable: bool) -> None:
        """运行时热切换遥测开关。

        子系统持有 Telemetry 对象引用，运行时检查 logger/metrics/agent，
        因此翻转 enabled + 重建/清空内部组件即可实现热切换，无需重建 Dispatcher/ToolExecutor。

        Args:
            enable: True=开启遥测, False=关闭遥测
        """
        if enable and not self.enabled:
            self.enabled = True
            self._config.enabled = True
            self.logger = StructuredLogger(self._config)
            self.metrics = MetricsCollector()
            self.tracer = Tracer()
            self.agent = AgentMetrics(self.metrics)
            self.logger.info("Telemetry 运行时开启")
        elif not enable and self.enabled:
            if self.logger:
                self.logger.info("Telemetry 运行时关闭")
            self.enabled = False
            self._config.enabled = False
            self.logger = None
            self.metrics = None
            self.tracer = None
            self.agent = None

    def prometheus_export(self) -> str:
        """导出 Prometheus 格式的指标文本"""
        return self.metrics.to_prometheus()

    def _make_trace_id(self) -> str:
        return uuid.uuid4().hex[:32]

    def hash_id(self, value: str) -> str:
        """生成匿名化 ID（用于错误报告）"""
        return hashlib.sha256(value.encode()).hexdigest()[:12]

    # ====== 查询接口（供 CLI 仪表盘使用） ======

    def snapshot(self) -> Dict[str, Any]:
        """完整遥测快照，包含指标摘要、运行时间、配置信息。

        供 /telemetry 命令使用，返回结构化 dict 由 CLI 渲染为 Rich 表格。
        """
        if not self.enabled or not self.metrics:
            return {"enabled": False}
        return {
            "enabled": True,
            "log_level": self._config.log_level,
            "log_dir": str(self._config.log_dir),
            "audit_enabled": self._config.enable_audit,
            "metrics": self.metrics.summary(),
            "uptime_seconds": time.time() - self.metrics._start_time,
        }

    def read_logs(self, log_type: str = "debug", n: int = 10,
                  level: Optional[str] = None,
                  filter_text: Optional[str] = None) -> List[Dict[str, Any]]:
        """读取最近 N 条日志，支持按级别/关键词过滤。

        Args:
            log_type: "debug" 或 "audit"
            n: 返回最近 N 条
            level: 按级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL）
            filter_text: 按关键词过滤（搜索整条 JSON）

        Returns:
            日志条目列表（最近 N 条在后）
        """
        if not self._config:
            return []
        path = Path(self._config.log_dir) / f"{log_type}.jsonl"
        if not path.exists():
            return []
        lines: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if level and entry.get("level", "").upper() != level.upper():
                        continue
                    if filter_text and filter_text.lower() not in json.dumps(
                        entry, ensure_ascii=False, default=str
                    ).lower():
                        continue
                    lines.append(entry)
        except Exception:
            return []
        return lines[-n:] if n > 0 else lines

    def list_spans(self) -> List[Dict[str, Any]]:
        """列出所有 Span，按耗时降序排列。

        供 /trace 命令使用。
        """
        if not self.tracer:
            return []
        spans = []
        for sid, span in self.tracer._spans.items():
            spans.append({
                "span_id": span.span_id[:8],
                "trace_id": span.trace_id[:16],
                "parent": span.parent_span_id[:8] if span.parent_span_id else "-",
                "operation": span.operation,
                "duration_ms": round(span.duration_ms, 1),
                "error": span.error,
                "tags": span.tags,
            })
        return sorted(spans, key=lambda s: s["duration_ms"], reverse=True)

    def cost_summary(self) -> Dict[str, Any]:
        """成本汇总：按模型拆分 + 累计总成本。

        供 /cost 命令使用。
        """
        if not self.metrics:
            return {"total_cost_usd": 0.0, "by_model": {}}
        counters = self.metrics.counters
        by_model: Dict[str, float] = {}
        for key, val in counters.items():
            if key.startswith("chacha_cost_total_usd"):
                cost = val / 1000.0  # counter 存储时 *1000，恢复实际成本
                if "{" in key:
                    tags_part = key.split("{", 1)[1].rstrip("}")
                    for pair in tags_part.split(","):
                        k, v = pair.split("=", 1)
                        if k.strip() == "model":
                            model = v.strip('"')
                            by_model[model] = by_model.get(model, 0.0) + cost
                            break
        cumulative = self.metrics.gauges.get("chacha_cost_cumulative_usd", 0.0)
        return {
            "total_cost_usd": round(cumulative, 6),
            "by_model": {m: round(c, 6) for m, c in sorted(by_model.items())},
        }
