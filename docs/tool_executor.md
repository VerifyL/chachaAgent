# 工具执行器 (`core/tool_executor.py`)

本文档说明 `ToolExecutor` 的执行流程、错误处理和与其他模块的联动。工具执行器是薄胶水层，编程已有 PolicyEngine / HookOrchestrator / Telemetry 完成编排。

## 概述

设计融合了 **Claude Code StreamingToolExecutor**（并发 + 错误包装）和 **Harness 错误即观察**（异常不抛，包装为结果）：

- **薄胶水层**：不重复实现策略、钩子、遥测，只编排调用顺序
- **并发执行**：`asyncio.Semaphore` 控制上限，`execute_batch()` 批量并发
- **超时+重试**：默认 60s 超时，指数退避（1s, 2s, 4s），仅超时可重试
- **错误即观察**：执行异常不抛，包装为 `ToolResult(error=True)` 反馈给 LLM

### 执行流程

```
execute("shell", {"cmd": "ls"}, session_id)
  │
  ├─ 0. Find tool in registry
  │     → 未注册：ToolResult(status="error", error="not found")
  │
  ├─ 1. PolicyEngine.evaluate_tool("shell", "ls", session_id)
  │     → blacklist 命中：ToolResult(status="blocked", error="...")
  │     → needs_approval：等待 Orchestrator 审批
  │
  ├─ 2. HookOrchestrator.run(PRE_TOOL_EXECUTION, tool_call=ctx)
  │     → BLOCK：ToolResult(status="blocked")
  │     → MODIFY：更新 arguments
  │
  ├─ 3. Execute with Semaphore + timeout + retry
  │     → 超时：指数退避 1s → 2s → 4s
  │     → 异常：不重试，直接返回 error
  │
  ├─ 4. Truncate (>100K chars → 截断 + "[truncated]")
  │
  ├─ 5. HookOrchestrator.run(POST_TOOL_EXECUTION)
  │
  ├─ 6. telemetry.agent.record_tool_call(name, duration, success, lines)
  │
  └─ → ToolResult(status, output, error, duration_ms, truncated)
```

---

## 1. ToolResult

```python
@dataclass
class ToolResult:
    tool_use_id: str
    tool_name: str
    status: str = "success"       # success | error | blocked | timeout
    output: str = ""               # 工具输出文本
    error: Optional[str] = None    # 错误详情
    duration_ms: int = 0           # 耗时（毫秒）
    truncated: bool = False        # 输出是否被截断
```

| status | 含义 | 何时返回 |
|--------|------|----------|
| `success` | 执行成功 | 正常完成 |
| `error` | 执行失败 | 工具未注册 / 运行时异常 |
| `blocked` | 被拦截 | 黑名单命中 / 钩子 BLOCK / 需要审批 |
| `timeout` | 超时 | 超过 `default_timeout` 且重试耗尽 |

---

## 2. 执行 API

### 2.1 `execute()` — 单个工具

```python
result = await executor.execute(
    tool_name="read_file",
    arguments={"path": "/tmp/main.py"},
    session_id="session-abc",
    tool_use_id="call-1",
)
```

### 2.2 `execute_batch()` — 批量并发

```python
calls = [
    {"tool_name": "read_file", "arguments": {"path": "/a"}, "tool_use_id": "c1"},
    {"tool_name": "shell", "arguments": {"cmd": "ls"}, "tool_use_id": "c2"},
]
results = await executor.execute_batch(calls, "s1")
```

并发数由 `max_concurrent` 控制（默认 5）。

---

## 3. 超时与重试

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `default_timeout` | 60.0 | 单次执行超时 |
| `max_retries` | 2 | 超时后重试次数 |

**重试策略**：仅超时重试，权限/运行时异常不重试（参考 Harness）。

```
attempt 1 → 超时 → backoff 1s
attempt 2 → 超时 → backoff 2s
attempt 3 → 超时 → ToolResult(status="timeout")
```

---

## 4. 输出截断

输出超过 `max_output_chars`（默认 100K 字符）时自动截断：

```
原始输出: 150K chars → 截断为 100K + "\n... [truncated]"
ToolResult.truncated = True
```

---

## 5. 联动模块

| 模块 | 何时调用 | 影响 |
|------|---------|------|
| `PolicyEngine` | 步骤 1 | 拦截黑名单命令、触发审批 |
| `HookOrchestrator` | 步骤 2 + 5 | 前置/后置钩子：BLOCK/MODIFY/CONTINUE |
| `Telemetry` | 步骤 6 | 记录工具调用指标（次数、耗时、输出行数） |

**与 Orchestrator 集成**：

```python
# Orchestrator 收到审批请求后
if result.status == "blocked" and "Approval required" in (result.error or ""):
    approved = await request_user_approval(decision)
    if approved:
        policy.record_approval(decision.cache_key, True)
        policy.grant_task_approval(session_id, tool_name)
        result = await executor.execute(...)  # 重新执行
```
