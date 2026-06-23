"""
capabilities/builtins/depe_analyzer.py
DepsAnalyzer — 单文件依赖/符号分析 + 多文件模块依赖图。

Phase A: 分析单文件的 import 和导出符号。
Phase B: 多文件依赖图（graph / reverse_graph），Python AST + 多语言正则 fallback。
"""

import ast
import json
import logging
import re
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

# ====== 多语言 import 正则 ======
# 键: 扩展名, 值: (compiled_regex, [group_names])
_IMPORT_PATTERNS: dict[str, tuple[re.Pattern, list[str]]] = {
    ".go": (
        re.compile(r'(?:^|\n)\s*import\s+(?:(\w+)\s+)?(\"[^\"]+\")', re.MULTILINE),
        ["alias", "path"],
    ),
    ".rs": (
        re.compile(r'(?:^|\n)\s*use\s+([\w:]+(?:::\{[^}]+\})?[\w:]*)', re.MULTILINE),
        ["path"],
    ),
    ".js": (
        re.compile(r'(?:^|\n)\s*(?:import\s+(?:\{[^}]*\}|\w+)\s+from\s+)?[\'\"]([^\'\"]+)[\'\"]', re.MULTILINE),
        ["path"],
    ),
    ".ts": (
        re.compile(r'(?:^|\n)\s*(?:import\s+(?:\{[^}]*\}|\w+)\s+from\s+)?[\'\"]([^\'\"]+)[\'\"]', re.MULTILINE),
        ["path"],
    ),
    ".jsx": (
        re.compile(r'(?:^|\n)\s*(?:import\s+(?:\{[^}]*\}|\w+)\s+from\s+)?[\'\"]([^\'\"]+)[\'\"]', re.MULTILINE),
        ["path"],
    ),
    ".tsx": (
        re.compile(r'(?:^|\n)\s*(?:import\s+(?:\{[^}]*\}|\w+)\s+from\s+)?[\'\"]([^\'\"]+)[\'\"]', re.MULTILINE),
        ["path"],
    ),
    ".java": (
        re.compile(r'(?:^|\n)\s*import\s+((?:static\s+)?[\w.]+(?:\.[*])?)', re.MULTILINE),
        ["path"],
    ),
    ".kt": (
        re.compile(r'(?:^|\n)\s*import\s+([\w.]+(?:\.[*])?)', re.MULTILINE),
        ["path"],
    ),
    ".swift": (
        re.compile(r'(?:^|\n)\s*import\s+(\w+)', re.MULTILINE),
        ["path"],
    ),
    ".c": (
        re.compile(r'(?:^|\n)\s*#include\s+[<\"]([^>\"]+)[>\"]', re.MULTILINE),
        ["path"],
    ),
    ".cpp": (
        re.compile(r'(?:^|\n)\s*#include\s+[<\"]([^>\"]+)[>\"]', re.MULTILINE),
        ["path"],
    ),
    ".h": (
        re.compile(r'(?:^|\n)\s*#include\s+[<\"]([^>\"]+)[>\"]', re.MULTILINE),
        ["path"],
    ),
    ".hpp": (
        re.compile(r'(?:^|\n)\s*#include\s+[<\"]([^>\"]+)[>\"]', re.MULTILINE),
        ["path"],
    ),
    ".cs": (
        re.compile(r'(?:^|\n)\s*using\s+([\w.]+)', re.MULTILINE),
        ["path"],
    ),
    ".rb": (
        re.compile(r'(?:^|\n)\s*require\s+[\'\"]([^\'\"]+)[\'\"]', re.MULTILINE),
        ["path"],
    ),
}

# Go 多行 import 块
_GO_MULTI_IMPORT = re.compile(r'import\s*\(\s*((?:\s*(?:\w+\s+)?\"[^\"]+\"\s*)+)\s*\)', re.MULTILINE)
_GO_SINGLE_IMPORT = re.compile(r'^\s*(?:(\w+)\s+)?\"([^\"]+)\"', re.MULTILINE)


class DepsAnalyzerTool(BaseTool):
    """分析文件依赖与导出符号 + 多文件模块依赖图。"""

    name = "depe_analyze"
    description = (
        "分析文件的依赖关系和导出符号。action 可选值：\\n"
        "- imports：列出文件所有 import（Python AST + 多语言正则 fallback）\\n"
        "- exports：列出公开函数/类/常量\\n"
        "- both：imports + exports（默认）\\n"
        "- graph：构建模块依赖图（谁依赖谁），支持多文件/目录\\n"
        "- reverse_graph：反向依赖图（谁依赖我）"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径（相对于项目根目录或绝对路径）。graph/reverse_graph 时可选，改用 targets",
            },
            "action": {
                "type": "string",
                "enum": ["imports", "exports", "both", "graph", "reverse_graph"],
                "description": "分析类型，默认 both",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "目标文件/目录列表（graph/reverse_graph 时使用。如 [\\\"core/\\\", \\\"agents/\\\"]）",
            },
        },
        "required": [],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(
        self,
        file_path: str = "",
        action: str = "both",
        targets: list[str] | None = None,
    ) -> str:
        """执行入口，按 action 分发。"""
        # --- graph / reverse_graph ---
        if action in ("graph", "reverse_graph"):
            return await self._execute_graph(action, file_path, targets)

        # --- imports / exports / both ---
        if not file_path:
            return json.dumps({"error": "missing_file_path", "message": "file_path 为必填参数"})

        raw = self._resolve_path(file_path)
        if error := self._validate_path(raw, file_path):
            return error

        source, err = self._read_file(raw)
        if err:
            return err

        ext = raw.suffix.lower()
        result: dict = {
            "file": str(raw.relative_to(self._root)),
            "total_lines": len(source.split("\\n")),
            "language": ext.lstrip(".") or "unknown",
        }

        if action in ("imports", "both"):
            if ext == ".py":
                result["imports"] = self._parse_python_imports(source)
            else:
                result["imports"] = self._parse_generic_imports(source, ext)

        if action in ("exports", "both"):
            if ext == ".py":
                result["exports"] = self._parse_python_exports(source)
            else:
                result["exports"] = self._parse_generic_exports(source, ext)

        return json.dumps(result, ensure_ascii=False)

    # ====== graph 核心 ======
    async def _execute_graph(
        self, action: str, file_path: str, targets: list[str] | None
    ) -> str:
        """构建模块依赖图：正向（谁依赖谁）或反向（谁依赖我）。"""
        # 收集目标文件
        files: list[Path] = []
        if targets:
            for t in targets:
                p = self._resolve_path(t)
                if p.is_dir():
                    files.extend(_collect_source_files(p))
                elif p.is_file():
                    files.append(p)
        elif file_path:
            p = self._resolve_path(file_path)
            if p.is_dir():
                files.extend(_collect_source_files(p))
            elif p.is_file():
                files.append(p)
        else:
            return json.dumps({
                "error": "missing_targets",
                "message": "graph/reverse_graph 需要 targets 或 file_path 参数",
            })

        if not files:
            return json.dumps({"error": "no_files_found", "message": "未找到任何源文件"})

        # 去重
        seen: set[str] = set()
        unique_files: list[Path] = []
        for f in files:
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                unique_files.append(f)
        files = unique_files

        # 构建每个文件的 imports
        file_imports: dict[str, list[dict]] = {}
        for f in files:
            rel = str(f.relative_to(self._root))
            source, _ = self._read_file(f)
            if source is None:
                continue
            ext = f.suffix.lower()
            if ext == ".py":
                imports = self._parse_python_imports(source)
            else:
                imports = self._parse_generic_imports(source, ext)
            file_imports[rel] = imports

        if action == "graph":
            return self._format_graph(file_imports)
        else:
            return self._format_reverse_graph(file_imports)

    def _format_graph(self, file_imports: dict[str, list[dict]]) -> str:
        """格式化正向依赖图。"""
        graph: dict[str, list[dict]] = {}
        stats = {"total_files": len(file_imports), "total_local_deps": 0}

        for file_path, imports in file_imports.items():
            local_deps: list[dict] = []
            for imp in imports:
                if imp.get("type") == "local":
                    dep = {"module": imp["module"]}
                    if imp.get("resolved"):
                        dep["resolved_file"] = imp["resolved_file"]
                    local_deps.append(dep)
            graph[file_path] = local_deps
            stats["total_local_deps"] += len(local_deps)

        return json.dumps({
            "action": "graph",
            **stats,
            "graph": graph,
        }, ensure_ascii=False)

    def _format_reverse_graph(self, file_imports: dict[str, list[dict]]) -> str:
        """格式化反向依赖图。"""
        reverse: dict[str, list[str]] = {}
        stats = {"total_files": len(file_imports), "total_edges": 0}

        for file_path, imports in file_imports.items():
            for imp in imports:
                if imp.get("type") == "local":
                    target = imp.get("resolved_file") or imp["module"]
                    if target not in reverse:
                        reverse[target] = []
                    if file_path not in reverse[target]:
                        reverse[target].append(file_path)
                        stats["total_edges"] += 1

        return json.dumps({
            "action": "reverse_graph",
            **stats,
            "reverse_graph": reverse,
        }, ensure_ascii=False)

    # ====== Python AST 解析 ======
    def _parse_python_imports(self, source: str) -> list[dict]:
        """从 Python 源码提取 import 列表（结构化 JSON）。"""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return [{"error": "syntax_error"}]

        imports: list[dict] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    kind = "local" if (alias.name.startswith(".") or self._is_project_module(top)) else (
                        "stdlib" if _is_stdlib(top) else "third_party"
                    )
                    imports.append({
                        "module": alias.name,
                        "alias": alias.asname,
                        "type": kind,
                        "resolved": False,
                    })
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    kind = "local" if (node.module.startswith(".") or self._is_project_module(top)) else (
                        "stdlib" if _is_stdlib(top) else "third_party"
                    )
                    names = [a.name for a in node.names]
                    imports.append({
                        "module": node.module,
                        "names": names[:8] if len(names) <= 8 else names[:7] + ["..."],
                        "type": kind,
                        "resolved": False,
                    })
                else:
                    imports.append({
                        "module": ".",
                        "names": [a.name for a in node.names],
                        "type": "local",
                        "resolved": False,
                    })

        # 尝试解析本地依赖到具体文件
        for imp in imports:
            if imp.get("type") == "local":
                resolved = self._resolve_python_import(imp["module"])
                if resolved:
                    imp["resolved_file"] = str(resolved.relative_to(self._root))
                    imp["resolved"] = True

        return imports

    def _parse_python_exports(self, source: str) -> list[dict]:
        """从 Python 源码提取公开符号。"""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return [{"error": "syntax_error"}]

        exports: list[dict] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    exports.append({
                        "name": node.name,
                        "kind": "async def" if isinstance(node, ast.AsyncFunctionDef) else "def",
                        "line": node.lineno,
                    })
            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith("_"):
                    exports.append({"name": node.name, "kind": "class", "line": node.lineno})
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        exports.append({"name": target.id, "kind": "const", "line": node.lineno})
        return exports

    # ====== 多语言正则解析 ======
    def _parse_generic_imports(self, source: str, ext: str) -> list[dict]:
        """用正则提取非 Python 文件的 import。"""
        imports: list[dict] = []
        processed: set[str] = set()

        # Go 多行 import 块
        if ext == ".go":
            for match in _GO_MULTI_IMPORT.finditer(source):
                block = match.group(1)
                for lm in _GO_SINGLE_IMPORT.finditer(block):
                    path = lm.group(2)
                    if path not in processed:
                        processed.add(path)
                        imports.append({"module": path, "type": "third_party", "resolved": False})

        pattern_tuple = _IMPORT_PATTERNS.get(ext)
        if pattern_tuple:
            pat, groups = pattern_tuple
            for match in pat.finditer(source):
                path = match.group("path") if "path" in groups else match.group(0)
                path = path.strip('"\'')
                if path not in processed:
                    processed.add(path)
                    kind = "local" if path.startswith(".") else "third_party"
                    imports.append({"module": path, "type": kind, "resolved": False})

        return imports

    def _parse_generic_exports(self, source: str, ext: str) -> list[dict]:
        """用正则提取非 Python 文件的公开符号。"""
        exports: list[dict] = []
        export_patterns = [
            (r'(?:^|\n)\s*export\s+(?:async\s+)?function\s+(\w+)', "function"),
            (r'(?:^|\n)\s*export\s+(?:default\s+)?class\s+(\w+)', "class"),
            (r'(?:^|\n)\s*export\s+(?:const|let|var)\s+(\w+)', "const"),
            (r'(?:^|\n)\s*(?:pub\s+)?fn\s+(\w+)', "fn"),
            (r'(?:^|\n)\s*(?:pub\s+)?struct\s+(\w+)', "struct"),
            (r'(?:^|\n)\s*(?:pub\s+)?enum\s+(\w+)', "enum"),
            (r'(?:^|\n)\s*(?:pub\s+)?trait\s+(\w+)', "trait"),
            (r'(?:^|\n)\s*(?:public\s+)?class\s+(\w+)', "class"),
            (r'(?:^|\n)\s*func\s+(\w+)', "func"),
            (r'(?:^|\n)\s*type\s+(\w+)', "type"),
        ]
        for pat, kind in export_patterns:
            for match in re.finditer(pat, source, re.MULTILINE):
                name = match.group(1)
                if not name.startswith("_"):
                    lineno = source[:match.start()].count("\\n") + 1
                    exports.append({"name": name, "kind": kind, "line": lineno})
        return exports

    # ====== 路径解析 ======
    def _is_project_module(self, top: str) -> bool:
        """判断 top-level 模块是否属于该项目（根目录下存在同名目录/文件）。"""
        return ((self._root / top).is_dir() or
                (self._root / f"{top}.py").is_file() or
                (self._root / "src" / top).is_dir())

    def _resolve_python_import(self, module: str) -> Optional[Path]:
        """将 Python import 路径解析为项目内文件。"""
        if module.startswith("."):
            return None
        parts = module.split(".")
        candidate_py = self._root.joinpath(*parts).with_suffix(".py")
        if candidate_py.is_file():
            return candidate_py
        candidate_pkg = self._root.joinpath(*parts, "__init__.py")
        if candidate_pkg.is_file():
            return candidate_pkg
        return None

    def _resolve_path(self, path_str: str) -> Path:
        """相对/绝对路径 → 绝对路径。"""
        p = Path(path_str)
        return p.resolve() if p.is_absolute() else (self._root / p).resolve()

    def _validate_path(self, raw: Path, original: str) -> Optional[str]:
        """路径安全校验。返回错误 JSON 或 None。"""
        try:
            raw.relative_to(self._root)
        except ValueError:
            return json.dumps({"error": "path_outside_root", "message": f"路径超出项目根目录: {original}"})
        if not raw.exists():
            return json.dumps({"error": "file_not_found", "message": f"文件不存在: {original}"})
        if not raw.is_file():
            return json.dumps({"error": "not_a_file", "message": f"不是文件: {original}"})
        return None

    def _read_file(self, raw: Path) -> tuple[Optional[str], Optional[str]]:
        """读取文件。返回 (内容, 错误JSON)。"""
        try:
            return raw.read_text(encoding="utf-8", errors="replace"), None
        except Exception as e:
            return None, json.dumps({"error": "read_error", "message": str(e)})


# ====== 工具函数 ======

_SOURCE_EXTS = {
    ".py", ".go", ".rs", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".php", ".scala", ".r", ".lua", ".zig",
}


def _collect_source_files(directory: Path, max_files: int = 200) -> list[Path]:
    """收集目录下所有源文件。"""
    files: list[Path] = []
    for f in directory.rglob("*"):
        if f.suffix.lower() in _SOURCE_EXTS and f.is_file():
            files.append(f)
            if len(files) >= max_files:
                break
    return files


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
