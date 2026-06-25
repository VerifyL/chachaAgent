# `grep`

正则搜索文件内容。支持正则表达式、分页和上下文行。自动排除 `.venv`/`__pycache__`/`node_modules`/`.git`。

参数：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `pattern` | string | 必填 | Python 正则表达式 |
| `path` | string | 项目根 | 搜索范围（文件或目录） |
| `include_glob` | string | `*.py` | 文件匹配模式 |
| `offset` | int | 0 | 跳过前 N 条（分页） |
| `limit` | int | 200 | 最多返回 N 条结果 |
| `context_lines` | int | 0 | 每条结果前后各 N 行上下文 |
