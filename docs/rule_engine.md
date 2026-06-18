# 声明式规则引擎 (`core/rule_engine.py`)

本文档说明如何通过 YAML 文件声明钩子规则，无需编写 Python 代码。

## 概述

`RuleEngine` 将 YAML 规则文件转换为 `HookOrchestrator.register()` 调用，支持三种 handler：

| handler | 示例 | 说明 |
|---------|------|------|
| `builtins.xxx` | `builtins.security_check` | 内置 Python 函数（阶段 5 补充实现） |
| `command:xxx` | `command:python .chacha/hooks/audit.py` | 外部进程（Claude Code 风格） |
| `python:xxx` | `python:my_hooks.my_func` | 自定义 Python 函数（阶段 5 动态导入） |

---

## 1. 规则文件格式

```yaml
# .chacha/rules/security.yaml
rules:
  - id: block-dangerous
    hook_point: pre_tool_execution
    handler: builtins.security_check
    matcher:
      type: command
      pattern: "rm|sudo|mkfs"
    priority: 10
    timeout: 3.0

  - id: audit-all
    hook_point: post_tool_execution
    handler: command:python .chacha/hooks/audit.py
    matcher:
      type: always
    priority: 1
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | ✅ | 规则唯一标识 |
| `hook_point` | ✅ | 挂载点（pre_tool_execution 等） |
| `handler` | ✅ | 处理器（见上表） |
| `matcher` | ❌ | 匹配器，默认 `{type: always}` |
| `priority` | ❌ | 优先级，越大越先执行，默认 0 |
| `timeout` | ❌ | 超时秒，默认 10 |

---

## 2. 加载与注册

```python
from core.rule_engine import RuleEngine
from core.hook_orchestrator import HookOrchestrator

engine = RuleEngine()
engine.load_dir(Path(".chacha/rules"))  # 加载所有 *.yaml/*.yml
print(engine.loaded_count)              # → 2

# 检测冲突
warnings = engine.validate()
for w in warnings:
    print(w)

# 注册到钩子系统
orch = HookOrchestrator()
engine.register_all(orch)
```

---

## 3. 冲突检测

两条规则 share 相同的 `hook_point` + `priority` 时触发警告：

```yaml
# 冲突示例
- id: rule-a
  hook_point: pre_tool_execution
  priority: 5

- id: rule-b
  hook_point: pre_tool_execution
  priority: 5   # ← 与 rule-a 冲突

# validate() → ["冲突: hook_point='pre_tool_execution' priority=5 有 2 个规则"]
```

---

## 4. 目录结构建议

```
.chacha/
  rules/
    security.yaml     # 安全规则
    audit.yaml        # 审计规则
    custom.yaml       # 自定义规则
  hooks/
    audit.py          # 外部钩子脚本
    notify.sh         # 外部钩子脚本
```
