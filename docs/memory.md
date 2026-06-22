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
      2026-06-22.md                      ← 每日对话
      topics/                            ← 主题记忆
      tool_cache/                        ← 工具缓存
      checkpoint.json                    ← 会话检查点

~/.chacha/USER_MEMORY.md                 ← 用户级永久记忆
```

## 触发时机

| 层 | 文件 | 触发 |
|------|------|------|
| Session Dream | `sessions/{sid}/MEMORY.md` | 10轮/24h/切session前 |
| Project Dream | `CHACHA_MEMORY.md` | 同 Session Dream 一起 |
| Global Dream | `~/.chacha/USER_MEMORY.md` | 50轮/72h/手动 |

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
