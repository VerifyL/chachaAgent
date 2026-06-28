# 子Agent 系统

`core/subagent/` sub-agent 设计，实现 LLM 自主委托子Agent 执行独立任务。

## 架构

```
LLM 调用 subagent(type="explore", task="梳理项目架构")
 → SubAgentTool.execute()
 → SubAgentSpawner.spawn()
 ├─ pre_subagent_spawn hook
 ├─ 新建 ContextManager（独立上下文）
 ├─ 新建 ToolExecutor（白名单过滤）
 ├─ Dispatcher.dispatch() ← LLM + 工具循环
 ├─ post_subagent_spawn hook
 └─ → SubAgentResult
```

## 三种内置子Agent

| 类型 | 用途 | 工具 | max_iter | skip_chacha_md |
|------|------|------|----------|----------------|
| `explore` | 代码库搜索 | read, grep, glob | 15 | ✅ |
| `plan` | 规划设计 | read, grep, glob, memory | 10 | ❌ |
| `worker` | 执行修改 | read, grep, glob, write, edit, bash | 10 | ❌ |

## LLM 自决

LLM 根据子Agent 的 `description` 字段自动判断是否委托：

```python
subagent(type="explore", task="找到所有循环依赖")
subagent(type="worker", task="重构 auth.py 拆分为 auth.py + tokens.py")
```

**我们不写代码判断**——LLM 看到任务和子Agent 描述，自己决定。

## 使用

```python
from core.subagent.spawner import SubAgentSpawner
from core.tool_executor import ToolExecutor
from capabilities.builtins.task_tool import TaskTool as SubAgentTool

# 主工具列表
parent_tools = ToolExecutor(tools=[read_tool, grep_tool, edit_tool])

# 孵化器
spawner = SubAgentSpawner(llm_invoker, parent_tools, hook_orchestrator=hooks)

# 注册为工具
tools = ToolExecutor(tools=[
 read_tool, grep_tool, edit_tool,
 SubAgentTool(spawner=spawner),
])

# LLM 调用
result = await spawner.spawn("explore", "梳理项目架构")
```

## Hook 支持

| Hook 点 | 时机 |
|---------|------|
| `pre_subagent_spawn` | 子Agent 创建后、执行前 |
| `post_subagent_spawn` | 子Agent 执行完成后 |

```python
from core.models.hook import HookPoint

orchestrator.register("cost-limit", HookPoint.PRE_SUBAGENT_SPAWN, async_handler)
```

## 安全

- 工具白名单：每个子Agent 类型只能使用指定工具
- `skip_chacha_md`：explore 不加载 CHACHA.md（避免污染上下文）
- 硬超时 300s（可配置）
- max_iterations 限制循环次数
