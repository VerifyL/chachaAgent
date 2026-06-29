# 工具执行器 (`core/tool_executor.py`)

本文档说明 `ToolExecutor` 的执行流程、错误处理和与其他模块的联动。工具执行器是薄胶水层，编程已有 PolicyEngine / HookOrchestrator / Telemetry 完成编排。

## 概述

设计融合了 **StreamingToolExecutor**（并发 + 错误包装）和 **Harness 错误即观察**（异常不抛，包装为结果）：

- **薄胶水层**：不重复实现策略、钩子、遥测，只编排调用顺序
- **并发执行**：`asyncio.Semaphore` 控制上限，`execute_batch()` 批量并发
- **超时+重试**：默认 60s 超时，指数退避（1s, 2s, 4s），仅超时可重试
- **错误即观察**：执行异常不抛，包装为 `ToolResult(error=True)` 反馈给 LLM

### 执行流程

```
execute("bash", {"cmd": "ls"}, session_id)
 │
 ├─ 0. Find tool in registry
 │ → 未注册：ToolResult(status="error", error="not found")
 │
 ├─ 1. PolicyEngine.evaluate_tool("bash", "ls", session_id)
 │ → blacklist 命中：ToolResult(status="blocked", error="...")
 │ → needs_approval：等待 Orchestrator 审批
 │
 ├─ 2. HookOrchestrator.run(PRE_TOOL_EXECUTION, tool_call=ctx)
 │ → BLOCK：ToolResult(status="blocked")
 │ → MODIFY：更新 arguments
 │
 ├─ 3. Execute with Semaphore + timeout + retry
 │ → 超时：指数退避 1s → 2s → 4s
 │ → 异常：不重试，直接返回 error
 │
 ├─ 4. Truncate (>200K chars → 截断 + cache_key，可用 cache_read 续读)
 │
 ├─ 5. HookOrchestrator.run(POST_TOOL_EXECUTION)
 │
 ├─ 6. telemetry.agent.record_tool_call(name, duration, success, lines)
 │
 └─ → ToolResult(status, output, error, duration_ms, truncated)
```

---

## 1. ToolResult 处理

### 1.1 `_execute_with_retry` 返回 5 元组

```python
# 工具层返回 ToolResult → 执行层解包为 5 元组
if isinstance(output, ToolResult):
    return output.content, output.error, output.status, output.data, output.warnings
return str(output), None, "success", {}, []
```

- `content`: 工具产出的纯净文本（非 Python repr）
- `data`: 工具级结构化元数据（如 task 的 `agent_type`、read 的 `path`）
- `warnings`: 非致命警告，透传到外层 ToolResult

### 1.2 截断与缓存

输出超过 `max_output_chars`（默认 200K 字符）时，截断作用在纯净 `content` 字符串上，
**不破坏 JSON 结构**。完整内容存入 `_output_cache`，LLM 可用 `cache_read` 工具续读。

```
原始 content: 250K → 截断为 200K → ToolResult(truncated=true, cache_key="xxx")
                                                    ↓
                                          _output_cache["xxx"] = 完整 250K
```

缓存 10 分钟过期，`_cleanup_cache()` 定期清理。

### 1.3 status 来源

| 层级 | 谁给 | 何时 |
|------|------|------|
| 工具层 | 工具自身 | 工具判定成功/失败 → `"success"` / `"error"` |
| 执行层 | ToolExecutor | 仅覆盖 timeout/blocked 等执行层异常 |

正常路径下 status 由工具决定，ToolExecutor 透传。

---

## 2. 执行 API

### 2.1 `execute()` — 单个工具

```python
result = await executor.execute(
 tool_name="read",
 arguments={"path": "/tmp/main.py"},
 session_id="session-abc",
 tool_use_id="call-1",
)
```

### 2.2 `execute_batch()` — 批量并发

```python
calls = [
 {"tool_name": "read", "arguments": {"path": "/a"}, "tool_use_id": "c1"},
 {"tool_name": "bash", "arguments": {"cmd": "ls"}, "tool_use_id": "c2"},
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

## 4. 输出截断与缓存

截断逻辑已统一到 [§1.2 截断与缓存](#12-截断与缓存)：200K 阈值，纯净 content 上截断，`cache_read` 续读。

---

## 5. 联动模块

| 模块 | 何时调用 | 影响 |
|------|---------|------|
| `PolicyEngine` | 步骤 1 | 拦截黑名单命令、触发审批 |
| `HookOrchestrator` | 步骤 2 + 5 | 前置/后置钩子：BLOCK/MODIFY/CONTINUE |
| `Telemetry` | 步骤 6 | 记录工具调用指标（次数、耗时、输出行数） |

**Hook 豁免**：`memory` 工具（含 topic_write 等 action）绕过 PRE 钩子（保证记忆写入不被全局规则意外阻塞）。后置钩子不受影响。

**与 Orchestrator 集成**：

```python
# Orchestrator 收到审批请求后
if result.status == "blocked" and "Approval required" in (result.error or ""):
 approved = await request_user_approval(decision)
 if approved:
 policy.record_approval(decision.cache_key, True)
 policy.grant_task_approval(session_id, tool_name)
 result = await executor.execute(...) # 重新执行
```
