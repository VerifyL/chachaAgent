# 工具文档

## 注册

所有工具在 `capabilities/registry.py` 中统一注册（两层加载）：
1. **内置工具**（21 个）— 硬编码在 `build_tools()` 中
2. **用户自定义工具** — 扫描 `~/.chacha/tools/*.py`，同名覆盖内置

`project_init.py`、`agent_bridge.py`、`subagent/spawner.py` 共用同一来源。

## 工具列表

| 工具 | 文件 | 用途 |
|------|------|------|
| `project_overview` | `project_overview.md` | 项目结构总览 |
| `file_outline` | `file_outline.md` | 文件骨架提取 |
| `list_files` | — | 目录树列表（glob/深度/git状态） |
| `depe_analyze` | — | 依赖/符号分析（imports/exports/graph） |
| `code_intel` | — | 跨文件语义分析（调用者/引用/继承） |
| `read_file` | `read_file.md` | 按行读取文件（mmap+seek，默认 500 行） |
| `read_files` | — | 批量读取多个文件 |
| `grep` | `grep.md` | 正则搜索（支持分页+上下文） |
| `edit_file` | `edit_file.md` | 精确替换（原子写入，无截断） |
| `apply_patch` | — | 应用 unified diff 补丁 |
| `subagent` | — | 派生子Agent 执行独立任务 |
| `expand_subagent` | — | 展开查看子Agent 完整结果 |
| `git_diff` | — | 查看工作区/暂存区变更 |
| `git_log` | — | 查看提交历史 |
| `git_status` | — | 查看工作区和分支状态 |
| `set_approval_mode` | — | 控制审批旁路（会话/持久化） |
| `read_cached_output` | — | 读取被截断的缓存输出 |
| `load_memory` | `load_memory.md` | 读取/搜索长期记忆 |
| `write_topic` | `write_topic.md` | 写入主题记忆 |
| `read_topic` | `read_topic.md` | 读取主题记忆 |
| `bash` | — | 执行 shell 命令（沙箱 + 需审批） |
