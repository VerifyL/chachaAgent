# 执行工具生产级别审查

日期: 2025-01-15 | 审查人: Reyn

---

## 总览

当前执行工具成熟度约 **60%**，核心功能可用但缺生产防护。

| 模块 | 文件 | 评级 |
|------|------|------|
| Sandbox | `capabilities/sandbox.py` | 🔴 |
| ToolExecutor | `core/tool_executor.py` | 🔴 |
| Dispatcher | `core/dispatcher.py` | 🟡 |
| PolicyEngine | `core/policy_engine.py` | 🟡 |
| AtomicWriter + CodePatcher | `capabilities/atomic_writer.py` `capabilities/builtins/code_patcher.py` | 🟡 |
| HttpTool | `capabilities/builtins/http_tool.py` | 🟡 |
| HookOrchestrator | `core/hook_orchestrator.py` | 🟡 |

---

## 1. Sandbox (`sandbox.py`)

### 🔴 阻塞级

- **`shell=True` — 最大安全漏洞。** 命令通过 shell 解析，`ls; rm -rf /` 直接执行。应改为 `shell=False` + 参数列表。
- **无进程组隔离。** 超时 kill 后孙进程变孤儿。应用 `preexec_fn=os.setsid` + `os.killpg`。
- **继承 Agent 全部环境变量。** `DATABASE_URL`、`AWS_SECRET` 直接暴露给子进程。

### 🟡 重要级

- 无资源限制（CPU/内存）。fork bomb 或内存泄漏直接拖垮 Agent。
- 无网络访问控制。子进程可任意访问内外网。
- 无文件系统写入控制。

### 🟢 优化级

- 无 stdin 支持
- 无工作目录参数

---

## 2. ToolExecutor (`tool_executor.py`)

### 🔴 阻塞级

- **审批链路断裂。** 审批被拦截后只返回 `ToolResult(status="blocked")`，Dispatcher 不处理——审批逻辑实际是空的。
- **重试只认 `asyncio.TimeoutError`。** 网络断开、连接拒绝等 `OSError` 直接 fail。

### 🟡 重要级

- `_HOOK_BYPASS_TOOLS` 硬编码在方法体内，无法配置。
- `execute_batch` 全部并发，1 个超时拖慢其他结果。
- 无幂等性标记——`edit_file` 重试可能重复写入。

### 🟢 优化级

- 审计日志未持久化
- 无调用频率限制

---

## 3. Dispatcher (`dispatcher.py`)

### 🟡 重要级

- **`_guess_tool_name` O(n²)。** 每轮遍历整个消息历史反查工具名。
- **API Key 清洗正则是局部定义**，每次工具调用都编译一次。
- `MAX_TOOL_ROUNDS=200` 硬上限，无基于时间/成本的动态熔断。
- 工具结果缓存文件清理只在会话结束时触发——Agent 崩溃则缓存堆积。

---

## 4. PolicyEngine (`policy_engine.py`)

### 🟡 重要级

- **黑名单是子串匹配。** `pattern in command` 太粗糙——`echo "rm -rf"` 也会被拦截。
- **审批缓存纯内存。** 服务重启全部丢失。
- **风险因子权重硬编码** `(0.3, 0.25, 0.2, 0.15, 0.1)`。
- `evaluate_tool` 中 `risk_factors` 默认全为 0，**风险评估实际从未生效**。

---

## 5. AtomicWriter + CodePatcher

### 🟡 重要级

- **无文件锁。** 两个 Agent 同时 edit 同一文件互相覆盖。
- **整个文件读入内存。** 编辑大文件会 OOM。
- 备份清理被动触发——长期运行可能磁盘堆积。

---

## 6. HttpTool

### 🟡 重要级

- 同步 `urllib`，阻塞事件循环。
- **无 SSRF 防护。** 未检查内网 IP（10.x、192.168.x、127.x）。
- 无重试、无连接池、无速率限制。

---

## 7. HookOrchestrator

### 🟡 重要级

- `_get_error_tolerance` **默认返回 True**（容错继续）。安全钩子崩溃会被静默跳过。
- 外部进程钩子每次启动新进程，高频开销大。

---

## 优先级路线图

| 优先级 | 改动 | 影响面 | 工作量 |
|--------|------|--------|--------|
| **P0** | Sandbox `shell=True` → `shell=False` + 进程组隔离 | 安全 | 中 |
| **P0** | 审批链路打通（ToolExecutor → Dispatcher → 用户交互） | 安全 | 大 |
| **P1** | 可重试异常分类（网络/超时 vs 业务错误） | 可靠性 | 小 |
| **P1** | PolicyEngine 风险评估默认值注入 | 安全 | 小 |
| **P1** | 文件锁（`fcntl.flock` / `portalocker`） | 数据安全 | 小 |
| **P2** | SSRF 防护（内网 IP 黑名单） | 安全 | 小 |
| **P2** | Sandbox 资源限制（`resource.setrlimit`） | 稳定性 | 中 |
| **P2** | Dispatcher `_guess_tool_name` 改为 O(1) 映射 | 性能 | 小 |
| **P2** | 审批缓存持久化 | 体验 | 中 |
| **P3** | HttpTool 换 `httpx` | 可靠性 | 中 |
| **P3** | 大文件流式处理 | 扩展性 | 大 |
| **P3** | 环境变量隔离 | 安全 | 小 |
