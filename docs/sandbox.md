# 沙箱执行器 (`capabilities/sandbox.py`)

`Sandbox` 继承 `BaseTool`，安全执行命令。

## 安全链

```
LLM 调用 bash 工具
  → ToolExecutor 查找
  → PolicyEngine 黑名单检查（如 rm -rf 拦截）
  → HookOrchestrator pre_tool_execution
  → Sandbox.execute()
    → subprocess.run(shell=True, timeout=60)
    → ANSI 清洗
    → 输出截断到 100K 字符
```

## 使用

```python
from capabilities.sandbox import Sandbox

sandbox = Sandbox()
result = await sandbox.execute(command="ls -la")
result = await sandbox.execute(command="pytest tests/ -v", timeout=120)
```

## 配置

```toml
[sandbox]
allowed_commands = ["ls", "cat", "grep", "python", "pytest", "git"]
max_timeout = 300       # 硬上限（秒）
max_output_chars = 100000
```
