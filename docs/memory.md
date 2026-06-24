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
      2026-06-22.md                      ← 每日对话（remember() 写入）
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
| `remember(content)` | 追加今日记忆 `YYYY-MM-DD.md`，时间戳格式 `## 10:30` |
| `read_day(date)` | 读指定日期记忆 |
| `list_days(limit=50)` | 列出所有日期（倒序） |
| `search(query)` | 跨日期搜索关键词（返回前 5 条） |
| `read()` | 读 MEMORY.md 索引 |
| `read_permanent_memory()` | 读 CHACHA_MEMORY.md |
| `write_topic(topic, content)` | 写入主题记忆 |
| `read_topic(topic)` | 读取主题记忆 |
| `read_recent_days(3)` | 读最近 N 天（自动注入 context） |
| `prune_old_days()` | 删除超 7 天记忆 |

## 触发时机

| 层 | 文件 | 触发 |
|------|------|------|
| `remember()` | `sessions/{sid}/YYYY-MM-DD.md` | 每轮对话后自动写入 |
| Session Dream | `sessions/{sid}/MEMORY.md` | 10轮/24h/切session前 |
| Project Dream | `CHACHA_MEMORY.md` | 同 Session Dream 一起 |
| Global Dream | `~/.chacha/USER_MEMORY.md` | 50轮/72h/自动 |

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
