"""
capabilities/builtins/depe_analyzer.py
DepsAnalyzer — 单文件依赖/符号分析工具。

Phase A: 分析单文件的 import 和导出符号。
Phase B: (预留) 跨文件引用追踪。
"""

import ast
import logging
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)


class DepsAnalyzerTool(BaseTool):
    """分析文件依赖与导出符号：deps_analyze(file_path, direction=both)"""

    name = "depe_analyze"
    description = (
        "分析文件的依赖关系和导出符号。direction=imports 列出所有 import；"
        "exports 列出公开函数/类/常量；both 两者合并。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径（相对于项目根目录或绝对路径）",
            },
            "direction": {
                "type": "string",
                "enum": ["imports", "exports", "both"],
                "description": "分析方向：imports=依赖, exports=导出符号, both=两者",
            },
        },
        "required": ["file_path", "direction"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, file_path: str, direction: str = "both") -> str:
        raw = (Path(file_path).resolve() if Path(file_path).is_absolute()
               else (self._root / file_path).resolve())
        try:
            raw.relative_to(self._root)
        except ValueError:
            return "[错误] 路径超出项目根目录"
        if not raw.exists() or not raw.is_file():
            return f"[错误] 文件不存在: {file_path}"
        if raw.suffix.lower() != ".py":
            return "[错误] 仅支持 .py 文件"

        try:
            source = raw.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return f"[错误] 语法解析失败: {e}"

        lines = source.split("\n")
        parts = [f"[文件] {raw.name} | {len(lines)}行"]

        if direction in ("imports", "both"):
            parts.append("\n── 依赖 (imports) ──")
            std_lib = 0
            third_party = 0
            local = 0
            local_imports: list[str] = []
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("."):
                            local += 1
                            local_imports.append(alias.name)
                        elif _is_stdlib(alias.name.split(".")[0]):
                            std_lib += 1
                        else:
                            third_party += 1
                            local_imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        if node.module.startswith("."):
                            local += 1
                            local_imports.append(f"from {node.module} import ...")
                        elif _is_stdlib(node.module.split(".")[0]):
                            std_lib += 1
                        else:
                            third_party += 1
                            names = [alias.name for alias in node.names]
                            local_imports.append(f"from {node.module} import {', '.join(names[:5])}")
                            if len(names) > 5:
                                local_imports[-1] += ", ..."
                    else:
                        # `from . import x`
                        local += 1
                        local_imports.append("from . import ...")
            parts.append(f"标准库: {std_lib} | 三方包: {third_party} | 项目内: {local}")
            for imp in local_imports:
                parts.append(f"  {imp}")

        if direction in ("exports", "both"):
            parts.append("\n── 导出符号 (exports) ──")
            exports = _extract_exports(tree)
            if exports:
                for name, kind, lineno in exports:
                    parts.append(f"  {kind} {name}  # L{lineno}")
            else:
                parts.append("  (无公开符号)")

        return "\n".join(parts)


def _is_stdlib(module: str) -> bool:
    """判断是否为 Python 标准库模块。"""
    stdlibs = {
        "os", "sys", "re", "json", "math", "time", "datetime", "pathlib",
        "collections", "itertools", "functools", "typing", "enum", "dataclasses",
        "abc", "logging", "io", "base64", "hashlib", "uuid", "random",
        "threading", "asyncio", "subprocess", "shutil", "tempfile", "glob",
        "argparse", "configparser", "csv", "xml", "html", "http", "urllib",
        "socket", "ssl", "email", "copy", "pprint", "textwrap", "string",
        "inspect", "traceback", "warnings", "contextlib", "fractions",
        "decimal", "statistics", "pickle", "shelve", "sqlite3",
        "unittest", "pdb", "profile", "tokenize", "ast",
    }
    return module in stdlibs


def _extract_exports(tree: ast.AST) -> list[tuple[str, str, int]]:
    """提取文件中的公开符号（函数、类、常量）。"""
    exports: list[tuple[str, str, int]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                exports.append((node.name, "def" if isinstance(node, ast.FunctionDef) else "async def", node.lineno))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                exports.append((node.name, "class", node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    exports.append((target.id, "const", node.lineno))
    return exports
