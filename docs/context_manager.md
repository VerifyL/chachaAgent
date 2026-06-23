# 上下文组装策略 (`core/context_manager.py`) (v3.0)

本文档说明 `ContextManager` 的上下文组装流程、DYNAMIC_BOUNDARY 策略、Token 预算控制和压缩触发。

## v3.0 上下文字段顺序

```
┌─────────────────────────────────────────┐
│  protected zone（永不压缩，固定顺序）      │
├─────────────────────────────────────────┤
│  1. SYSTEM_PROMPT（已合并 CHACHA.md）    │
│  2. USER_MEMORY.md（用户级永久记忆）       │
│  3. CHACHA_MEMORY.md（项目永久记忆）       │
│  4. SKILLS / tool schemas               │
├─────────────────────────────────────────┤
│  dynamic zone（可压缩，稳定度降序）        │
├─────────────────────────────────────────┤
│  5. MEMORY.md（DreamPipeline 轻量索引）    │
│  6. 对话历史（最近 N 轮）                 │
│  7. 工具结果（完整 + 缓存占位符）          │
│  8. RAG / SubAgent / 钩子注入            │
└─────────────────────────────────────────┘
```

## 数据流

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
| `global_permanent_memory` | 2 | protected | USER_MEMORY.md 用户级记忆，缓存 300s |
| `permanent_memory` | 3 | protected | CHACHA_MEMORY.md 项目记忆，缓存 300s |
| `skill` | 4 | protected | 技能定义，缓存 1200s |
| `memory_index` | 10 | dynamic | MEMORY.md 记忆索引 |
| `session_memory` | 11 | dynamic | 今日会话记忆 |
| `history` | 20+ | dynamic | 用户消息 + 助手回复 |
| `tool_result` | 30+ | dynamic | 工具执行输出 |

### 1.2 注入接口

```python
mgr = ContextManager()
mgr.set_system_prompt("你是助手")
mgr.set_static_rules("CHACHA.md 内容")              # 宪法
mgr.set_global_permanent_memory("USER_MEMORY.md")   # 用户级永久记忆（跨项目）
mgr.set_permanent_memory("永久记忆内容")             # 项目永久记忆
mgr.set_memory_index("MEMORY.md 索引")              # 动态区
mgr.set_session_memory("今日会话记忆")               # 动态区
```

### 1.3 静态块缓存

```python
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
protected_tokens = sum(protected zone blocks)
utilization_ratio = total_tokens / budget_per_request
needs_compression = utilization_ratio > compression_trigger_ratio
compression_pressure = min(1.0, utilization_ratio * 1.25)
```

### 2.2 压缩层级推荐

| pressure | 推荐层级 | 说明 |
|----------|---------|------|
| < 0.5 | NONE | 无需压缩 |
| 0.5~0.7 | FROZEN | Stage 2 激进冻结（JSON key 最小化） |
| 0.7~0.85 | TRIMMED | 历史消息首尾裁剪 |
| 0.85~0.95 | SUMMARIZED | LLM 摘要 |
| > 0.95 | CONSOLIDATED | 记忆整合 |

---

## 3. 两阶段工具结果缓存

### Stage 1: Dispatcher（宽松）

- 保留最近 10 个完整工具结果
- 更早的结果 → `{"toolname":"x","result_summary":"x","cache_path":"x"}`
- 缓存到 `session/{session_id}/tool_cache/`

### Stage 2: ContextCompressor FROZEN（激进）

- JSON 占位符 → `{"t":"x","s":"x","p":"x"}`
- 完整结果 → 150 字符摘要 + 缓存
- protected zone 跳过

---

## 4. 消息格式输出

```python
ctx = mgr.assemble(state, session_id="s1")
messages = ctx.get_messages()
# [{"role": "system", "content": "You are ChaChaAgent..."},
#  {"role": "system", "content": "[Permanent Memory]\n..."},
#  {"role": "user", "content": "读一下 main.py"},
#  {"role": "assistant", "content": "正在读取..."},
#  {"role": "tool", "content": "print('hello')"}]
```

---

## 5. 阶段 4 升级路径

> TODO(阶段4): 以下功能在阶段 4 接入完整上下文子系统后实现。

| 当前（v2.0） | 阶段 4 |
|---------------|--------|
| `_estimate_tokens()` 粗略估算 | `TokenCounter` 精确 tiktoken |
| 直接从 `ConversationState` 转换 | `ContextAssembler` 三阶段组装 |
| `needs_compression` 仅标记 | `ContextCompressor` 实际压缩执行 |
| `static_rules` 外部传入 | `StaticRuleLoader` 自动分层加载 |
| MemoryManager 注入 | 自动加载 + Auto Dream |
| 缓存仅内存 TTL | 缓存写入 pkl 文件 |
