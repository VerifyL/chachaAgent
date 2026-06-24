# 工具文档

## 注册

所有工具在 `capabilities/registry.py` 中统一注册，`project_init.py` 和 `agent_bridge.py` 共用同一来源。

## 工具列表

| 工具 | 文件 | 用途 |
|------|------|------|
| `project_overview` | `project_overview.md` | 项目结构总览 |
| `file_outline` | `file_outline.md` | 文件骨架提取 |
| `project_overview` | `project_overview.md` | 项目结构总览 |
| `file_outline` | `file_outline.md` | 文件骨架提取 |
| `list_files` | — | 目录树列表 |
| `depe_analyze` | — | 依赖/符号分析 |
| `read_file` | `read_file.md` | 按行读取文件（mmap+seek，默认500行） |
| `read_files` | — | 批量读取多个文件 |
| `grep` | `grep.md` | 正则搜索（支持分页+上下文） |
| `edit_file` | `edit_file.md` | 文件修改（原子写入，无截断） |
| `load_memory` | `load_memory.md` | 读取会话记忆 |
| `remember` | `remember.md` | 写入短期记忆 |
| `write_topic` | `write_topic.md` | 写入主题记忆 |
| `read_topic` | `read_topic.md` | 读取主题记忆 |
| `subagent` | — | 派生子Agent 执行独立任务 |
| `expand_subagent` | — | 展开查看子Agent 完整结果 |
| `bash` | — | 执行 shell 命令（需审批） |
