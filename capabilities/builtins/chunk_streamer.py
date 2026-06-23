"""
capabilities/builtins/chunk_streamer.py
ReadFile + Grep — 文件读写工具（BaseTool）。

用法:
  read_file(path, start_line?, end_line?)  → 读取文件内容
  grep(pattern, path?, include_glob?)       → 搜索文件内容
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 100_000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# 二进制扩展名黑名单
_BINARY_EXTS = frozenset({
    ".pyc", ".so", ".dll", ".dylib", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".bin", ".dat", ".db", ".sqlite", ".o", ".a", ".lib",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
})


class ReadFileTool(BaseTool):
    """读取文件：read_file(path, start_line?, end_line?)"""

    name = "read_file"
    description = (
        "读取文件内容。支持符号跳转（symbol=函数/类名直接定位）、"
        "流式分页（page+page_size）、行范围（start_line/end_line）。"
        "文件路径相对于项目根目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于项目根目录或绝对路径）"},
            "start_line": {"type": "integer", "description": "起始行号（1-based，可选）"},
            "end_line": {"type": "integer", "description": "结束行号（1-based，含），可选"},
            "symbol": {"type": "string", "description": "跳转到函数/类/变量定义处。优先级高于 start_line/page"},
            "page": {"type": "integer", "description": "页码（1-based），配合 page_size 自动分页。与 symbol 可组合使用"},
            "page_size": {"type": "integer", "description": "每页行数，默认 200"},
            "context_lines": {"type": "integer", "description": "目标（symbol 定位行或指定行）前后各 N 行上下文，默认 0"},
        },
        "required": ["path"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, path: str, start_line: int = 0, end_line: int = 0,
                      symbol: str = "", page: int = 0, page_size: int = 200,
                      context_lines: int = 0) -> str:
        import json

        # 1. 路径解析 + containment 检查
        raw = (Path(path).resolve() if Path(path).is_absolute()
               else (self._root / path).resolve())
        try:
            raw.relative_to(self._root)
        except ValueError:
            return json.dumps({"error": "access_denied", "message": "路径超出项目根目录"}, ensure_ascii=False)

        if not raw.exists():
            return json.dumps({"error": "not_found", "message": f"文件不存在: {path}"}, ensure_ascii=False)
        if not raw.is_file():
            return json.dumps({"error": "not_a_file", "message": f"不是文件: {path}"}, ensure_ascii=False)

        # 2. 大小检查（避免 OOM）
        try:
            fsize = raw.stat().st_size
        except OSError as e:
            return json.dumps({"error": "access_error", "message": str(e)}, ensure_ascii=False)
        if fsize > MAX_FILE_SIZE:
            return json.dumps({"error": "file_too_large", "message": "请用 grep 搜索或指定 symbol/page 范围"}, ensure_ascii=False)

        # 3. 二进制检测
        if raw.suffix.lower() in _BINARY_EXTS:
            return json.dumps({"error": "binary_file", "message": f"二进制文件: {path}"}, ensure_ascii=False)

        # 4. 读取全量行
        try:
            with open(raw, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except PermissionError:
            return json.dumps({"error": "permission_denied", "message": f"权限不足: {path}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": "read_error", "message": str(e)}, ensure_ascii=False)

        total = len(lines)

        # 5. 符号解析（优先级最高）
        symbol_line = 0
        if symbol:
            symbol_line = _resolve_symbol(self._root, raw, symbol)
            if symbol_line == 0:
                return json.dumps({"error": "symbol_not_found", "message": f"未找到符号: {symbol} in {path}"}, ensure_ascii=False)

        # 6. 确定读取范围
        if symbol_line > 0:
            # 以符号行为锚点
            ctx = max(context_lines, 0) if context_lines else 0
            if page > 0 and page_size > 0:
                # 分页从符号锚点开始偏移
                anchor = symbol_line + (page - 1) * page_size
                s = max(1, anchor)
                e = min(total, s + page_size - 1)
            else:
                s = max(1, symbol_line - ctx)
                e = min(total, symbol_line + ctx)
        elif page > 0 and page_size > 0:
            s = (page - 1) * page_size + 1
            e = min(total, page * page_size)
        else:
            s = start_line if start_line > 0 else 1
            e = min(end_line, total) if end_line > 0 else total

        # 7. 截取
        selected = lines[s - 1:e]
        output = "".join(selected)

        # 8. 行对齐截断
        truncated = False
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
            nl = output.rfind("\n")
            if nl > 0:
                output = output[:nl]
            truncated = True

        # 9. 构建 JSON 响应
        shown_start = s
        shown_end = s + output.count("\n")
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1
        current_page = page if page > 0 else (shown_start // page_size + 1 if page_size > 0 else 1)

        result = {
            "file": str(raw.relative_to(self._root) if raw.is_relative_to(self._root) else raw),
            "total_lines": total,
            "range": [shown_start, shown_end],
        }
        if page > 0 or symbol_line > 0:
            result["page"] = current_page
            result["total_pages"] = total_pages
            result["has_next"] = shown_end < total
            result["has_prev"] = current_page > 1
        if symbol_line > 0:
            result["symbol_found"] = symbol
            result["symbol_line"] = symbol_line
        if truncated:
            result["truncated"] = True
        result["content"] = output

        return json.dumps(result, ensure_ascii=False)


# ====== 符号解析（跨语言） ======

_LANG_PATTERNS = {
    "go": [
        (r"^func\s+(\w+)\s*\([^)]*\)", "func"),
        (r"^type\s+(\w+)\s+struct", "struct"),
        (r"^type\s+(\w+)\s+interface", "interface"),
        (r"^func\s+\([^)]+\)\s+(\w+)\s*\([^)]*\)", "method"),
    ],
    "rust": [
        (r"^\s*fn\s+(\w+)\s*\([^)]*\)", "fn"),
        (r"^\s*struct\s+(\w+)", "struct"),
        (r"^\s*enum\s+(\w+)", "enum"),
        (r"^\s*trait\s+(\w+)", "trait"),
        (r"^\s*impl\s+(\w+)", "impl"),
        (r"^\s*fn\s+(\w+)\s*\(&?self[^)]*\)", "method"),
    ],
    "java": [
        (r"(public|private|protected)?\s*(static|abstract|final)?\s*(class|interface|enum)\s+(\w+)", "type"),
        (r"(public|private|protected)?\s*(static|final)?\s*\w+\s+(\w+)\s*\([^)]*\)", "method"),
    ],
    "kotlin": [
        (r"^(class|interface|object|enum class)\s+(\w+)", "type"),
        (r"^fun\s+(\w+)\s*\([^)]*\)", "fun"),
    ],
    "typescript": [
        (r"^(export\s+)?(class|interface|type|enum)\s+(\w+)", "type"),
        (r"^(export\s+)?(function|async function)\s+(\w+)", "func"),
        (r"^(export\s+)?(const)\s+(\w+)\s*[:=]\s*\(?[^)]*\)?\s*=>", "const"),
    ],
    "javascript": [
        (r"^(class)\s+(\w+)", "class"),
        (r"^(function|async function)\s+(\w+)", "func"),
        (r"^(const)\s+(\w+)\s*[:=]\s*\(?[^)]*\)?\s*=>", "const"),
    ],
    "swift": [
        (r"^(class|struct|enum|protocol)\s+(\w+)", "type"),
        (r"^func\s+(\w+)\s*\([^)]*\)", "func"),
    ],
    "ruby": [
        (r"^(class|module)\s+(\w+)", "type"),
        (r"^def\s+(\w+)", "def"),
    ],
    "php": [
        (r"^(class|interface|trait|abstract class|final class)\s+(\w+)", "type"),
        (r"function\s+(\w+)\s*\(", "func"),
    ],
    "scala": [
        (r"^(class|object|trait|case class)\s+(\w+)", "type"),
        (r"^def\s+(\w+)\s*\([^)]*\)", "def"),
    ],
}


def _resolve_symbol(root: Path, file_path: Path, symbol: str) -> int:
    """解析符号 → 行号（1-based），未找到返回 0。

    支持语言：
    - Python: AST 解析，精确匹配类/函数/顶层变量
    - Go/Rust/Java/Kotlin/TS/JS/Swift/Ruby/PHP/Scala: 正则匹配
    - Markdown/Config/其他: 纯文本搜索
    """
    suffix = file_path.suffix.lower()
    lang_map = {
        ".py": "python", ".go": "go", ".rs": "rust",
        ".java": "java", ".kt": "kotlin",
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".swift": "swift", ".rb": "ruby",
        ".php": "php", ".scala": "scala",
    }

    try:
        content = str(file_path.read_text(encoding="utf-8" if suffix != ".py" else "utf-8", errors="replace"))
    except Exception:
        return 0

    if suffix == ".py":
        return _resolve_python(content, symbol)
    elif suffix in lang_map:
        return _resolve_regex(content, symbol, _LANG_PATTERNS.get(lang_map[suffix], []))
    else:
        return _resolve_text(content, symbol)


def _resolve_python(source: str, symbol: str) -> int:
    """Python AST 解析符号行号。"""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _resolve_text(source, symbol)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == symbol:
            return node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return node.lineno
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    return node.lineno
    return 0


def _resolve_regex(content: str, symbol: str, patterns: list) -> int:
    """正则匹配符号行号。"""
    lines = content.split("\n")
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*", "///")):
            continue
        for regex, _kind in patterns:
            m = re.search(regex, stripped)
            if m:
                # 检查是否有捕获组匹配 symbol
                for g in m.groups():
                    if g == symbol:
                        return lineno
    return 0


def _resolve_text(content: str, symbol: str) -> int:
    """纯文本搜索符号（适用于 Markdown/配置等）。"""
    lines = content.split("\n")
    for lineno, line in enumerate(lines, 1):
        if symbol in line:
            return lineno
    return 0


class GrepTool(BaseTool):
    """搜索文件内容：grep(pattern, path?, include_glob?, offset?, limit?, context_lines?)"""

    name = "grep"
    description = (
        "在文件中搜索匹配模式。支持正则表达式、分页 (offset/limit) 和上下文行 (context_lines)。"
        "首次了解项目请先使用 project_overview。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "搜索模式（支持 Python 正则表达式）"},
            "path": {"type": "string", "description": "搜索目录或文件路径（可选，默认项目根目录）"},
            "include_glob": {"type": "string", "description": "文件匹配模式（如 *.py），可选"},
            "offset": {"type": "integer", "description": "跳过前 N 条结果（分页），默认 0"},
            "limit": {"type": "integer", "description": "最多返回 N 条结果，默认 200"},
            "context_lines": {"type": "integer", "description": "每条结果前后各 N 行上下文，默认 0"},
        },
        "required": ["pattern"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, pattern: str, path: str = "", include_glob: str = "*.py",
                      offset: int = 0, limit: int = 200, context_lines: int = 0) -> str:
        search_root = (Path(path).resolve() if path else self._root)
        try:
            search_root.relative_to(self._root)
        except ValueError:
            return f"[错误] 搜索路径超出项目根目录: {path}"

        if not search_root.exists():
            return f"[错误] 路径不存在: {path}"

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"[错误] 无效正则: {e}"

        raw_results: list[tuple[str, int, str]] = []  # (file_path, lineno, line_text)
        total_matches = 0

        files = search_root.rglob(include_glob) if search_root.is_dir() else [search_root]
        skip_parts = {".git", ".venv", "__pycache__", "node_modules", ".idea", ".codebuddy"}
        for f in files:
            if not f.is_file() or f.suffix == ".pyc":
                continue
            if any(p in f.parts for p in skip_parts):
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
                    if compiled.search(line):
                        raw_results.append((str(f), i, line.strip()))
                        total_matches += 1
                        if total_matches >= offset + limit:
                            break
            except Exception:
                continue
            if total_matches >= offset + limit:
                break

        if total_matches == 0:
            return f"未找到匹配 '{pattern}' 的结果。"

        # 分页切片
        page = raw_results[offset:offset + limit]

        # 上下文行
        if context_lines > 0:
            output_parts: list[str] = []
            for fpath, lineno, text in page:
                output_parts.append(f"{fpath}:{lineno}: {text}")
                # 尝试读上下文
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        file_lines = f.readlines()
                    ctx_start = max(0, lineno - 1 - context_lines)
                    ctx_end = min(len(file_lines), lineno + context_lines)
                    ctx = file_lines[ctx_start:ctx_end]
                    ctx_out = []
                    for ci, cl in enumerate(ctx, ctx_start + 1):
                        marker = ">" if ci == lineno else " "
                        ctx_out.append(f"  {marker} {ci}:{cl.rstrip()}")
                    output_parts.append("\n".join(ctx_out))
                except Exception:
                    pass
            output = "\n".join(output_parts)
        else:
            output = "\n".join(f"{f}:{l}: {t}" for f, l, t in page)

        # 截断
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
            nl = output.rfind("\n")
            if nl > 0:
                output = output[:nl]
            output += "\n... [输出截断]"

        remaining = total_matches - (offset + limit)
        header = f'[grep] 模式: "{pattern}" | 共 {total_matches} 条匹配 | offset={offset} limit={limit}'
        if remaining > 0:
            header += f"\n... 还有 {remaining} 条，使用 offset={offset + limit} 查看下一页"
        return f"{header}\n{output}"


class ReadFilesTool(BaseTool):
    """批量读取多个文件：read_files(paths, start_line?, end_line?)"""

    name = "read_files"
    description = (
        "同时读取多个文件（批量），每个文件用 === 分隔。"
        "适合需要阅读多个相关文件时减少 LLM 往返。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "文件路径列表（相对于项目根目录或绝对路径）",
            },
            "start_line": {"type": "integer", "description": "起始行号（1-based，可选，所有文件共享）"},
            "end_line": {"type": "integer", "description": "结束行号（1-based，含，可选）"},
        },
        "required": ["paths"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, paths: list, start_line: int = 0, end_line: int = 0) -> str:
        parts: list[str] = []
        total_chars = 0
        for p in paths:
            raw = (Path(p).resolve() if Path(p).is_absolute() else self._root / p).resolve()
            try:
                raw.relative_to(self._root)
            except ValueError:
                parts.append(f"=== {p} ===\n[错误] 访问被拒绝")
                continue
            if not raw.exists():
                parts.append(f"=== {p} ===\n[错误] 文件不存在")
                continue
            if not raw.is_file():
                parts.append(f"=== {p} ===\n[错误] 不是文件")
                continue
            binary_ext = {".pyc", ".so", ".dll", ".png", ".jpg", ".zip", ".tar", ".gz", ".exe", ".o", ".a"}
            if raw.suffix.lower() in binary_ext:
                parts.append(f"=== {p} ===\n[错误] 二进制文件")
                continue
            try:
                with open(raw, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception as e:
                parts.append(f"=== {p} ===\n[错误] 读取失败: {e}")
                continue
            total = len(lines)
            s = start_line if start_line > 0 else 1
            e = min(end_line, total) if end_line > 0 else total
            content = "".join(lines[s - 1:e])
            header = f"=== {p} ({total}行, 行 {s}-{e}) ==="
            block = f"{header}\n{content}"
            if total_chars + len(block) > MAX_OUTPUT_CHARS:
                remaining = MAX_OUTPUT_CHARS - total_chars
                block = block[:remaining] + "\n... [输出截断]"
                parts.append(block)
                parts.append(f"... [还有 {len(paths) - len(parts)} 个文件未读取]")
                break
            parts.append(block)
            total_chars += len(block)
        return "\n\n".join(parts)
