# 沙箱执行器 (`capabilities/sandbox.py`)

`Sandbox` 继承 `BaseTool`，注册为工具名 `bash`，安全执行系统命令。

## 工具参数

```python
bash(command="ls -la")           # 默认 60s 超时
bash(command="pytest tests/ -v", timeout=120)  # 自定义超时
```

| 参数 | 说明 |
|------|------|
| `command` | 要执行的命令（必填） |
| `timeout` | 超时秒数（默认 60，硬上限 300） |

## 安全链

```
LLM 调用 bash 工具
  → ToolExecutor 查找
  → PolicyEngine 黑名单检查（如 rm -rf 拦截）
  → HookOrchestrator pre_tool_execution
  → Sandbox.execute()
    → subprocess.run(shell=True, timeout)
    → ANSI 清洗
    → 输出截断到 100K 字符
```

## 风险

`risk = "high"`，需要用户审批。超时后返回 `[错误] 命令超时（>Xs）`。

> ⚠️ **已知限制**：当前使用 `shell=True`，存在注入风险（详见 `docs/tool-production-audit.md`）。后续计划改为 `shell=False` + 进程组隔离。
