# 统一可观测性 (`core/telemetry.py`)

本文档说明 `Telemetry` 的结构化日志、指标收集、Span 追踪和 Prometheus 导出。该模块采用 **"被调用者"模式** —— 不主动监听任何事件，由其他模块（Orchestrator、Gateway、LLMInvoker等）在完成任务后主动调用记录方法。

## 概述

设计融合了 **可观测性三大支柱**（Metrics + Logs + Traces）：

- **双轨日志**：debug.jsonl（研发调试）+ audit.jsonl（安全审计）
- **指标收集**：counter / gauge / histogram + P50/P99 百分位
- **领域指标**：LLM 调用、工具调用、会话、上下文利用率
- **单进程追踪**：Span 用 trace_id 关联全链路（预留）

### 调用模式（CLI 集成）

```
ChatEngine.send_message()
  │
  ├─ ContextManager.assemble() → telemetry.agent.record_context()
  ├─ Dispatcher.dispatch_stream()
  │    ├─ LLMInvoker → telemetry.agent.record_llm_call()
  │    ├─ ToolExecutor → telemetry.agent.record_tool_call() + audit.jsonl
  │    └─ ChatEngine → debug.jsonl ("LLM 调用", "本轮完成")
  │
  └─ SessionService.add_round() → telemetry.agent.record_session()
```

---

## 1. 结构化日志

### 1.1 日志级别

```python
class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
```

级别过滤：`log_level` 配置决定最低输出级别。如 `log_level="WARNING"` 则 DEBUG/INFO 被静默忽略。

### 1.2 双轨输出

| 轨道 | 文件 | 消费者 | 格式 |
|------|------|--------|------|
| debug | `.chacha_agent/logs/debug.jsonl` | 开发者 | `{"ts":"...","level":"INFO","msg":"...","key":"value"}` |
| audit | `.chacha_agent/logs/audit.jsonl` | `AuditRecord` | 调用 `record.model_dump()` |

### 1.3 使用

```python
# 通用日志（写入 debug.jsonl）
telemetry.logger.info("session started", session_id="s1", project_id="p1")
telemetry.logger.error("config load failed", path="/tmp/config.toml")

# 审计日志（写入 audit.jsonl）
from core.models.audit import CostAuditEvent
audit = CostAuditEvent(model_name="gpt-4", provider="openai", ...)
telemetry.logger.audit(audit)
```

---

## 2. 指标收集

### 2.1 三种指标类型

```python
MetricsCollector
  .inc(name, value=1, tags=None)      # counter：累加
  .gauge(name, value, tags=None)       # gauge：瞬时值
  .observe(name, value, tags=None)     # histogram：分布
  .percentile(name, pct)              # P50 / P99
  .summary()                           # 全部指标快照
```

### 2.2 Prometheus 导出

```python
output = telemetry.metrics.to_prometheus()
# chacha_llm_calls_total{model="gpt-4",status="success"} 42
# chacha_llm_latency_ms_count{model="gpt-4",status="success"} 42
# chacha_llm_latency_ms_sum{model="gpt-4",status="success"} 84000
```

`enable_prometheus=true` 时，`/metrics` 端点直接返回此文本。

### 2.3 指标键格式

| 类型 | 无标签 | 有标签 |
|------|--------|--------|
| counter | `requests` | `requests{model="gpt-4"}` |
| gauge | `memory_mb` | `memory_mb` |
| histogram | `latency_count{...}` / `latency_sum{...}` | 同左 |

标签按字母序排列，保证键确定性。

---

## 3. 领域指标

```python
telemetry.agent  # AgentMetrics 实例
```

| 方法 | 记录内容 | 指标示例 |
|------|----------|----------|
| `record_llm_call(model, input_tokens, output_tokens, latency_ms, success)` | LLM 调用次数、token、延迟 | `chacha_llm_calls_total` |
| `record_tool_call(tool_name, duration_ms, success, output_lines)` | 工具调用次数、耗时、输出行数 | `chacha_tool_calls_total` |
| `record_hook(hook_name, duration_ms, action)` | 钩子调用次数、耗时、决策 | `chacha_hook_calls_total` |
| `record_session(session_id, total_tokens, total_cost, duration_ms)` | 会话结束统计 | `chacha_sessions_total` |
| `record_cost(model, cost_usd)` | 逐次成本 + 累计成本（gauge） | `chacha_cost_cumulative_usd` |
| `record_context(total_tokens, utilization, compression_triggered)` | 上下文 token + 压缩次数 | `chacha_context_utilization` |

---

## 4. Span 追踪

```python
# Orchestrator 启动
root = telemetry.tracer.start_span("orchestration")

# LLM 调用
llm = telemetry.tracer.start_span("llm_call", parent=root, tags={"model": "gpt-4"})
llm.finish()  # → duration_ms 自动计算

# 工具执行
tool = telemetry.tracer.start_span("tool_exec", parent=root, tags={"tool": "read_file"})
tool.finish(error=None)

# trace_id 贯穿全链路
assert llm.trace_id == tool.trace_id == root.trace_id
```

`Span` 是单进程追踪（当前无分布式需求），`trace_id` 由第一个 Span 生成，子 Span 继承。

---

## 5. 配置

在 `~/.chacha/config.toml`：

```toml
[telemetry]
# enabled = true               # 开启结构化日志 + 审计（默认关闭）
# log_level = "INFO"           # DEBUG / INFO / WARNING / ERROR
```

---

## 6. 与 Harness 三大支柱的对应

| Harness 支柱 | Telemetry 实现 |
|-------------|---------------|
| Metrics（指标） | `MetricsCollector` + `AgentMetrics` |
| Logs（日志） | `StructuredLogger`（debug.jsonl + audit.jsonl） |
| Traces（追踪） | `Tracer` + `Span`（单进程 trace_id） |
