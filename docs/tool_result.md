# ToolResult — 工具统一返回结构

所有 10 个工具（read / edit / write / bash / grep / glob / task / memory / cache_read / approval_control）共用此结构。

## 设计分层

```
┌─────────────────────────────────┐
│  LLM-facing (序列化给 LLM)       │
│  status / content / error /     │
│  error_type / truncated /       │
│  truncated_from / cache_key /   │
│  data / warnings                │
├─────────────────────────────────┤
│  Internal-only (序列化时排除)     │
│  tool_name / execution_time_ms  │
│  trace / internal               │
└─────────────────────────────────┘
```

## 字段详解

### LLM-facing

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | `"success" \| "error"` | 成功 / 失败 |
| `content` | `str` | 主体内容（代码、匹配结果、摘要等） |
| `error` | `str \| null` | 人类可读错误描述，status=error 时填充 |
| `error_type` | `str \| null` | 机器可读错误分类（见下文枚举） |
| `truncated` | `bool` | 输出是否被截断 |
| `truncated_from` | `int \| null` | 截断前原始字符数 |
| `cache_key` | `str \| null` | 续读缓存 key |

### LLM-facing 元数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `data` | `dict` | 工具级结构化元数据（见各工具 data 约定） |
| `warnings` | `list[str]` | 非致命警告 |

### Internal-only

| 字段 | 类型 | 用途 |
|------|------|------|
| `tool_name` | `str \| null` | 日志 & 统计 |
| `execution_time_ms` | `int \| null` | 性能统计 & telemetry |
| `trace` | `dict` | span_id / parent_span_id / tool_call_id / trace_id |
| `internal` | `dict` | 异常栈、沙箱状态、资源用量、重试次数 |

---

## `error_type` 枚举

| 值 | 含义 | LLM 建议恢复策略 |
|----|------|-----------------|
| `file_not_found` | 文件不存在 | 用 glob 查找正确路径 |
| `permission_denied` | 权限不足 | 告知用户 |
| `timeout` | 执行超时 | 缩小范围重试 |
| `parse_error` | AST/语法解析失败 | 换 text 模式 |
| `exit_code_nonzero` | 命令非零退出 | 检查 stderr (warnings) |
| `invalid_argument` | 参数无效 | LLM 修正参数重试 |
| `network_error` | 网络错误 | 重试 |
| `unknown` | 未知错误 | 告知用户 |

---

## 各工具 data 约定

| 工具 | data 字段 |
|------|----------|
| `read` | `path`, `offset`, `limit`, `total_lines`, `encoding` |
| `edit` | `path`, `replacements`, `bytes_written`, `dry_run` |
| `write` | `path`, `lines`, `bytes` |
| `bash` | `command`, `exit_code` |
| `grep` | `matches`, `files`, `mode` |
| `glob` | `pattern`, `count`, `max_depth` |
| `task` | `subagent_type`, `description` |
| `memory` | `action`, `topic` |
| `cache_read` | `cache_key`, `offset`, `limit` |
| `approval_control` | `action`, `categories`, `persist` |
| `cache_read` | `cache_key`, `offset`, `limit` |
| `approval_control` | `action`, `categories`, `persist` |

---

## 各工具返回示例

### read（成功）
```json
{
  "status": "success",
  "content": "   1| def foo():\n   2|     pass\n[EOF]",
  "truncated": false,
  "data": {
    "path": "a.py",
    "offset": 1,
    "limit": 100,
    "total_lines": 2,
    "encoding": "utf-8"
  },
  "warnings": []
}
```

### read（截断）
```json
{
  "status": "success",
  "content": "   1| ...(前 3000 字符)...",
  "truncated": true,
  "truncated_from": 48500,
  "cache_key": "r_a1b2c3",
  "data": {
    "path": "large.log",
    "offset": 1,
    "limit": 100,
    "total_lines": 50000,
    "encoding": "utf-8"
  },
  "warnings": ["文件较大 (3.2MB)，建议用 offset 分页读取"]
}
```

### bash（失败）
```json
{
  "status": "error",
  "content": "",
  "error": "命令执行失败: gcc -o main main.c",
  "error_type": "exit_code_nonzero",
  "truncated": false,
  "data": {
    "command": "gcc -o main main.c",
    "exit_code": 1
  },
  "warnings": ["stderr: main.c:3:10: fatal error: 'missing.h' not found"]
}
```

### grep（异常）
```json
{
  "status": "error",
  "content": "",
  "error": "AST 解析失败: a.py 第 15 行",
  "error_type": "parse_error",
  "truncated": false,
  "data": {
    "file": "a.py",
    "line": 15
  },
  "warnings": []
}
```
```python
# internal (Only for debug)
{
  "traceback": "SyntaxError: invalid syntax\n  File \"a.py\", line 15\n    def foo(\n           ^",
  "ast_mode": "callers",
  "symbol": "validate"
}
```

---

## 序列化控制

```python
r = ToolResult(...)

r.model_dump()                              # LLM-facing 字段
r.model_dump(exclude_none=True)             # 同上，去掉 None 值字段
r.model_dump(include={"tool_name", "execution_time_ms", "trace", "internal"})
                                            # Internal-only
```

---

## 源码位置

`capabilities/result.py` — 与 `base.py` 同级，供所有工具和 `tool_executor` 消费。
