# `grep`

正则搜索文件内容。自动排除 `.venv`/`__pycache__`/`node_modules`/`.git`/`.codebuddy`。

参数：
- `pattern` (必填)：Python 正则表达式
- `path` (可选)：搜索范围（默认项目根目录）
- `include_glob` (可选)：文件匹配模式，默认 `*.py`

最多返回 200 条匹配。
