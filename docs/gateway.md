# 异步网关 (`protocol/gateway.py`)

本文档详细说明 `ChaChaAsyncGateway` 的设计原理、核心 API、数据流向和使用示例。网关是 ChachaAgent 的**统一消息总线**，所有组件（CLI/Web 前端、Orchestrator、LLMInvoker、ToolExecutor）均通过它收发 JSON-RPC 2.0 消息。

## 概述

设计融合了 **Harness EventBus**（发布-订阅 + 事件历史）和 **Claude Code 隐式背压**（阻塞等待而非拒绝）：

- **会话隔离**：每个会话独立 `asyncio.Queue`，慢前端不拖慢其他会话
- **全局有序**：`seq` 自增保证跨会话消息顺序
- **全局监听**：Telemetry/Audit 可一次性注册监听所有事件，不需要每会话重复订阅
- **背压可见**：`get_backpressure()` 返回 0~1 压力值，调用方可提前感知
- **事件历史**：保留最近 N 条完整消息，调试可追溯

**消息流水线**：

```
Producer (Orchestrator/LLMInvoker)
  │ publish(payload, session_id)
  ▼
Gateway._lock → 分配 seq → 包装 GatewayMessage
  │
  ├─→ _event_history (debug 追溯)
  ├─→ _global_handlers 异步执行 (Telemetry/Audit)
  └─→ _sessions[sid].queue 入队 (背压阻塞等待)
         │
         ▼
Consumer (CLI/Web 前端)
  async for msg in gateway.subscribe(sid):
      render(msg)
```

---

## 1. 初始化参数

```python
ChaChaAsyncGateway(
    max_queue_size: int = 10000,     # 每个会话队列的最大容量
    max_history: int = 500,          # event_history 保留条数，0=关闭
    publish_timeout: float = 10.0,   # 背压时阻塞等待的最大秒数
)
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_queue_size` | 10000 | 单会话消息队列容量。开发环境可调小测试背压行为 |
| `max_history` | 500 | 事件历史上限。开发调试设 1000，生产环境设 100，极限压测设 0 |
| `publish_timeout` | 10.0 | 队列满时 `put()` 阻塞的最长等待时间，超时返回 `False` |

---

## 2. 生命周期

### 2.1 启动

```python
gateway = ChaChaAsyncGateway()
await gateway.start()
```

- 设置 `_running = True`
- 之后才能 `publish()` / `subscribe()`

### 2.2 关闭

```python
await gateway.stop()
```

**优雅关闭流程**：

```
stop()
  ├─ _running = False
  ├─ 向所有会话队列发送 None 哨兵
  │    → subscribe() 的异步迭代器检测到 None → break
  ├─ 等待队列排空
  ├─ 清空 _sessions / _global_handlers / _event_history
  └─ 日志：关闭完成
```

- 重复调用 `stop()` 安全（幂等）
- 关闭后 `publish()` 返回 `False`

---

## 3. 核心 API

### 3.1 `register(session_id)` — 注册会话

```python
gateway.register("session-abc")
```

- 创建该会话的 `asyncio.Queue`
- 记录 `send_count` / `received_count` 用于背压计算
- 重复注册安全（仅警告，不创建重复队列）

### 3.2 `unregister(session_id)` — 注销会话

```python
gateway.unregister("session-abc")
```

- 清空队列中的剩余消息
- 移除会话上下文

### 3.3 `publish(payload, session_id, project_id)` — 发布消息

```python
ok = await gateway.publish(
    TokenChunkEvent().set_delta("你好"),
    session_id="session-abc",
)
```

**执行流程**：

```
1. _lock.acquire() → seq = _seq++
2. 包装 GatewayMessage(seq, project_id, session_id, payload)
3. _event_history.append(msg)
4. for handler in _global_handlers:
       asyncio.create_task(handler(msg))  # 异步，不阻塞
5. await _sessions[sid].queue.put(msg)    # 背压阻塞等待
6. send_count += 1
7. return True
```

| 返回值 | 含义 |
|--------|------|
| `True` | 成功入队（或 session_id 为空/未注册但仍通过全局监听者处理） |
| `False` | 队列满 + 超时 / gateway 已关闭 |

**session_id=None 时**：消息仅通过全局监听者处理，不入任何会话队列（用于系统级通知）。

### 3.4 `subscribe(session_id)` — 订阅消息

```python
async for msg in gateway.subscribe("session-abc"):
    if isinstance(msg.payload, TokenChunkEvent):
        print(msg.payload.params["delta"], end="")
```

- 返回 `AsyncIterator[GatewayMessage]`
- 每消费一条，`received_count += 1`
- 收到 `None` 哨兵时结束（Gateway 关闭触发）
- 消费者被 `CancelledError` 时正常退出

### 3.5 `on_event(handler)` — 全局监听

```python
async def audit_writer(msg: GatewayMessage):
    await write_audit_line(msg.to_jsonl())

gateway.on_event(audit_writer)
```

- `handler` 是 `async def (GatewayMessage) -> None`
- **异步执行**：`publish()` 内部用 `asyncio.create_task()` 调度，不阻塞主发布路径
- **故障隔离**：handler 崩溃只记录日志，不影响其他 handler 或 publish
- 可注册多个 handler，依次调度

### 3.6 `get_backpressure(session_id?)` — 背压查询

```python
# 查询单个会话
bp = gateway.get_backpressure("session-abc")  # → 0.35

# 查询所有会话中的最高值
bp = gateway.get_backpressure()  # → 0.65
```

| 返回值 | 含义 |
|--------|------|
| 0.0 | 队列为空，无积压 |
| 0.5 | 队列半满 |
| 1.0 | 队列全满 |

**公式**：`(send_count - received_count) / max_queue_size`

### 3.7 查询方法

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_event_history(limit?)` | `List[GatewayMessage]` | 最近 N 条完整消息，`limit=None` 返回全部 |
| `list_sessions()` | `Dict[str, float]` | 所有会话 ID → 背压比率 |
| `seq` (property) | `int` | 当前全局序列号 |
| `running` (property) | `bool` | 网关运行状态 |

---

## 4. 背压机制

**策略**：阻塞等待 + 超时兜底，而不是直接拒绝。

```
队列有空位 → put_nowait → 立即返回 True
队列满     → put() 阻塞，等待消费者取走消息
等待 > publish_timeout → 返回 False（调用方自行决定重试/降级/告警）
```

**为什么不是直接拒绝？**
- Harness 指南的 `BackpressureQueue` 也是阻塞等待
- Claude Code 的隐式背压也是 flow control 而非 reject
- 消息总线不应该丢消息 —— 丢消息的责任在上游（Orchestrator 决定是否降级），网关只负责诚实传递

**监控建议**：定期调用 `get_backpressure()`，超过 0.8 时告警或降级。

---

## 5. 事件历史

```python
# 完整消息（含 payload），上限可配
history = gateway.get_event_history(limit=20)
for msg in history:
    print(f"seq={msg.seq} session={msg.session_id} type={type(msg.payload).__name__}")
```

**使用场景**：

| 场景 | 用法 |
|------|------|
| 调试 | "为什么这个消息没收到？" → 查 `event_history` |
| 崩溃回放 | gateway 挂掉后从 `event_history` 重建状态 |
| 监控 | 定时快照 `len(history)` 作为吞吐量指标 |

**内存估算**：`TokenChunkEvent` 约 500B/条，500 条 ≈ 250KB，可忽略。

---

## 6. 典型使用场景

### 6.1 单会话简单流程

```python
gateway = ChaChaAsyncGateway()
await gateway.start()
gateway.register("s1")

await gateway.publish(
    RPCRequest(method="user/message", params={"content": "hello"}),
    session_id="s1",
)

async for msg in gateway.subscribe("s1"):
    print(msg.seq, msg.payload.method)
    break

await gateway.stop()
```

### 6.2 多会话隔离

```python
gateway.register("cli-session")
gateway.register("web-session")

# 两个生产者互不干扰
await gateway.publish(evt_a, session_id="cli-session")
await gateway.publish(evt_b, session_id="web-session")

# cli 消费者只收到 evt_a
async for msg in gateway.subscribe("cli-session"):
    assert msg.session_id == "cli-session"
    break
```

### 6.3 全局监听者（审计 + 遥测）

```python
async def audit_handler(msg: GatewayMessage):
    await append_audit_log(msg)

async def metrics_handler(msg: GatewayMessage):
    PROM_COUNTER.labels(type=type(msg.payload).__name__).inc()

gateway.on_event(audit_handler)
gateway.on_event(metrics_handler)

# 后续所有 publish 都会异步触发这两个 handler
await gateway.publish(event, session_id="s1")
```

### 6.4 背压感知的生产者

```python
# Orchestrator 发布前检查背压
if gateway.get_backpressure(session_id) > 0.8:
    # 暂停生成，等待消费者消化
    await asyncio.sleep(0.5)

ok = await gateway.publish(event, session_id=session_id)
if not ok:
    logger.warning("发布失败，会话 %s 背压过高", session_id)
```

---

## 7. 与其他模块的关系

| 模块 | 关系 |
|------|------|
| `protocol/rpc_schema.py` | Gateway 包装的消息类型全部来自此模块。`publish()` 接受任意 `RPCRequest | RPCResponse | RPCEvent` 子类 |
| `core/orchestrator.py` (阶段 2) | 生产者：发布流式 token、工具状态、权限请求；消费者：接收用户消息 |
| `core/telemetry.py` (阶段 2) | 全局监听者：统计消息吞吐、记录延迟分布 |
| `core/models/audit.py` | 全局监听者：将 `AuditTrailEvent` 写入 `audit.jsonl` |
| `interface/cli/` (阶段 7) | 消费者：`subscribe()` 接收消息，渲染到终端 |
| `interface/web/` (阶段 8) | 消费者：WebSocket 推送 |

---

## 8. 设计要点

1. **单例不强制**：Gateway 不设计单例模式（`ConfigManager` 是单例）。如需隔离，可创建多个实例。

2. **顺序不跨会话**：同一会话内消息严格按 `seq` 有序。不同会话间的顺序无保证（也不需要）。

3. **全局监听者异步化**：handler 用 `create_task` 而非 `await`，publish 不等待 handler 完成。这带来一个微妙语义：**handler 失败你不知道**。对于审计日志这种"尽力而为"的场景是可接受的。

4. **哨兵关闭优于强制终止**：`stop()` 发 `None` 让订阅者正常退出，而不是直接取消协程，保证消费者有清理机会。

5. **背压超时非异常**：`publish()` 超时返回 `False` 而非抛异常，调用方无需 try/except，用 `if not ok:` 即可。

6. **不解析 payload**：Gateway 只看 `GatewayMessage` 的 `seq`/`session_id`/`project_id` 做路由，不关心 `payload` 的具体类型，保持职责单一。
