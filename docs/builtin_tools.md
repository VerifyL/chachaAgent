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
| `expand_subagent` | `builtins/expand_subagent.py` | low | ❌ |
| `read_file` | `builtins/chunk_streamer.py` | low | ❌ |
| `read_files` | `builtins/chunk_streamer.py` | low | ❌ |
| `grep` | `builtins/chunk_streamer.py` | low | ❌ |
| `edit_file` | `builtins/code_patcher.py` | low | ❌ |
| `apply_patch` | `builtins/diff_patcher.py` | medium | ❌ |
| `git_diff` | `builtins/git_tools.py` | low | ❌ |
| `git_log` | `builtins/git_tools.py` | low | ❌ |
| `git_status` | `builtins/git_tools.py` | low | ❌ |
| `load_memory` | `builtins/memory_tool.py` | low | ❌ |
| `write_topic` | `builtins/memory_tool.py` | low | ❌ |
| `read_topic` | `builtins/memory_tool.py` | low | ❌ |
| `set_approval_mode` | `builtins/approval_control.py` | high | ✅ |
| `read_cached_output` | `builtins/cache_reader.py` | low | ❌ |
| `bash` | `sandbox.py` | high | ✅ |

---

## 文件工具

### `read_file(path, offset=1, limit=500, symbol?, context_lines?)`

读取文件内容。底层 mmap + seek 字节偏移读取，不加载全文件。支持多种定位方式：

| 参数 | 说明 |
|------|------|
| `path` | 文件路径（必填） |
| `offset` | 起始行号（1-based），默认 1 |
| `limit` | 最大行数，默认 500 |
| `symbol` | 跳转到函数/类/变量定义处（优先级高于 offset） |
| `context_lines` | symbol 定位行前后各 N 行上下文 |

```python
read_file(path="main.py")                            # 默认行 1-500
read_file(path="main.py", offset=100, limit=50)      # 行 100-149
read_file(path="main.py", symbol="MyClass")           # 跳转到 MyClass 定义
read_file(path="main.py", symbol="MyClass", context_lines=5)  # 类定义前后 5 行
```

返回值 JSON：
```json
{"file":"main.py","offset":1,"lines_read":500,"next_offset":501,"total_lines":2430,"has_more":true,"content":"..."}
```
`next_offset` 可直接作为下一轮 offset 参数（盲传分页）。

**安全**：mmap 只读映射，二进制文件自动拒绝（10MB 上限）。无审批。

### `read_files(paths, offset=1, limit=200)`

同时读取多个文件，每个文件用 `===` 分隔 + 元数据行。减少 LLM 往返。

```python
read_files(paths=["main.py", "config.py"])
read_files(paths=["main.py", "utils.py"], offset=50, limit=100)
```

### `grep(pattern, path?, include_glob?, offset?, limit?, context_lines?)`

在文件中搜索匹配模式。支持正则表达式、分页 (offset/limit) 和上下文行 (context_lines)。

```python
grep(pattern="def hello", path="src/")                 # 搜索 src/ 目录
grep(pattern="class.*Test", include_glob="*.py")        # 只搜 .py 文件
grep(pattern="TODO", context_lines=2)                   # 每条结果前后 2 行上下文
```

限制 200 条结果。无审批。

### `edit_file(path, old_string, new_string, replace_all=False)`

精确替换文件内容。底层走 `AtomicWriter`（临时文件 + fsync + 原子 rename），无截断风险。

- **匹配失败时**：返回文件头部 15 行 + 总行数，帮助 LLM 修正 old_string
- **替换成功时**：返回替换次数 + 备份路径

```python
edit_file(path="main.py", old_string="print('hello')", new_string="print('world')")
edit_file(path="config.py", old_string="version=1", new_string="version=2", replace_all=True)
```

**备份**：`.chacha_agent/backups/{filename}/{timestamp}.bak`，每文件最多保留 5 个版本，7 天自动清理。

**审批策略**：low 风险，无需审批。

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

### `write_topic(topic, content)`

写入长期主题记忆。五大主题：`user-preferences`、`project-decisions`、`lessons-learned`、`errors-fixed`、`project-progress`。

### `read_topic(topic?)`

读取某主题的长期记忆，不传参数时列出所有主题。

---

## 注册方式

### 两层加载（后覆盖先）

```python
from capabilities.registry import build_tools

tools = build_tools(root=project_root, memory_manager=manager)
# 第一层：自动包含以上全部 21 个内置工具
# 第二层：扫描 ~/.chacha/tools/*.py，同名工具覆盖内置
executor = ToolExecutor(tools=tools)
```

### 用户自定义工具

将 `.py` 文件放入 `~/.chacha/tools/`，其中任何具备 `name` 属性的类会被自动加载为工具。规则：

- **命名**：排除 `_` 和 `.` 开头的文件
- **识别**：`isinstance(attr, type) and hasattr(attr, "name")` 的类
- **覆盖**：与内置工具同名时，用户工具覆盖内置（后加载优先）
- **自动创建**：`~/.chacha/tools/` 不存在时首次运行时自动创建

### 执行流水线

```
find → policy → pre-hooks → execute(重试) → 截断 → post-hooks → telemetry
```

1. **find** — 按 `name` 查注册表
2. **policy** — `PolicyEngine.evaluate_tool()` 评估（白/黑名单 + 源码保护 + 审批）
3. **pre-hooks** — 前置钩子（记忆工具 `write_topic/read_topic/load_memory` 豁免）
4. **execute** — 最多 3 次重试（退避 1s/2s），仅超时/网络可重试
5. **截断** — 输出 >200K 字符时换行边界截断，缓存完整结果
6. **post-hooks + telemetry** — 记录审计

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

**工具执行器白名单**：`tool_executor.py` 对 `write_topic`、`read_topic`、`load_memory` 三个关键记忆工具豁免 Hook 拦截，保证记忆写入不被 YAML 配置的全局规则意外阻塞。
