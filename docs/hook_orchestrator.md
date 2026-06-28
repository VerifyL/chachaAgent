# 钩子协调器 (`core/hook_orchestrator.py`)

本文档说明 `HookOrchestrator` 的注册、执行、容错机制和使用示例。钩子系统遵循 **"数据契约 + 执行引擎"** 分离架构：`core/models/hook.py` 定义数据结构，`core/hook_orchestrator.py` 驱动责任链。

## 概述

`HookOrchestrator` 是钩子系统的执行引擎，融合了 **外部进程钩子 + Harness Plugin Hook** 的设计理念：

- **双模式 handler**：内置 Python callable（开发用）+ `ShellCommand` 外部进程（用户自定义，风格）
- **安全优先容错**：可能返回 `BLOCK`/`MODIFY` 的钩子超时/崩溃 → **默认拒绝操作**；仅返回 `CONTINUE` 的钩子 → 容错继续
- **结果累积**：多个钩子的 `additional_context` 跨钩子拼接，`MODIFY` 链式覆盖参数
- **洋葱语义**：PRE 钩子按优先级正序执行，POST 钩子**倒序**执行（先注册的先解绑）

### 数据流

```
HookOrchestrator.run(hook_point, session_id, tool_call, ...)
 │
 ├─ 1. _select() → 筛选匹配 hook_point + matcher 的钩子
 │
 ├─ 2. 排序：PRE 正序（高 pri 先），POST 倒序（低 pri 先）
 │
 ├─ 3. 构建 HookContext (frozen)
 │
 └─ 4. 链式执行：
 for hook in candidates:
 ctx = HookContext(tool_call.arguments = 当前累积的 args)
 result = await _execute_hook(hook, ctx)
 if result.BLOCK → 立即返回，后续钩子不执行
 if result.MODIFY → 更新 current_tool_args（链式覆盖）
 if result.additional_context → 拼接到 accumulated_context
 if result.STOP → 停止链
 if result.CONTINUE → 继续下一个钩子
```

---

## 1. 注册钩子

### 1.1 `register()` — 注册内置钩子

```python
from core.hook_orchestrator import HookOrchestrator
from core.models.hook import HookPoint, HookMatcher, HookResult, HookContext

orchestrator = HookOrchestrator()

# 最简单：全局匹配（所有工具）
orchestrator.register(
 name="audit-log",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=my_audit_handler, # async def (HookContext) → HookResult
)
```

### 1.2 带匹配器的注册

```python
# 只匹配包含 "rm" 的危险命令
orchestrator.register(
 name="danger-safety",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=safety_check,
 matcher=HookMatcher(type="command", pattern="rm"),
 priority=10, # 高优先级，最先执行
 timeout=3.0, # 3 秒超时
)
```

### 1.3 注册外部进程钩子

```python
from core.hook_orchestrator import ShellCommand

orchestrator.register(
 name="custom-audit",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=ShellCommand(
 command="python .chacha/hooks/audit.py",
 timeout=10.0,
 env={"MY_VAR": "value"}, # 额外环境变量
 ),
)
```

外部进程通过 stdin 接收 `HookContext` JSON，stdout 返回 `HookResult` JSON：

```python
# .chacha/hooks/audit.py
import json, sys

ctx = json.loads(sys.stdin.read())
tool_name = ctx["tool_call"]["tool_name"]

result = {
 "action": "continue",
 "additional_context": f"审计: 调用工具 {tool_name}",
}
print(json.dumps(result))
```

额外注入的环境变量：

| 变量 | 值 |
|------|-----|
| `CHACHA_SESSION_ID` | 当前会话 ID |
| `CHACHA_PROJECT_ID` | 当前项目 ID |
| `CHACHA_HOOK_POINT` | 挂载点（如 `pre_tool_execution`） |

---

## 2. 执行钩子链

### 2.1 `run()` — 执行责任链

```python
from core.models.hook import ToolCallContext

tc = ToolCallContext(
 tool_name="shell",
 tool_use_id="call-1",
 command_or_action="rm -rf /tmp/test",
 arguments={"cmd": "rm -rf /tmp/test"},
)

result = await orchestrator.run(
 session_id="s1",
 project_id="p1",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 tool_call=tc,
)

if result.is_blocked():
 print(f"操作被拦截: {result.message}")
 return
```

### 2.2 POST 钩子倒序执行

```python
# 注册顺序：pre-check(pri=3), pre-audit(pri=1), post-cleanup(pri=0), post-report(pri=2)

# PRE 执行顺序：pre-check → pre-audit （正序）
# POST 执行顺序：post-report → post-cleanup （倒序）
```

倒序的原因是中间件语义：后注册的钩子先被拆掉。

---

## 3. 容错机制

### 3.1 自动推断

| handler 类型 | 超时/崩溃 | 原因 |
|-------------|----------|------|
| Python callable | **容错继续** | 默认可恢复 |
| `ShellCommand` 外部进程 | **默认拒绝** | 外部进程行为不确定，保守处理 |

### 3.2 显式覆盖

```python
orchestrator.register(
 name="critical-safety",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=safety_check,
 on_timeout_continue=False, # 超时也拒绝，不放过
 on_error_continue=False, # 崩溃也拒绝
)

orchestrator.register(
 name="non-critical-log",
 hook_point=HookPoint.POST_TOOL_EXECUTION,
 handler=write_log,
 on_timeout_continue=True, # 超时也继续
 on_error_continue=True, # 崩溃也继续
)
```

### 3.3 安全钩子识别

如果一个钩子**可能返回 BLOCK 或 MODIFY**（安全检查、参数校验），建议显式设置 `on_timeout_continue=False`，防止超时后危险操作被放行：

```python
async def safety_check(ctx: HookContext) -> HookResult:
 if "rm" in (ctx.tool_call.command_or_action or ""):
 return HookResult.block("networking not allowed")
 return HookResult.continue_()

orchestrator.register(
 name="safety",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=safety_check,
 on_timeout_continue=False, # ← 关键：挂了就拒绝
 on_error_continue=False,
)
```

---

## 4. 内置钩子示例

### 4.1 安全检查钩子

```python
async def builtin_security_check(ctx: HookContext) -> HookResult:
 """拦截黑名单命令"""
 if ctx.tool_call is None:
 return HookResult.continue_()

 blocked = ["rm -rf", "sudo", "mkfs", "dd", "chmod 777"]
 cmd = ctx.tool_call.command_or_action or ""

 for pattern in blocked:
 if pattern in cmd:
 return HookResult.block(
 message=f"命令 '{pattern}' 命中黑名单",
 additional_context=f"⚠️ 危险命令已被阻止: {cmd}",
 )
 return HookResult.continue_()
```

### 4.2 成本控制钩子

```python
async def builtin_cost_check(ctx: HookContext) -> HookResult:
 """检查累计成本是否超限"""
 # 从 ctx.metadata 读取累计成本（由 Orchestrator 注入）
 cumulative = ctx.metadata.get("cumulative_cost_usd", 0)
 limit = ctx.metadata.get("cost_limit_usd", 10.0)

 if cumulative > limit:
 return HookResult.block(
 message=f"成本超限 ({cumulative:.2f} > {limit:.2f})",
 additional_context="📊 会话累计成本已超过上限，后续 LLM 调用将被阻止",
 )
 return HookResult.continue_()
```

### 4.3 上下文压缩钩子

```python
async def builtin_compression_hook(ctx: HookContext) -> HookResult:
 """上下文超限时建议压缩"""
 if ctx.llm_request is None:
 return HookResult.continue_()

 est_input = ctx.llm_request.estimated_input_tokens
 budget = ctx.metadata.get("budget_per_request", 128000)

 if est_input > budget * 0.8:
 return HookResult.continue_(
 additional_context="📊 输入 token 已达 80% 窗口，建议在此之前执行上下文压缩",
 )
 return HookResult.continue_()
```

### 4.4 参数修正钩子

```python
async def builtin_path_sanitizer(ctx: HookContext) -> HookResult:
 """修正路径参数，防止跨目录访问"""
 if ctx.tool_call is None:
 return HookResult.continue_()

 path = ctx.tool_call.arguments.get("path", "")
 if ".." in path or not path.startswith("/tmp/"):
 # 修正到安全路径
 safe_path = "/tmp/sandbox/" + path.replace("..", "").lstrip("/")
 return HookResult.modify(
 modified_tool_args={"path": safe_path},
 message=f"路径已修正: {path} → {safe_path}",
 additional_context="🔧 文件路径已被自动修正为沙箱内路径",
 )
 return HookResult.continue_()
```

---

## 5. 自定义钩子示例

### 5.1 项目级审计钩子

```python
# .chacha/hooks/project_audit.py
import json, sys

def main():
 ctx = json.loads(sys.stdin.read())
 tool = ctx.get("tool_call", {})

 with open(".chacha_agent/logs/project_audit.jsonl", "a") as f:
 f.write(json.dumps({
 "ts": ctx.get("timestamp", ""),
 "tool": tool.get("tool_name"),
 "session": ctx.get("session_id"),
 }) + "\n")

 print(json.dumps({"action": "continue", "message": "audit logged"}))

if __name__ == "__main__":
 main()
```

注册：

```python
orchestrator.register(
 name="project-audit",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=ShellCommand(
 command="python .chacha/hooks/project_audit.py",
 timeout=5.0,
 ),
 matcher=HookMatcher(type="always"),
)
```

### 5.2 通知钩子（Slack / Webhook）

```python
import aiohttp
from core.hook_orchestrator import HookOrchestrator

async def slack_notify(ctx: HookContext) -> HookResult:
 url = ctx.metadata.get("slack_webhook")
 if not url:
 return HookResult.continue_()

 msg = {
 "text": f"🔧 ChachaAgent: {ctx.tool_call.tool_name} 执行中\n"
 f"Session: {ctx.session_id}"
 }
 async with aiohttp.ClientSession() as s:
 await s.post(url, json=msg)

 return HookResult.continue_()

orchestrator.register(
 name="slack-notify",
 hook_point=HookPoint.PRE_TOOL_EXECUTION,
 handler=slack_notify,
 matcher=HookMatcher(type="command", pattern="git|pytest|deploy"),
)
```

---

## 6. 与 Orchestrator 的集成点

钩子系统在编排流程中的介入点：

```
Orchestrator 主循环
 │
 ├─ 用户消息 → hook_orchestrator.run(PRE_CONTEXT_ASSEMBLY)
 │ 记忆注入、规则加载
 │
 ├─ 组装上下文 → hook_orchestrator.run(POST_CONTEXT_ASSEMBLY)
 │ 验证上下文大小、触发压缩
 │
 ├─ LLM 调用前 → hook_orchestrator.run(PRE_LLM_CALL)
 │ 成本检查、提示词注入
 │
 ├─ LLM 调用后 → hook_orchestrator.run(POST_LLM_CALL)
 │ 响应过滤、tool_calls 校验
 │
 ├─ 工具执行前 → hook_orchestrator.run(PRE_TOOL_EXECUTION)
 │ 安全检查、参数修正、审批拦截
 │
 ├─ 工具执行后 → hook_orchestrator.run(POST_TOOL_EXECUTION)
 │ 结果校验、输出脱敏、审计
 │
 └─ 异常发生时 → hook_orchestrator.run(ON_ERROR)
 错误恢复、通知推送
```
