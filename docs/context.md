# 上下文组装结果模型 (`core/models/context.py`)

本文档详细说明上下文组装结果模型中所有数据结构的字段含义、类型、读写角色、生命周期及设计考量。该模块定义了 `ContextAssembler` 的输出 —— `AssembledContext`，是 `ContextManager` 做压缩决策和 `LLMInvoker` 格式化调用的数据契约。

## 设计理念

融合 Claude Code + Harness 工程指南的上下文管理最佳实践：

- **Claude Code 压缩骨架**：7 层渐进式压缩（FROZEN → TRIMMED → SUMMARIZED → CONSOLIDATED），通过 `compression_pressure` 显式化，可观测、可调参
- **DYNAMIC_BOUNDARY**：保护区（~7% 窗口）永不被截断，动态区按 `importance_score` 排序压缩
- **三级预算**：`per_request` / `per_task` / reasoning 分离追踪
- **缓存感知**：静态块（系统提示、工具定义）带 `cache_ttl`，减少重复计算

核心原则引用自 Harness 工程指南："表观的模型质量，本质上是上下文质量"。

---

## 1. 来源分类：`BlockSource`

```python
class BlockSource(str, Enum):
    SYSTEM_PROMPT = "system_prompt"            # 系统提示词
    STATIC_RULE = "static_rule"               # CHACHA.md 静态规范
    MEMORY = "memory"                         # MEMORY.md 记忆
    HISTORY = "history"                       # 对话历史
    TOOL_RESULT = "tool_result"               # 工具执行结果
    ADDITIONAL_CONTEXT = "additional_context"  # 钩子注入
```

| 来源 | 对应内容 | zone 默认值 | 可缓存 |
|------|----------|------------|--------|
| `SYSTEM_PROMPT` | 系统级指令（模块化组装） | `protected` | ✅ TTL 600s |
| `STATIC_RULE` | 分层 CHACHA.md（~/.chacha/ → 项目/ → 子目录/） | `protected` | ✅ TTL 600s |
| `SKILL` | 技能定义（内置技能、OpenClaw 技能、工具 schema、领域知识） | `protected` | ✅ TTL 1200s |
| `MEMORY` | MEMORY.md 核心记忆（启动预注入）+ 主题文件（按需加载） | `dynamic` | ❌ |
| `HISTORY` | 用户消息、助手回复 | `dynamic` | ❌ |
| `TOOL_RESULT` | 工具执行输出（文件读、命令执行等，可能已冻结/修剪） | `dynamic` | ❌ |
| `RAG_RESULT` | Code-RAG 检索结果（LanceDB 语义搜索 + Tree-sitter 符号图） | `dynamic` | ✅ TTL 120s |
| `SUBAGENT_RESULT` | 子Agent 任务完成汇总报告 | `dynamic` | ❌ |
| `ADDITIONAL_CONTEXT` | 钩子注入的系统提示/警告 | `dynamic` | ❌ |

---

## 2. 渐进式压缩层级：`CompressionLevel`

```python
class CompressionLevel(str, Enum):
    NONE = "none"                # 未压缩
    FROZEN = "frozen"            # 冻结工具输出（零 LLM 成本）
    TRIMMED = "trimmed"          # 规则引擎修剪（零 LLM 成本）
    SUMMARIZED = "summarized"    # LLM 语义摘要
    CONSOLIDATED = "consolidated" # 多块整合为一条
```

| 层级 | 触发 pressure | LLM 成本 | 做法 | 典型压缩率 |
|------|-------------|----------|------|-----------|
| `NONE` | < 0.5 | — | 原始内容 | 0% |
| `FROZEN` | 0.5~0.7 | 零 | 工具输出只保留首尾各 2 行 + 退出码/行数/耗时 | 90%+ |
| `TRIMMED` | 0.7~0.85 | 零 | 去空行、去格式符、截断超长内容 | 30-60% |
| `SUMMARIZED` | 0.85~0.95 | 一次 | LLM 摘要保留语义，丢弃细节 | 70-90% |
| `CONSOLIDATED` | > 0.95 | 一次 | 多条相关块合并为一条（记忆整合用） | 85%+ |

---

## 3. 压缩触发原因：`TriggerReason`

```python
class TriggerReason(str, Enum):
    NONE = "none"                  # 未触发
    THRESHOLD = "threshold"        # utilization_ratio 超限
    TIME_GATE = "time_gate"        # 时间门：距上次压缩 > 24h
    SESSION_GATE = "session_gate"  # 会话门：累计 5 会话未压缩
    MANUAL = "manual"              # 用户/钩子显式触发
```

**用途**：标记触发压缩的原因，便于决策追踪和后续优化。

---

## 4. 上下文块：`ContextBlock`

```python
class ContextBlock(BaseModel):
    id: str                    # UUID4
    source: BlockSource        # 来源分类
    role: str                  # LLM 角色
    content: str               # 文本内容
    zone: "protected"|"dynamic" # 分区
    priority: int              # 排序优先级
    compression_level: CompressionLevel
    original_token_count: Optional[int]
    token_count: int
    persisted_path: Optional[str]      # 原文存档路径
    frozen_kept_lines: Optional[int]   # 冻结保留行数
    frozen_total_lines: Optional[int]  # 冻结原始行数
    compression_history: List[str]     # 压缩轨迹
    content_hash: Optional[str]        # SHA256
    importance_score: float    # 0~1
    cache_ttl: Optional[int]   # 秒
    created_at: datetime       # UTC
```

**用途**：上下文的最小单元。由 `ContextAssembler` 从各来源收集创建（`NONE` 状态），经 `ContextCompressor` 渐进式压缩，最终由 `get_messages()` 转为 LLM API 格式。

| 字段 | 说明 |
|------|------|
| `source` | 来源分类，用于审计和统计分析 |
| `role` | LLM 消息角色（system/user/assistant/tool），与 API 格式对齐 |
| `content` | 文本内容，随压缩层级加深逐步精简 |
| `zone` | `protected`=系统提示+当前任务+规则，永不被压缩；`dynamic`=其余内容，按重要性压缩 |
| `priority` | 组装排序优先级（0 最高），同 priority 按 `importance_score` 排序 |
| `compression_level` | 当前压缩层级，轨迹：NONE → FROZEN → TRIMMED → SUMMARIZED → CONSOLIDATED |
| `original_token_count` | 压缩前的 token 数，压缩后记录供审计 |
| `token_count` | 当前 token 数 |
| `importance_score` | 重要性评分，公式：recency×0.4 + relevance×0.4 + initial×0.2，压缩时优先删低分块 |
| `cache_ttl` | `None`=不缓存，`300`=静态块缓存 5 分钟，`600`=系统提示缓存 10 分钟，`1200`=技能缓存 20 分钟 |
| `created_at` | 创建时间（UTC），用于时间门触发和时间衰减计算 |
| `persisted_path` | 压缩后原文存档路径（如 `.chacha_agent/compressed/s1/b-abc.json`），`content` 中仅保留关键行 + 占位引用 |
| `frozen_kept_lines` | 冻结时实际保留的行数（含 error/warning/退出码/首尾行） |
| `frozen_total_lines` | 冻结前原始总行数，供 LLM 判断"这个输出有多大" |
| `compression_history` | 压缩轨迹（如 `["NONE→FROZEN", "FROZEN→SUMMARIZED"]`），已到终态者不再压缩 |
| `content_hash` | 原文 SHA256 哈希（仅 NONE 状态有效），用于复用已有压缩结果（相同原文=相同压缩） |

### 冻结占位引用示例

压缩后 `content` 中保留关键行 + 存档引用，LLM 可以直接判断问题，不需再调工具读取存档：

```
[已冻结] 命令执行完毕，退出码 1，耗时 320ms
--- 关键输出 ---
error: ModuleNotFoundError: No module named 'requests'
warning: config.yaml not found, using defaults
--- 全量存档: .chacha_agent/compressed/s1/b-abc.json (134行, 5000 tokens) ---
```

| 保留行 | 理由 |
|--------|------|
| ERROR / FATAL | LLM 决策关键依据 |
| WARNING | 帮助 LLM 判断严重程度 |
| 退出码 / 耗时 / 行数 | 执行状态摘要 |
| 首尾各 2 行 | 防止断章取义 |

---

## 5. 组装元信息：`ContextAssemblyMeta`

```python
class ContextAssemblyMeta(BaseModel):
    session_id / project_id
    assembled_at: datetime              # 组装时间
    trigger: str                        # normal|compression|first_turn|recovery
    total_tokens: int                   # 总 token
    protected_tokens: int               # 保护区 token
    dynamic_tokens: int                  # 动态区 token
    budget_per_request: int             # 单次调用上限
    budget_per_task: Optional[int]      # 任务级上限
    utilization_ratio: float            # 利用率
    compression_pressure: float         # 压缩激进程度
    trigger_reason: TriggerReason        # 触发原因
    reasoning_budget_tokens: int        # 思考配额
    reasoning_tokens_used: int          # 已用思考 token
    blocks_by_source: Dict[str, int]    # 各来源分布
```

| 字段 | 说明 |
|------|------|
| `total_tokens` | 所有 blocks 的 `token_count` 总和 |
| `protected_tokens` | 仅 `zone=protected` 的 token 总和 |
| `dynamic_tokens` | 仅 `zone=dynamic` 的 token 总和 |
| `budget_per_request` | 单次 LLM 调用的 token 上限（默认 128000） |
| `budget_per_task` | 任务级累计 token 预算，`None` 表示不限 |
| `utilization_ratio` | `total / budget_per_request`，>1 已超限 |
| `compression_pressure` | 0~1，驱动压缩层级跃迁 |
| `trigger_reason` | 当前压缩触发原因 |
| `reasoning_budget_tokens` | 思考 token 配额（0 表示不使用思考） |
| `reasoning_tokens_used` | 累计思考 token 消耗 |
| `blocks_by_source` | 各 `BlockSource` 的 token 分布（用于优化决策） |

---

## 6. 上下文组装结果：`AssembledContext`

```python
class AssembledContext(BaseModel):
    meta: ContextAssemblyMeta
    blocks: List[ContextBlock]
    needs_compression: bool
    recommended_level: CompressionLevel
```

**用途**：`ContextAssembler` 的最终产出。`ContextManager` 读取 `needs_compression` 和 `recommended_level` 触发压缩；`LLMInvoker` 调用 `get_messages()` 生成 API 格式。

| 字段 | 说明 |
|------|------|
| `meta` | 组装元信息（含统计、预算、压力值） |
| `blocks` | 按 `priority` 排序的上下文片段列表 |
| `needs_compression` | `utilization_ratio > trigger_ratio` 时为 True |
| `recommended_level` | 基于 `compression_pressure` 推荐的压缩层级 |

**方法**：

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_messages()` | `List[Dict]` | 所有 blocks 转为 OpenAI/Anthropic API 格式，按 priority 排序 |
| `get_protected_slice()` | `List[ContextBlock]` | 仅返回 `zone=protected` 的 blocks |
| `get_dynamic_slice()` | `List[ContextBlock]` | 仅返回 `zone=dynamic` 的 blocks，按 `importance_score` 降序 |
| `get_statistics()` | `str` | 人类可读的统计摘要 |
| `empty()` | `AssembledContext` | 工厂方法，创建空上下文 |

---

## 7. 典型压缩决策流程

```
ContextAssembler 产出 AssembledContext(blocks=25, total_tokens=120000)
        │
        ▼
ContextManager 读取 meta.utilization_ratio → 0.94（>0.8 触发线）
        │
        ├─ needs_compression = True
        ├─ compression_pressure = 0.82  → recommended_level = TRIMMED
        │
        ▼
ContextCompressor 按策略执行：
        │
        ├─ 1. 保护区 BLOCK 跳过（system_prompt / static_rule）
        │
        ├─ 2. 动态区按 importance_score 升序删除最低分 BLOCK
        │        删掉 3 个 TOOL_RESULT（score=0.3~0.4）
        │
        ├─ 3. 对剩余 TOOL_RESULT 执行 FROZEN（保留首尾 + 退出码）
        │        token_count: 5000 → 200
        │        compression_level → FROZEN
        │        original_token_count = 5000
        │
        └─ 4. total_tokens: 120000 → 85000
             utilization_ratio: 0.94 → 0.66 ← 回到安全区
```

---

## 8. 与其他模型的关联

| 关联模型 | 关系 |
|----------|------|
| `ConversationState` (session.py) | `get_messages_for_llm()` 是简化版上下文，`AssembledContext` 是完整版（含来源/优先级/压缩状态） |
| `HookContext` (hook.py) | `PRE_CONTEXT_ASSEMBLY` 钩子可读取 `AssembledContext`，`POST_CONTEXT_ASSEMBLY` 钩子可修改 |
| `AuditEvent` (audit.py) | 压缩操作生成 `MemoryChangeAuditEvent`（compression 类） |
| `ChaChaConfig.context` (config.py) | `max_tokens` → `budget_per_request`，`compression_trigger_ratio` → 触发线 |
