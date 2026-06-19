# 记忆管理系统 (v2.1)

五层设计（双向记忆管理 + 跨项目提炼）：

```
write:   LLM 调用 remember / write_topic 工具 → 按日期 + session 隔离写入 *.md 文件
read:    LLM 调用 load_memory / read_topic 工具 → 搜索所有文件
dream:   每 N 次会话或定时异步运行 → 同时更新 MEMORY.md + CHACHA_MEMORY.md（项目级）
global:  项目 DreamPipeline 触发后检查 → 跨项目合并 → ~/.chacha/USER_MEMORY.md
context: ContextManager 自动注入 MEMORY.md(动态区) + CHACHA_MEMORY.md + USER_MEMORY.md(保护区)
```

| 模块 | 文件 |
|------|------|
| 记忆读写 | `core/context/memory_manager.py` |
| 整合管道（项目级） | `core/context/dream.py` |
| 整合管道（用户级） | `core/context/global_dream.py` |
| 上下文注入 | `core/context_manager.py` |
| 项目永久记忆 | `CHACHA_MEMORY.md`（项目级，无条目上限，保护区） |
| 用户永久记忆 | `USER_MEMORY.md`（用户级，跨项目，保护区） |
| Topic 工具 | `capabilities/builtins/memory_tool.py` |

---

## 1. 文件存储结构

### 项目级

```
~/.chacha/projects/{project_id}/
├── CHACHA_MEMORY.md           ← 项目永久记忆（保护区，永不删除）
├── topics/                     ← 结构化主题存储
│   └── {topic_name}.md        ← write_topic / read_topic 工具读写
├── memory/
│   ├── MEMORY.md               ← DreamPipeline 轻量索引（摘要+路径+时间）
│   └── {YYYY-MM-DD}.md         ← 项目级每日记忆
└── sessions/{session_id}/
    ├── {YYYY-MM-DD}.md         ← 会话每日记忆（user+assistant，无工具调用）
    └── tool_cache/             ← 工具结果缓存（会话结束时删除）
```

### 用户级（全局）

```
~/.chacha/
└── USER_MEMORY.md              ← 跨项目用户永久记忆（GlobalDream 产物）
```

### 各类文件生命周期

| 文件 | 生命周期 | 清理策略 |
|------|---------|---------|
| `CHACHA_MEMORY.md` | 永存 | DreamPipeline 增量合并更新，**永不删除** |
| `USER_MEMORY.md` | 永存 | GlobalDream 增量合并更新，**永不删除** |
| `MEMORY.md` | 永存 | DreamPipeline 全量重建，**永不删除** |
| `topics/*.md` | 永存 | 用户/LLM 主动管理，不自动清理 |
| `{date}.md` (每日) | 7天 | DreamPipeline prune 删除超过 7 天的 |
| `tool_cache/` | 单次会话 | 会话结束时删除整个目录 |

---

## 2. 每日文件（raw 层）

LLM 通过 `remember` 工具写入。按 session 隔离存储：

```
sessions/{session_id}/2026-06-18.md
  → Q: 如何配置 ruff
  → A: 在 pyproject.toml 中添加 [tool.ruff] 配置
```

- 每次对话结束才异步保存
- 只包含 user + assistant 内容，**不含工具调用**
- 按 `## ISO时间戳` 分割条目

---

## 3. 工具结果缓存（两阶段）

### Stage 1: Dispatcher 层（宽松，10个）

```
触发：工具执行后，累积结果数 > 10
操作：第 1 ~ (N-10) 个结果 → JSON 占位符
     最近 10 个结果 → 保持完整
占位格式：
  {"toolname": "read_file", "result_summary": "读取了 main.py...", "cache_path": "tool_cache/t3.json"}
```

### Stage 2: ContextCompressor FROZEN 层（激进）

```
触发：utilization > trigger_ratio，进入 FROZEN 压缩
操作：
  - JSON 占位符 → key 最小化 {"t":"x","s":"x","p":"x"}
  - 完整结果 → 截断到 150 字符摘要 + 缓存文件
```

---

## 4. CHACHA_MEMORY.md 项目永久记忆（保护区）

项目级永久记忆文件，DreamPipeline 在整合时判断哪些记忆应升级为永久：

- **存储位置**：`~/.chacha/projects/{id}/CHACHA_MEMORY.md`
- **更新方式**：增量合并（KEEP / UPDATE / DELETE / NEW）
- **上下文位置**：protected zone priority=3，在 USER_MEMORY.md 之后、SKILL 之前
- **清理策略**：永不删除，只增量更新

格式示例：
```markdown
## Critical Preferences
- 项目使用 Python 3.11+ + ruff 格式化
- 部署使用 Docker Compose

## Key Decisions
- 2026-06-15: 选择 LanceDB 作为向量存储方案
```

---

## 5. USER_MEMORY.md 用户永久记忆（GlobalDream）

GlobalDream 在项目级 DreamPipeline 触发后检查，跨项目提炼用户级知识：

- **存储位置**：`~/.chacha/USER_MEMORY.md`
- **触发条件**：累计 50 次项目 Dream 或距上次 > 168 小时（7 天）
- **数据来源**：所有 `~/.chacha/projects/*/CHACHA_MEMORY.md` + 旧 `USER_MEMORY.md`
- **更新方式**：增量合并（KEEP / UPDATE / DELETE / NEW），LLM 1 次调用
- **上下文位置**：protected zone priority=2，在 CHACHA.md 之后、CHACHA_MEMORY.md 之前
- **只提取跨项目信息**：用户偏好、通用经验、跨项目错误模式
- **不包含**：单项目特定技术栈、里程碑、一次性错误

### 触发决策逻辑

```python
# 每次项目 DreamPipeline 完成后：
gd = get_global_dream()
gd.record_project_dream()      # 计数 +1
if gd.should_run():             # ≥50 次项目 dream 或 >168 小时
    await gd.run()              # 跨项目合并 → 写入 USER_MEMORY.md
```

---

## 6. Topic 工具（结构化主题存储）

LLM 可通过 `write_topic` / `read_topic` 工具管理持久化主题：

```python
write_topic("user-preferences", "喜欢用 pytest，偏好深色主题")
read_topic()                    # 列出所有主题
read_topic("user-preferences")  # 读取指定主题内容
```

- **存储位置**：`~/.chacha/projects/{id}/topics/{name}.md`
- **用途**：结构化偏好/配置/重要信息，供 DreamPipeline 整合时引用
- **生命周期**：不自动清理，由用户/LLM 主动管理

---

## 7. MEMORY.md 索引（DreamPipeline）

`DreamPipeline` 每 N 次会话或超时后异步运行，1 次 LLM 调用同时生成：

```
输入：
  - 旧 MEMORY.md（保留有价值条目）
  - 旧 CHACHA_MEMORY.md（永久记忆基准）
  - 最近 7 天所有 session 的每日记忆文件

LLM 输出：
  - ===MEMORY_MD=== 更新后的轻量索引（摘要 + 源文件路径 + 时间）
  - ===CHACHA_MEMORY_MD=== 更新后的永久记忆
```

### 触发机制

DreamPipeline 在每轮对话结束时自动检查触发条件：

```python
# app.py 每轮对话结束后
self._session.record_dream_hint()    # 会话计数 +1
if self._session.should_dream():     # ≥N 次或超时
    await self._bridge.run_dream()   # 异步执行整合
    self._session.mark_dream_run()   # 重置计数
```

触发阈值在 `core/context/dream.py` 中集中管理：

```python
_DREAM_SESSION_COUNT = 10    # 每 N 次会话触发
_DREAM_HOURS = 24            # 距上次运行超 N 小时触发
```

---

## 8. 使用示例

```python
mgr = MemoryManager(project_id="p1")
# 读写
mgr.remember("偏好 Python 3.11")      # LLM 工具
mgr.search("Python")                  # LLM 工具

# 永久记忆
mgr.read_permanent_memory()           # ContextManager 自动
mgr.write_permanent_memory(content)   # DreamPipeline 写入

# Session 隔离
mgr = MemoryManager(project_id="p1", session_id="s1")
mgr.remember("Q: xxx\nA: yyy")        # 写入 sessions/s1/2026-06-18.md

# Topic 工具
mgr.write_topic("config", "项目使用 Python 3.12")
mgr.read_topic("config")

# 工具缓存
mgr.cache_tool_result("c1", "read_file", result)
mgr.cleanup_tool_cache()              # 会话结束

# DreamPipeline（自动触发，也可手动调用）
dream = DreamPipeline(llm_invoker)
memory_md, permanent_md = await dream.run(mgr)

# GlobalDream（自动触发，也可手动调用）
from core.context.global_dream import get_global_dream
gd = get_global_dream()
gd.record_project_dream()
if gd.should_run():
    await gd.run()
```

---

## 9. 配置

```toml
[memory]
prune_days = 7             # 每日文件保留天数
max_memory_lines = 200     # MEMORY.md 最大条目数

[context]
enable_memory_injection = true  # 是否注入 MEMORY.md 索引
enable_permanent_memory = true  # 是否注入 CHACHA_MEMORY.md
```
