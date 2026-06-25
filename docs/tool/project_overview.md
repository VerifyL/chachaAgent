# `project_overview`

项目结构总览工具。了解项目时优先使用。

参数：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_items` | int | 200 | 目录树最大条目数（上限 1000） |

返回：
- `pyproject.toml` 元数据（name / version / description）
- README.md 摘要（前 300 字符）
- 目录树（排除 .venv、__pycache__、node_modules、.git），截断时提示调大 `max_items`
