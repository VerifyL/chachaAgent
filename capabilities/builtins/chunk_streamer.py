"""
capabilities/builtins/chunk_streamer.py
ReadFile + ReadFiles + Grep — 文件读写工具（BaseTool）。

全部基于 stream_reader (mmap 行号索引 + seek 偏移读取):
- read_file(path, offset?, limit?, symbol?, context_lines?)
- read_files(paths, offset?, limit?)
- grep(pattern, path?, include_glob?, offset?, limit?, context_lines?)

与 Claude Code 语义对齐：offset=行号（1-based），limit=行数，next_offset 盲传。
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool
from capabilities.builtins.stream_reader import read_by_offset, read_mmap_lines

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 100_000


class ReadFileTool(BaseTool):
    """读取文件：read_file(path, offset?, limit?, symbol?, context_lines?)"""

    name = "read_file"
    description = (
        "读取文件内容。支持符号跳转（symbol=函数/类名直接定位）、"
        "流式分页（offset+limit 行号偏移）、"
        "关键字搜索（search=搜索词，自动定位并展开上下文）。"
        "文件路径相对于项目根目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于项目根目录或绝对路径）"},
            "offset": {"type": "integer", "description": "起始行号（1-based），默认 1"},
            "limit": {"type": "integer", "description": "最大读取行数，默认 100"},
            "symbol": {"type": "string", "description": "跳转到函数/类/变量定义处。优先级高于 offset"},
            "context_lines": {"type": "integer", "description": "目标（symbol 定位行或指定行）前后各 N 行上下文，默认 0"},
            "search": {"type": "string", "description": "搜索关键词（纯文本匹配），自动定位匹配行并以 context_lines 展开上下文。多结果时返回摘要"},
            "skip_first": {"type": "integer", "description": "search 模式下跳过前 N 条匹配（续读用），默认 0"},
        },
        "required": ["path"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, path: str, offset: int = 1, limit: int = 500,
                      symbol: str = "", context_lines: int = 0,
                      search: str = "", skip_first: int = 0) -> str:
        # 符号跳转优先级最高
        if symbol:
            raw = (Path(path).resolve() if Path(path).is_absolute()
                   else (self._root / path).resolve())
            try:
                raw.relative_to(self._root)
            except ValueError:
                return json.dumps({"error": "access_denied", "message": "路径超出项目根目录"}, ensure_ascii=False)
            if not raw.exists() or not raw.is_file():
                return json.dumps({"error": "not_found", "message": f"文件不存在: {path}"}, ensure_ascii=False)

            symbol_line = _resolve_symbol(self._root, raw, symbol)
            if symbol_line == 0:
                return json.dumps({"error": "symbol_not_found", "message": f"未找到符号: {symbol} in {path}"}, ensure_ascii=False)

            # 以符号行为锚点，应用 context_lines
            ctx = max(context_lines, 0)
            offset = max(1, symbol_line - ctx)
            limit = 2 * ctx + 1

            result_str = read_by_offset(
                str(raw), offset=offset, limit=limit, root=self._root,
            )
            result = json.loads(result_str)
            result["symbol_found"] = symbol
            result["symbol_line"] = symbol_line
            return json.dumps(result, ensure_ascii=False)

        # search 搜索定位（与 offset 互斥）
        if search:
            if offset != 1:
                return json.dumps({
                    "error": "invalid_params",
                    "message": "search 与 offset 不能同时使用",
                }, ensure_ascii=False)

            raw = (Path(path).resolve() if Path(path).is_absolute()
                   else (self._root / path)).resolve()
            try:
                raw.relative_to(self._root)
            except ValueError:
                return json.dumps({
                    "error": "access_denied",
                    "message": "路径超出项目根目录",
                }, ensure_ascii=False)
            if not raw.exists() or not raw.is_file():
                return json.dumps({
                    "error": "not_found",
                    "message": f"文件不存在: {path}",
                }, ensure_ascii=False)

            # Step 1: 先获取匹配行（无上下文），确定匹配数
            result_no_ctx = read_mmap_lines(
                str(raw), search,
                offset=skip_first, limit=200, context_lines=0,
                root=self._root,
            )
            if result_no_ctx is None:
                return json.dumps({
                    "error": "not_found",
                    "message": f"未找到匹配: '{search}' in {path}",
                }, ensure_ascii=False)

            header_no_ctx, output_no_ctx = result_no_ctx
            mcnt = re.search(r"共 (\d+) 条匹配", header_no_ctx)
            total = int(mcnt.group(1)) if mcnt else 0

            if total == 0:
                return json.dumps({
                    "error": "not_found",
                    "message": f"未找到匹配: '{search}' in {path}",
                }, ensure_ascii=False)

            # 单条匹配 → 像 symbol 模式一样用行号锚定
            if total == 1:
                first_line = output_no_ctx.split("\n")[0]
                parts = first_line.split(":", 2)
                if len(parts) >= 2:
                    try:
                        match_lineno = int(parts[1])
                    except ValueError:
                        match_lineno = 1
                else:
                    match_lineno = 1

                ctx = max(context_lines, 0)
                actual_offset = max(1, match_lineno - ctx)
                actual_limit = 2 * ctx + 1 if ctx > 0 else 1

                result_str = read_by_offset(
                    str(raw), offset=actual_offset, limit=actual_limit, root=self._root,
                )
                result = json.loads(result_str)
                result["search"] = search
                result["match_line"] = match_lineno
                result["total_matches"] = 1
                return json.dumps(result, ensure_ascii=False)

            # 少量匹配 → 带上下文完整展开
            if total <= 10:
                result_ctx = read_mmap_lines(
                    str(raw), search,
                    offset=skip_first, limit=total, context_lines=context_lines,
                    root=self._root,
                )
                if result_ctx is None:
                    header, output = header_no_ctx, output_no_ctx
                else:
                    header, output = result_ctx
                return f"{header}\n{output}"

            # 大量匹配 → 返回摘要，引导 LLM 展开具体匹配
            lines = output_no_ctx.split("\n")
            display_count = min(20, len(lines))
            summary_parts = [
                f'[search] "{search}" | 共 {total} 条匹配 | 显示前 {display_count} 条摘要:',
            ]
            for line in lines[:display_count]:
                summary_parts.append(line)

            if total > display_count:
                summary_parts.append(
                    f"\n... 还有 {total - display_count} 条匹配。"
                    f'用 read_file(path="{path}", search="{search}", skip_first={display_count}, context_lines=5) 查看更多，'
                    f"或用 read_file(path=\"{path}\", offset=行号, context_lines=5) 展开具体匹配。"
                )

            return "\n".join(summary_parts)

        # 正常读取 — context_lines 与 offset 配合
        if context_lines > 0:
            actual_offset = max(1, offset - context_lines)
            actual_limit = 2 * context_lines + 1
            return read_by_offset(
                path, offset=actual_offset, limit=actual_limit, root=self._root,
            )
        return read_by_offset(
            path, offset=offset, limit=limit, root=self._root,
        )


# ====== 符号解析（跨语言，使用共享模式） ======

from capabilities.builtins.lang_patterns import (
    LANG_MAP, LANG_PATTERNS, get_lang, resolve_regex, resolve_text,
)


def _resolve_symbol(root: Path, file_path: Path, symbol: str) -> int:
    """解析符号 → 行号（1-based），未找到返回 0。"""
    suffix = file_path.suffix.lower()

    try:
        content = str(file_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return 0

    if suffix == ".py":
        return _resolve_python(content, symbol)
    lang = get_lang(suffix)
    if lang and lang in LANG_PATTERNS:
        return resolve_regex(content, symbol, LANG_PATTERNS[lang])
    else:
        return resolve_text(content, symbol)


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
            re.compile(pattern)
        except re.error as e:
            return f"[错误] 无效正则: {e}"

        files = search_root.rglob(include_glob) if search_root.is_dir() else [search_root]
        skip_parts = {".git", ".venv", "__pycache__", "node_modules", ".idea", ".codebuddy"}

        combined_header = ""
        combined_output_parts: list[str] = []
        total_all_matches = 0

        for f in files:
            if not f.is_file() or f.suffix == ".pyc":
                continue
            if any(p in f.parts for p in skip_parts):
                continue

            result = read_mmap_lines(
                str(f), pattern,
                offset=offset, limit=limit, context_lines=context_lines,
                root=self._root,
            )
            if result is None:
                continue

            header, output = result
            # 从 header 提取总数
            mcnt = re.search(r"共 (\d+) 条匹配", header)
            if mcnt:
                total_all_matches += int(mcnt.group(1))

            if not combined_header:
                combined_header = header
            combined_output_parts.append(output)

            if total_all_matches >= offset + limit:
                break

        if not combined_output_parts:
            return f"未找到匹配 '{pattern}' 的结果。"

        output = "\n".join(combined_output_parts)

        # 截断
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
            nl = output.rfind("\n")
            if nl > 0:
                output = output[:nl]
            output += "\n... [输出截断]"

        remaining = total_all_matches - (offset + limit)
        header = f'[grep] 模式: "{pattern}" | 共 {total_all_matches} 条匹配 | offset={offset} limit={limit}'
        if remaining > 0:
            header += f"\n... 还有 {remaining} 条，使用 offset={offset + limit} 查看下一页"
        return f"{header}\n{output}"


class ReadFilesTool(BaseTool):
    """批量读取多个文件：read_files(paths, offset?, limit?)"""

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
            "offset": {"type": "integer", "description": "起始行号（1-based，所有文件共享），默认 1"},
            "limit": {"type": "integer", "description": "最大读取行数，默认 200"},
        },
        "required": ["paths"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, paths: list, offset: int = 1, limit: int = 200) -> str:
        parts: list[str] = []
        total_chars = 0
        for p in paths:
            result_str = read_by_offset(
                p, offset=offset, limit=limit, root=self._root,
            )
            result = json.loads(result_str)

            if "error" in result:
                err_msg = result.get("message", result["error"])
                block = f"=== {p} ===\n[错误] {err_msg}"
            else:
                header = f"=== {p} ({result['total_lines']}行, 行 {result['offset']}-{result['offset'] + result['lines_read'] - 1}) ==="
                block = f"{header}\n{result['content']}"

            if total_chars + len(block) > MAX_OUTPUT_CHARS:
                remaining = MAX_OUTPUT_CHARS - total_chars
                block = block[:remaining] + "\n... [输出截断]"
                parts.append(block)
                parts.append(f"... [还有 {len(paths) - len(parts)} 个文件未读取]")
                break
            parts.append(block)
            total_chars += len(block)
        return "\n\n".join(parts)
