# 上下文组装结果模型 (`core/models/context.py`) (v2.0)

本文档详细说明上下文组装结果模型中所有数据结构的字段含义、类型、读写角色、生命周期及设计考量。

## v2.0 新增来源

| 来源 | 对应内容 | zone 默认值 |
|------|----------|------------|
| `MEMORY_INDEX` | MEMORY.md 轻量索引（autoDream 产物） | `dynamic` |
| `SESSION_MEMORY` | 今日会话记忆（session/{date}.md） | `dynamic` |
| `TOOL_CACHE_PLACEHOLDER` | JSON 占位符工具结果 | `dynamic` |

## 设计理念

融合 Claude Code + Harness 工程指南的上下文管理最佳实践：

- **Claude Code 压缩骨架**：7 层渐进式压缩（FROZEN → TRIMMED → SUMMARIZED → CONSOLIDATED）
- **DYNAMIC_BOUNDARY**：保护区（system_prompt + CHACHA.md + CHACHA_MEMORY.md + skills）永不被截断
- **v2.0 永久记忆**：CHACHA_MEMORY.md 在保护区，永不压缩、永不删除
- **v2.0 两阶段工具缓存**：Stage 1 Dispatcher → Stage 2 Compressor

---

## 1. 来源分类：`BlockSource`

```python
class BlockSource(str, Enum):
    SYSTEM_PROMPT = "system_prompt"            # 系统提示词
    STATIC_RULE = "static_rule"               # CHACHA.md 静态规范
    MEMORY = "memory"                         # MEMORY.md 记忆 / CHACHA_MEMORY.md 永久记忆
    HISTORY = "history"                       # 对话历史
    TOOL_RESULT = "tool_result"               # 工具执行结果
    SKILL = "skill"                           # 技能定义
    ADDITIONAL_CONTEXT = "additional_context"  # 钩子注入
```

| 来源 | 对应内容 | zone 默认值 | 可缓存 |
|------|----------|------------|--------|
| `SYSTEM_PROMPT` | 系统级指令 | `protected` | ✅ TTL 600s |
| `STATIC_RULE` | 分层 CHACHA.md | `protected` | ✅ TTL 600s |
| `MEMORY` (permanent) | CHACHA_MEMORY.md 永久记忆（≤100条） | `protected` | ✅ TTL 300s |
| `SKILL` | 技能定义、工具 schema | `protected` | ✅ TTL 1200s |
| `MEMORY` (index) | MEMORY.md 轻量索引 | `dynamic` | ❌ |
| `MEMORY` (session) | 今日会话记忆 | `dynamic` | ❌ |
| `HISTORY` | 用户消息、助手回复 | `dynamic` | ❌ |
| `TOOL_RESULT` | 工具执行输出（可能为 JSON 占位符） | `dynamic` | ❌ |
| `RAG_RESULT` | Code-RAG 检索结果 | `dynamic` | ✅ TTL 120s |
| `SUBAGENT_RESULT` | 子Agent 任务完成报告 | `dynamic` | ❌ |
| `ADDITIONAL_CONTEXT` | 钩子注入 | `dynamic` | ❌ |

---

## 2. 渐进式压缩层级：`CompressionLevel`

| 层级 | 触发 pressure | LLM 成本 | 做法 | 典型压缩率 |
|------|-------------|----------|------|-----------|
| `NONE` | < 0.5 | — | 原始内容 | 0% |
| `FROZEN` | 0.5~0.7 | 零 | Stage 2: JSON key 最小化 + 150字符摘要 | 90%+ |
| `TRIMMED` | 0.7~0.85 | 零 | 历史消息首尾裁剪 | 30-60% |
| `SUMMARIZED` | 0.85~0.95 | 一次 | LLM 摘要保留语义 | 70-90% |
| `CONSOLIDATED` | > 0.95 | 一次 | 多条相关块合并为一条 | 85%+ |

---

## 3. v2.0 上下文字段顺序

```
protected zone:
  0. SYSTEM_PROMPT
  1. CHACHA.md (STATIC_RULE)
  2. CHACHA_MEMORY.md (MEMORY, 永久记忆)
  3. SKILL

dynamic zone:
  10. MEMORY.md 索引
  11. 今日会话记忆
  20+. 对话历史
  30+. 工具结果（含 JSON 占位符）
  40+. RAG / SubAgent
  50+. 钩子注入
```

---

## 4. 典型压缩决策流程

```
ContextManager 产出 AssembledContext(blocks=25, total_tokens=120000)
        │
        ▼
读取 meta.utilization_ratio → 0.94（>0.8 触发线）
        │
        ├─ needs_compression = True
        ├─ recommended_level = TRIMMED
        │
        ▼
ContextCompressor 按策略执行：
        │
        ├─ 1. 保护区 BLOCK 跳过
        ├─ 2. Stage 2 FROZEN: JSON 占位符 → {"t","s","p"} 最小化
        ├─ 3. Stage 2 FROZEN: 完整结果 → 150字符摘要
        ├─ 4. TRIMMED: 旧历史消息裁剪
        └─ 5. total_tokens 回到安全区
```
