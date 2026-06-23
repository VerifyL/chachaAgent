# 内置工具

ChachaAgent 内置工具，全部基于 `capabilities/base.py` 的 `BaseTool` 基类。

| 工具 | 文件 | 风险 | 审批 |
|------|------|------|------|
| `project_overview` | `builtins/project_overview.py` | low | ❌ |
| `file_outline` | `builtins/file_outline.py` | low | ❌ |
| `list_files` | `builtins/list_files.py` | low | ❌ |
| `depe_analyze` | `builtins/depe_analyzer.py` | low | ❌ |
| `code_intel` | `builtins/code_intel.py` | low | ❌ |
| `subagent` | `builtins/subagent_tool.py` | medium | ❌ |
| `read_file` | `builtins/chunk_streamer.py` | low | ❌ |
| `read_files` | `builtins/chunk_streamer.py` | low | ❌ |
| `grep` | `builtins/chunk_streamer.py` | low | ❌ |
| `edit_file` | `builtins/code_patcher.py` | medium | ✅ |
| `git_diff` | `builtins/git_tools.py` | low | ❌ |
| `git_log` | `builtins/git_tools.py` | low | ❌ |
| `git_status` | `builtins/git_tools.py` | low | ❌ |
| `load_memory` | `builtins/memory_tool.py` | low | ❌ |
| `remember` | `builtins/memory_tool.py` | low | ❌ |
| `write_topic` | `builtins/memory_tool.py` | low | ❌ |
| `read_topic` | `builtins/memory_tool.py` | low | ❌ |
| `bash` | `sandbox.py` | high | ✅ |

---

## 文件工具

### `read_file(path, start_line?, end_line?, symbol?, page?, page_size?, context_lines?)`

读取文件内容。支持多种定位方式：

| 参数 | 说明 |
|------|------|
| `path` | 文件路径（必填） |
| `start_line` / `end_line` | 行范围读取 |
| `symbol` | 跳转到函数/类/变量定义处（优先级最高） |
| `page` / `page_size` | 流式分页（默认每页 200 行） |
| `context_lines` | 目标前后各 N 行上下文 |

```python
read_file(path="main.py")                        # 全文
read_file(path="main.py", start_line=1, end_line=50)  # 行 1-50
read_file(path="main.py", symbol="MyClass")      # 跳转到 MyClass 定义
read_file(path="main.py", page=2, page_size=100) # 第 2 页
```

**安全**：只读，二进制文件自动拒绝（10MB 上限）。无审批。

### `read_files(paths, start_line?, end_line?)`

同时读取多个文件，用 `===` 分隔。减少 LLM 往返。

```python
read_files(paths=["main.py", "config.py"])
```

### `grep(pattern, path?, include_glob?, offset?, limit?, context_lines?)`

在文件中搜索匹配模式。支持正则表达式、分页 (offset/limit) 和上下文行 (context_lines)。

```python
grep(pattern="def hello", path="src/")                 # 搜索 src/ 目录
grep(pattern="class.*Test", include_glob="*.py")        # 只搜 .py 文件
grep(pattern="TODO", context_lines=2)                   # 每条结果前后 2 行上下文
```

限制 200 条结果。无审批。

### `edit_file(path, old_string, new_string, replace_all?)`

精确替换文件内容。底层走 `AtomicWriter`（原子 rename + 版本化备份）。**v2 新增**：

- **模糊匹配 fallback**：精确匹配失败时，用 `difflib.SequenceMatcher`（阈值 80%）找最相似文本，返回提示帮助 LLM 修正
- **多匹配上下文展示**：`old_string` 匹配多处时，列出前 5 处位置（含行号和上下文），不再只报数量

```python
edit_file(path="main.py", old_string="print('hello')", new_string="print('world')")
```

**备份**：`.chacha_agent/backups/{filename}/{timestamp}.bak`，每文件最多保留 5 个版本，7 天自动清理。

**审批策略**：medium 风险，需要用户确认。

---

## 项目探索工具

| 工具 | 快速了解 |
|------|---------|
| `project_overview` | 项目目录树 + README + 元数据，首次使用优先调用 |
| `file_outline` | 提取类/函数签名（不读实现），快速了解文件骨架 |
| `list_files` | 目录列表，支持 glob/深度/隐藏文件，可选 git 状态标注 |
| `depe_analyze` | 分析文件 import 依赖和 export 符号 |
| `code_intel` | 跨文件语义分析（调用者/引用/继承链） |
| `subagent` | 派生子 Agent 执行独立任务 |

---

## Git 工具

Phase 2 新增的三个只读 Git 工具，让 LLM 可以按需深入查看仓库状态。

### `git_diff(path?, staged?, from_ref?, to_ref?)`

查看工作区或暂存区的变更详情。返回结构化 JSON。

```python
git_diff()                             # 工作区 vs HEAD 的完整 diff
git_diff(path="src/main.py")           # 仅看某个文件
git_diff(staged=True)                  # 仅看暂存区
git_diff(from_ref="main", to_ref="feature")  # 比较两个分支
git_diff(to_ref="HEAD~3")              # 工作区与某历史版本比较
```

**非 git 仓库 fallback**：`from_ref + to_ref` 模式下自动降级为系统 `diff -ur`。

**风险**：low，纯只读。超时 15 秒。diff 输出截断至 150 行。

### `git_log(n?, path?, oneline?)`

查看提交历史。返回结构化 JSON（含 commit 列表）。

```python
git_log(n=5)                        # 最近 5 次提交
git_log(path="src/", oneline=False) # 某目录的完整提交历史
```

**风险**：low，纯只读。

### `git_status(detailed?)`

返回工作区和分支的详细状态。

```python
git_status()                        # git status --short
git_status(detailed=True)           # 完整 git status（含分支跟踪）
```

**风险**：low，纯只读。

---

## 记忆工具

### `load_memory(query?)`

读取或搜索长期记忆。无参数时列出可用日期。

```python
load_memory()                    # → "可用记忆日期: 2025-01-15.md ..."
load_memory(query="Python 配置")  # → 搜索相关记忆
```

### `remember(content)`

记录关键信息到今日短期记忆（7 天自动清理）。

### `write_topic(topic, content)`

写入长期主题记忆。五大主题：`user-preferences`、`project-decisions`、`lessons-learned`、`errors-fixed`、`project-progress`。

### `read_topic(topic?)`

读取某主题的长期记忆，不传参数时列出所有主题。

---

## 注册方式

```python
from capabilities.registry import build_tools

tools = build_tools(root=project_root, memory_manager=manager)
# 自动包含以上全部 17 个工具
executor = ToolExecutor(tools=tools)
```

---

## 内置 Hook

除工具外，ChachaAgent 提供**可插拔 Hook**，通过 `HookOrchestrator` 注册后自动注入上下文。

### Git 感知 (`capabilities/builtins/git_context.py`)

自动采集 git 状态，在每轮对话前注入到 LLM 上下文：

```
[Git Context]
分支: feature/git-aware (based on main)
工作区: 2 files changed, 1 untracked
  M  core/context_manager.py
  ?? capabilities/git_context.py
暂存区: 1 file(s) staged
最近提交:
  a1b2c3d feat: add git context provider
```

**注册方式**（在 `agent_bridge.py` 中自动完成，一行可开关）：

```python
hooks.register("git-context", HookPoint.PRE_CONTEXT_ASSEMBLY,
               GitContextHook(project_root=Path.cwd()), priority=10)

# 关闭: hooks.unregister("git-context")
```

**设计要点**：
- 纯只读：仅执行 `git status/diff/log` 无副作用命令
- 容错降级：非 git 仓库时静默跳过，超时/异常不影响主流程
- 轻量：单次采集 < 50ms，`importance=0.55`，上下文压力大时优先裁剪

**工具执行器白名单**：`tool_executor.py` 对 `write_topic`、`remember`、`read_topic`、`load_memory` 四个关键记忆工具豁免 Hook 拦截，保证记忆写入不被 YAML 配置的全局规则意外阻塞。
