# 上下文组装策略 (`core/context_manager.py`)

本文档说明 `ContextManager` 的上下文组装流程、DYNAMIC_BOUNDARY 策略、Token 预算控制和压缩触发。ContextManager 位于 Orchestrator 与 LLMInvoker 之间，将 `ConversationState` 转换为 `AssembledContext`。

> **当前版本**：阶段 2 实现从 `ConversationState` 的基本组装。阶段 4 将接入 `ContextAssembler`（记忆/RAG 搜索）和 `ContextCompressor`（实际压缩）。

## 概述

设计融合了 **Claude Code DYNAMIC_BOUNDARY**（保护区永不被截断）和 **Harness 三阶段组装**（需求分析→检索→排序）：

- **DYNAMIC_BOUNDARY**：protected 区（系统提示 + CHACHA.md + 技能定义）约占 7% 窗口，永不被压缩
- **Token 预算检查**：`total / max_tokens > trigger_ratio` → `needs_compression=True`
- **缓存**：静态块（system_prompt / static_rule / skill）带 TTL 缓存，不必每轮重建
- **来源分布**：`blocks_by_source` 统计各来源 token 占比，优化决策依据

### 数据流

```
ConversationState  ──→  ContextManager.assemble()  ──→  AssembledContext  ──→  LLMInvoker
      │                         │                          │
  对话历史                   1. 事件→ContextBlock         .get_messages()
  工具结果                   2. Token 统计               .needs_compression
                             3. 压缩判定                  .recommended_level
```

---

## 1. 上下文组装

### 1.1 来源 → 优先级 → zone

| 来源 | priority | zone | 说明 |
|------|----------|------|------|
| `system_prompt` | 0 | protected | 核心指令，缓存 600s |
| `static_rule` | 1 | protected | CHACHA.md 规范，缓存 600s |
| `skill` | 1 | protected | 技能定义，缓存 1200s |
| `memory` | 2 | dynamic | MEMORY.md 记忆内容 |
| `history` | 3 | dynamic | 用户消息 + 助手回复 |
| `tool_result` | 4 | dynamic | 工具执行输出 |

**排序规则**：按 `priority` 升序，同 priority 按 `importance_score` 降序。

### 1.2 静态块缓存

```python
mgr = ContextManager()
# 第一次 → 计算 token_count，存入缓存
ctx1 = mgr.assemble(state, static_rules="规范A")

# 第二次（TTL 内 + 内容相同）→ 直接复用缓存块
ctx2 = mgr.assemble(state, static_rules="规范A")

# 内容变更 → 缓存失效，重新创建
ctx3 = mgr.assemble(state, static_rules="规范B")
```

---

## 2. Token 预算与压缩

### 2.1 计算公式

```
total_tokens = sum(block.token_count for all blocks)
utilization_ratio = total_tokens / budget_per_request
needs_compression = utilization_ratio > compression_trigger_ratio
compression_pressure = min(1.0, utilization_ratio * 1.25)
```

### 2.2 压缩层级推荐

| pressure | 推荐层级 | 说明 |
|----------|---------|------|
| < 0.5 | NONE | 无需压缩 |
| 0.5~0.7 | FROZEN | 冻结工具输出（保留关键行） |
| 0.7~0.85 | TRIMMED | 规则修剪（去空行、格式符） |
| 0.85~0.95 | SUMMARIZED | LLM 摘要 |
| > 0.95 | CONSOLIDATED | 记忆整合 |

### 2.3 示例

```python
from core.models.config import ContextConfig

cfg = ContextConfig(max_tokens=128000, compression_trigger_ratio=0.8)
mgr = ContextManager(cfg)

ctx = mgr.assemble(state, session_id="s1")
print(f"tokens: {ctx.meta.total_tokens}/{cfg.max_tokens}")
print(f"utilization: {ctx.meta.utilization_ratio:.1%}")
print(f"needs_compression: {ctx.needs_compression}")
print(f"recommended: {ctx.recommended_level.value}")
```

---

## 3. 消息格式输出

```python
ctx = mgr.assemble(state, session_id="s1")
messages = ctx.get_messages()
# [{"role": "system", "content": "You are ChaChaAgent..."},
#  {"role": "user", "content": "读一下 main.py"},
#  {"role": "assistant", "content": "正在读取..."},
#  {"role": "tool", "content": "print('hello')"}]
```

---

## 4. 阶段 4 升级路径

> TODO(阶段4): 以下功能在阶段 4 接入完整上下文子系统后实现。

| 当前（阶段 2） | 阶段 4 |
|---------------|--------|
| `_estimate_tokens()` 粗略估算 | `TokenCounter` 精确 tiktoken |
| 直接从 `ConversationState` 转换 | `ContextAssembler` 三阶段组装 |
| `needs_compression` 仅标记 | `ContextCompressor` 实际压缩执行 |
| `static_rules` 外部传入 | `StaticRuleLoader` 自动分层加载 |
| `memory_content` 外部传入 | `MemoryManager` 自动加载 + Auto Dream |
| 缓存仅内存 TTL | 缓存写入 pkl 文件 |
