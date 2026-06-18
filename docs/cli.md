# ChachaAgent CLI

基于 Textual 的 Claude Code 风格终端界面。

## 运行

```bash
DEEPSEEK_API_KEY=sk-... .venv/bin/python -m interface.cli.app /path/to/project
```

## 布局

```
┌──────────────────────────────────────────┐
│ ChachaAgent v0.1               🔒 ChaCha│ ← Header
├──────────────────────────────────────────┤
│                                          │
│  [You] 读取 main.py                      │ ← RichLog
│                                          │
│  [Chacha]                                │
│  文件内容: print('hello')...              │ ← 流式输出
│                                          │
│  🔧 Calling: read_file                   │ ← 工具调用横幅
│  ✅ read_file done                       │
│                                          │
│  ⏱ 1245ms  |  💬 352 tokens  |  🔄 第3轮  │ ← 审计行
│                                          │
├──────────────────────────────────────────┤
│ deepseek-chat  |  💬 1526  |  🔄 3轮     │ ← 状态栏
├──────────────────────────────────────────┤
│ > _                                      │ ← 输入
└──────────────────────────────────────────┘
```

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+N` | 新会话 |
| `Ctrl+S` | 保存会话 |
| `Ctrl+D` | 调试面板（显示 Token/压缩状态） |
| `Ctrl+X` | 压缩上下文 |
| `Ctrl+L` | 清屏 |
| `Ctrl+C` | 退出 |

## 命令

| 命令 | 功能 |
|------|------|
| `/model <name>` | 切换模型 |
| `/url <url>` | 切换 API URL |
| `/key <sk->` | 设置 API Key |
| `/new` | 新会话 |
| `/save` | 保存会话 |
| `/memory` | 查看记忆文件列表 |
| `/dream` | 运行 DreamPipeline 记忆整合 |
| `/compact` | 手动压缩上下文 |
| `/audit` | 完整审计报告 |
| `/trace` | 最近一轮追踪 |
| `/status` | 系统状态 |
| `/help` | 帮助信息 |

## 自动行为

| 时机 | 动作 |
|------|------|
| 启动 | 加载 CHACHA.md 为"宪法" |
| 每轮结束 | 审计行（耗时/Token/轮次） |
| 3 次对话 | 提示运行 DreamPipeline |
| 24h 未 Dream | 自动提示 |
| Tool 调用中 | 状态栏显示 "⏳ tool_name..." |

## 组件

| 文件 | 说明 |
|------|------|
| `app.py` | Textual 主应用 |
| `agent_bridge.py` | CLI ↔ 核心模块桥接 |
| `session_manager.py` | 会话生命周期 + 审计 |
| `widgets.py` | ChatMessage / ToolCallBanner / StatusBar |

## 开发

```bash
# 单元测试
.venv/bin/python -m pytest tests/unit/test_cli_widgets.py -v
```
