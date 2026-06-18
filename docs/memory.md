# 记忆管理系统 (v2.0)

四层设计（双向记忆管理）：

```
write:  LLM 调用 remember 工具 → 按日期 + session 隔离写入 *.md 文件
read:   LLM 调用 load_memory 工具 → 搜索所有文件
dream:  每 10 次会话或 24h 异步运行 → 同时更新 MEMORY.md + CHACHA_MEMORY.md
context: ContextManager 自动注入 MEMORY.md(动态区) + CHACHA_MEMORY.md(保护区)
```

| 模块 | 文件 |
|------|------|
| 记忆读写 | `core/context/memory_manager.py` |
| 整合管道 | `core/context/dream.py` |
| 上下文注入 | `core/context_manager.py` |
| 永久记忆 | `CHACHA_MEMORY.md`（项目根，≤100条，保护区） |

---

## 1. 文件存储结构

```
.chacha_agent/memory/projects/{project_id}/
├── CHACHA_MEMORY.md           ← 永久记忆（保护区，永不删除，≤100条）
├── memory/
│   ├── MEMORY.md               ← autoDream 轻量索引（摘要+路径+时间）
│   └── {YYYY-MM-DD}.md         ← 项目级每日记忆（无 session 回退）
└── sessions/{session_id}/
    ├── {YYYY-MM-DD}.md         ← 会话每日记忆（user+assistant，无工具调用）
    └── tool_cache/             ← 工具结果缓存（会话结束时删除）
```

### 各类文件生命周期

| 文件 | 生命周期 | 清理策略 |
|------|---------|---------|
| `CHACHA_MEMORY.md` | 永存 | autoDream 可更新覆盖，**永不删除** |
| `MEMORY.md` | 永存 | autoDream 周期性覆盖更新，**永不删除** |
| `{date}.md` (每日) | 7天 | autoDream 删除超过 7 天的 |
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

## 4. CHACHA_MEMORY.md 永久记忆（保护区）

项目级永久记忆文件，LLM 在 autoDream 时判断哪些记忆应升级为永久：

- **最大条目**：100 条
- **上下文位置**：protected zone，在 CHACHA.md 之后、SKILL 之前
- **更新方式**：autoDream 全文重新生成（基于旧版本增量更新）
- **清理策略**：永不删除，只覆盖更新

格式示例：
```markdown
## Critical Preferences
- 项目使用 Python 3.11+ + ruff 格式化
- 部署使用 Docker Compose

## Key Decisions
- 2026-06-15: 选择 LanceDB 作为向量存储方案
```

---

## 5. MEMORY.md 索引（autoDream）

`DreamPipeline` 每 10 次会话或 24 小时后异步运行，1 次 LLM 调用同时生成：

```
输入：
  - 旧 MEMORY.md（保留有价值条目）
  - 旧 CHACHA_MEMORY.md（永久记忆基准）
  - 最近 7 天所有 session 的每日记忆文件

LLM 输出：
  - ===MEMORY_MD=== 更新后的轻量索引（摘要 + 源文件路径 + 时间）
  - ===CHACHA_MEMORY_MD=== 更新后的永久记忆（≤100条）
```

---

## 6. 使用示例

```python
mgr = MemoryManager(project_id="p1")
# 读写
mgr.remember("偏好 Python 3.11")      # LLM 工具
mgr.search("Python")                  # LLM 工具

# 永久记忆
mgr.read_permanent_memory()           # ContextManager 自动
mgr.write_permanent_memory(content)   # autoDream 写入

# Session 隔离
mgr = MemoryManager(project_id="p1", session_id="s1")
mgr.remember("Q: xxx\nA: yyy")        # 写入 sessions/s1/2026-06-18.md

# 工具缓存
mgr.cache_tool_result("c1", "read_file", result)
mgr.cleanup_tool_cache()              # 会话结束

# DreamPipeline
dream = DreamPipeline(llm_invoker)
dream.record_session()                # 每次会话结束
if dream.should_run():                # 10 次或 24h
    memory_md, permanent_md = await dream.run(mgr)
```

---

## 7. 配置

```toml
[memory]
prune_days = 7             # 每日文件保留天数
max_memory_lines = 200     # MEMORY.md 最大条目数

[context]
enable_memory_injection = true  # 是否注入 MEMORY.md 索引
enable_permanent_memory = true  # 是否注入 CHACHA_MEMORY.md
```
