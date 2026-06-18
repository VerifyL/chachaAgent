# 内置工具

ChachaAgent 内置 6 个 LLM 可调用工具，全部基于 `capabilities/base.py` 的 `BaseTool` 基类。

| 工具 | 文件 | 风险 | 审批 |
|------|------|------|------|
| `read_file` | `builtins/chunk_streamer.py` | low | ❌ |
| `grep` | `builtins/chunk_streamer.py` | low | ❌ |
| `edit_file` | `builtins/code_patcher.py` | medium | ✅ |
| `load_memory` | `builtins/memory_tool.py` | low | ❌ |
| `remember` | `builtins/memory_tool.py` | low | ❌ |
| `http_request` | `builtins/http_tool.py` | medium | ✅ |
| `bash` | `../sandbox.py` | high | ✅ |

---

## 文件工具

### `read_file(path, start_line?, end_line?)`

读取文件内容。可指定行范围避免输出过大。

```python
read_file(path="main.py")              # 全文
read_file(path="main.py", start_line=1, end_line=50)  # 行 1-50
```

**安全**：只读文件，无修改风险。无审批。

### `grep(pattern, path?, include_glob?)`

在文件中搜索匹配模式。支持正则表达式。

```python
grep(pattern="def hello", path="src/")           # 搜索 src/ 目录
grep(pattern="class.*Test", include_glob="*.py")  # 只搜 .py 文件
```

限制 200 条结果，输出截断到 100K 字符。无审批。

### `edit_file(path, old_string, new_string, replace_all?)`

精确替换文件内容。`old_string` 必须唯一匹配（除非 `replace_all=true`），自动备份到 `.chacha_agent/backups/`。

```python
edit_file(path="main.py", old_string="print('hello')", new_string="print('world')")
edit_file(path="config.json", old_string='"1.0"', new_string='"2.0"', replace_all=True)
```

**审批策略**：medium 风险，需要用户确认。

---

## 记忆工具

### `load_memory(query?)`

读取或搜索长期记忆。无参数时列出可用日期文件。

```python
load_memory()                    # → "可用记忆日期: 2026-06-15.md ..."
load_memory(query="Python 配置")  # → 搜索相关记忆
```

数据来源：`.chacha_agent/memory/projects/{id}/memory/` 每日文件。无审批。

### `remember(content)`

将重要信息记录到长期记忆。

```python
remember(content="用户偏好 Python 3.11，使用 ruff 格式化")
```

写入今日文件，自动带时间戳。无审批。

---

## HTTP 工具

### `http_request(method, url, headers?, body?, timeout?)`

发送 HTTP 请求。仅支持 http/https 协议，默认超时 30 秒。

```python
http_request(method="GET", url="https://api.example.com/data")
http_request(method="POST", url="https://api.example.com/submit", body='{"key":"val"}')
```

**审批策略**：medium 风险，访问外部网络需确认。

---

## 注册到 ToolExecutor

```python
from capabilities.builtins.memory_tool import LoadMemoryTool, RememberTool
from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
from capabilities.builtins.code_patcher import EditFileTool
from capabilities.builtins.http_tool import HttpTool
from capabilities.sandbox import Sandbox
from core.tool_executor import ToolExecutor

tools = [
    ReadFileTool(root=project_root),
    GrepTool(root=project_root),
    EditFileTool(root=project_root),
    LoadMemoryTool(memory_manager),
    RememberTool(memory_manager),
    HttpTool(),
    Sandbox(),
]
executor = ToolExecutor(tools=tools)
```
