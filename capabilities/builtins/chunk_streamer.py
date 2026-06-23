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
    description = "读取文件内容。可指定行范围避免输出过大。文件路径相对于项目根目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于项目根目录或绝对路径）"},
            "start_line": {"type": "integer", "description": "起始行号（1-based，可选）"},
            "end_line": {"type": "integer", "description": "结束行号（1-based，含），可选"},
        },
        "required": ["path"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        # 1. 路径解析 + containment 检查
        raw = (Path(path).resolve() if Path(path).is_absolute()
               else (self._root / path).resolve())
        try:
            raw.relative_to(self._root)
        except ValueError:
            return "[错误] 访问被拒绝: 路径超出项目根目录"

        if not raw.exists():
            return f"[错误] 文件不存在: {path}"
        if not raw.is_file():
            return f"[错误] 不是文件: {path}"

        # 2. 大小检查（避免 OOM）
        try:
            fsize = raw.stat().st_size
        except OSError as e:
            return f"[错误] 无法访问文件: {e}"
        if fsize > MAX_FILE_SIZE:
            mb = fsize / 1024 / 1024
            return (f"[错误] 文件过大 ({mb:.1f}MB)，"
                    f"超过 {MAX_FILE_SIZE // 1024 // 1024}MB 限制。请用 grep 搜索。")

        # 3. 二进制检测
        if raw.suffix.lower() in _BINARY_EXTS:
            return f"[错误] 二进制文件: {path}"

        # 4. 逐行读取 + 范围过滤
        try:
            with open(raw, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except PermissionError:
            return f"[错误] 权限不足: {path}"
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        total = len(lines)
        s = start_line if start_line > 0 else 1
        e = min(end_line, total) if end_line > 0 else total

        selected = lines[s - 1:e]
        output = "".join(selected)

        # 5. 行对齐截断
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
            nl = output.rfind("\n")
            if nl > 0:
                output = output[:nl]
            output += "\n... [输出截断]"

        # 6. 结构化元数据前缀
        shown_lines = output.count("\n")
        meta = f"[文件] {raw.name} | {total}行 | {fsize // 1024}KB | 行 {s}-{s + shown_lines - 1}"
        content = output.strip()
        if not content:
            return f"{meta}\n[文件为空]"
        return f"{meta}\n{content}"


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
