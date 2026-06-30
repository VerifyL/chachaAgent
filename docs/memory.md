# 记忆系统

三层记忆架构：

```
Session 级  →  Project 级  →  User 级
MEMORY.md      CHACHA_MEMORY      USER_MEMORY
```

## 目录

```
~/.chacha/projects/{hash}/
  CHACHA_MEMORY.md                       ← 项目永久记忆
  memory/sessions/
    {sid}/
      MEMORY.md                          ← Session Dream 产物
      2026-06-22.md                      ← 每日对话（Agent 自动追加）
      topics/                            ← 主题记忆
        user-preferences.md              ← 用户偏好
        project-decisions.md             ← 技术决策
        lessons-learned.md               ← 踩坑教训
      tool_cache/                        ← 工具缓存
      checkpoint.json                    ← 会话检查点

~/.chacha/USER_MEMORY.md                 ← 用户级永久记忆
```

## MemoryManager 方法

| 方法 | 用途 |
|------|------|
| `read_day(date)` | 读指定日期记忆 |
| `topics()` | 列出所有日期（倒序） |
| `search(query)` | 跨日期搜索关键词（返回前 5 条） |
| `read()` | 读 MEMORY.md 索引 |
| `read_permanent_memory()` | 读 CHACHA_MEMORY.md |
| `topic_write(topic, content)` | 写入主题记忆 |
| `topic_read(topic)` | 读取主题记忆 |
| `recent(days=3)` | 读最近 N 天（自动注入 context） |
| `prune_old_days()` | 删除超 7 天记忆 |

## 触发时机

| 层 | 文件 | 触发 |
|------|------|------|
| Agent auto-append | `sessions/{sid}/YYYY-MM-DD.md` | 每轮对话后自动写入 |
| Session Dream | `sessions/{sid}/MEMORY.md` | 10 次会话/24h/切 session 前 |
| Project Dream | `CHACHA_MEMORY.md` | 同 Session Dream 一起 |
| Global Dream | `~/.chacha/USER_MEMORY.md` | 50 次项目 Dream/168h/自动 |

## DreamPipeline — 项目级记忆整合

`core/context/dream.py` 实现会话结束后的异步记忆整合，不会阻塞正常对话。

### 触发条件（二选一，先到先触发）

- **累计 10 次会话**（`dream_rounds`）
- **距上次运行超过 24 小时**（`dream_hours`）

### 五阶段流水线

```
record_session() → should_run()? → run():
  1. Gather  ─ 收集最近 30 天每日文件 + 所有 topic 内容
  2. Consolidate ─ 1 次 LLM 调用，同时输出 MEMORY.md + CHACHA_MEMORY.md
  3. Write   ─ 写入 MEMORY.md + CHACHA_MEMORY.md
  4. Prune   ─ 删除超过 7 天的旧每日文件
  5. Notify  ─ 通知 GlobalDream（record_project_dream）
```

### 增量合并规则（CHACHA_MEMORY.md）

LLM 按 KEEP / UPDATE / DELETE / NEW 四类处理每个条目：

| 操作 | 含义 | ID 处理 |
|------|------|---------|
| KEEP | 依然有效，保留原样 | 保留原 `[id:xxx]` |
| UPDATE | 信息过时，改写内容 | 保留原 `[id:xxx]` |
| DELETE | 不再适用，删除 | 移除整条 |
| NEW | 从新记忆中提取的高价值信息 | 分配新 `[id:xxx]` |

### 使用示例

```python
from core.context.dream import DreamPipeline

pipeline = DreamPipeline(
    llm_invoker=invoker,
    max_entries=200,         # 单次整合最大条目
    prune_days=7,            # 清理超过 7 天的旧文件
    session_trigger=10,      # 10 次会话触发
    hours_trigger=24,        # 或 24 小时触发
)

# 每次会话结束时调用
pipeline.record_session()

# 满足条件时执行（异步，不阻塞）
if pipeline.should_run():
    memory_md, permanent_md = await pipeline.run(memory_manager)
    print(f"MEMORY.md: {len(memory_md)} chars")
    print(f"CHACHA_MEMORY.md: {len(permanent_md)} chars")
```

### LLM 输出格式

DreamPipeline 的 system prompt 要求 LLM 输出两段标记分隔的内容：

```
===MEMORY_MD===
## Memory Index (autoDream generated at 2026-06-30T...)

### User Preferences
- Summary line → topics/user-preferences.md
...

===CHACHA_MEMORY_MD===
## Permanent Project Memory (autoDream updated at 2026-06-30T...)

### Critical Preferences
- [id:pref-001] 用户偏好...
```

解析器自动分离两段，兼容旧版 `---` 分隔格式。

## GlobalDream — 用户级跨项目记忆整合

`core/context/global_dream.py` 在每个项目 DreamPipeline 完成后检查触发，提炼跨项目共性知识到 `~/.chacha/USER_MEMORY.md`。

### 触发条件

- **累计 50 次项目级 DreamPipeline**（`dream_rounds`）
- **或距上次运行超过 168 小时（7 天）**（`dream_hours`）

### 整合流程

```
DreamPipeline 完成
  → GlobalDream.record_project_dream()
  → should_run()?
    → 1. Gather ─ 收集所有 ~/.chacha/projects/*/CHACHA_MEMORY.md
    → 2. Consolidate ─ 1 次 LLM 调用，增量合并
    → 3. Write ─ 写入 ~/.chacha/USER_MEMORY.md
```

### 筛选规则

GlobalDream **只提取跨项目共性信息**，丢弃项目特定内容：

| ✅ 提取（跨项目通用） | ❌ 丢弃（项目特定） |
|-----------------------|---------------------|
| 用户偏好（如"偏好 pytest"） | 项目技术栈选择 |
| 通用教训（如"始终验证 API 响应"） | 项目里程碑 |
| 跨项目重复出现的错误模式 | 单次偶发错误 |

### 使用示例

```python
from core.context.global_dream import get_global_dream, read_global_permanent_memory

# 获取模块级单例
gd = get_global_dream()

# 配置（通常在 framework 初始化时完成）
gd.configure(
    llm_invoker=invoker,
    dream_rounds=50,
    dream_hours=168,
)

# 每次项目 DreamPipeline 完成后调用
gd.record_project_dream()

# 满足条件时执行
if gd.should_run():
    # DreamPipeline 中会自动以 asyncio.create_task 触发
    merged = await gd.run()
    print(f"USER_MEMORY.md: {len(merged)} chars")

# 读取当前用户级永久记忆
content = read_global_permanent_memory()
```

### DreamPipeline → GlobalDream 级联

`DreamPipeline.run()` 第 5 阶段自动调用 `get_global_dream().record_project_dream()`，并在满足条件时以 `asyncio.create_task(gd.run())` 异步触发 GlobalDream。因此正常使用时无需手动调用 GlobalDream——它是全自动的级联。

## 隔离

- 每日对话: session 间完全隔离 (`sessions/{sid}/`)
- 项目永久记忆: 项目内所有 session 共享
- 用户永久记忆: 所有项目共享

## 配置

```toml
[auto_memory]
dream_rounds = 15
dream_hours = 24
global_dream_rounds = 100
global_dream_hours = 72
```
