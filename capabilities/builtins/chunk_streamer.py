"""
capabilities/builtins/chunk_streamer.py
ReadFile + ReadFiles + Grep — 文件读写工具（BaseTool）。

核心设计（v2，无 mmap）：
- read_file: 纯 seek+read 按行号读取。每次 f.read() 全量建行偏移列表
- grep: 系统 ripgrep 子进程 → grep → Python re 逐级降级
- read_files: 批量 read_file

与 Claude Code 语义对齐：offset=行号（1-based），limit=行数，next_offset 盲传。
"""

import json
import logging
import os
import re
import subprocess
import fnmatch
from pathlib import Path
from typing import Optional, List, Tuple

from capabilities.base import BaseTool
from capabilities.builtins.lang_patterns import (
    LANG_MAP, LANG_PATTERNS, get_lang, resolve_regex, resolve_text,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 100_000
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB，与 edit_file 对齐


_BINARY_EXTS = frozenset({
    ".pyc", ".so", ".dll", ".dylib", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".bin", ".dat", ".db", ".sqlite", ".o", ".a", ".lib",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
})

_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".idea",
              ".codebuddy", ".chacha_agent", ".mypy_cache", ".pytest_cache"}

# ripgrep 缺失时仅警告一次，避免每次 grep 调用都刷日志
_rg_warned = False


# ====== 核心：按行号偏移读取 ======

def _read_by_offset(
    path: str,
    offset: int = 1,
    limit: int = 100,
    root: Optional[Path] = None,
) -> str:
    """按行号偏移读取文件（无索引缓存，单次内存扫描）。返回 JSON 字符串。

    与旧版 stream_reader.read_by_offset 签名和输出格式完全兼容。
    """
    # 防御：LLM streaming 可能传字符串（如 "330"），强制转换为整数
    offset = int(offset)
    limit = int(limit)
    root = root or Path.cwd().resolve()

    # 1. 路径解析
    raw = (Path(path).resolve() if Path(path).is_absolute()
           else (root / path).resolve())
    try:
        raw.relative_to(root)
    except ValueError:
        return json.dumps(
            {"error": "access_denied", "message": "路径超出项目根目录"},
            ensure_ascii=False,
        )
    if not raw.exists():
        return json.dumps(
            {"error": "not_found", "message": f"文件不存在: {path}"},
            ensure_ascii=False,
        )
    if not raw.is_file():
        return json.dumps(
            {"error": "not_a_file", "message": f"不是文件: {path}"},
            ensure_ascii=False,
        )

    # 2. 大小检查
    try:
        fsize = raw.stat().st_size
    except OSError as e:
        return json.dumps(
            {"error": "access_error", "message": str(e)},
            ensure_ascii=False,
        )
    if fsize > MAX_FILE_SIZE:
        return json.dumps({
            "error": "file_too_large",
            "file": str(raw.relative_to(root)),
            "size_mb": round(fsize / 1024 / 1024, 1),
            "message": "文件过大，请用 grep 搜索关键词定位，或用 offset 参数分页读取",
        }, ensure_ascii=False)

    # 3. 二进制检测
    if raw.suffix.lower() in _BINARY_EXTS:
        return json.dumps(
            {"error": "binary_file", "message": f"二进制文件: {path}"},
            ensure_ascii=False,
        )

    # 4. 读全文件 + 建行偏移索引
    try:
        with open(raw, "rb") as f:
            data = f.read()
    except PermissionError:
        return json.dumps(
            {"error": "permission_denied", "message": f"权限不足: {path}"},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"error": "read_error", "message": str(e)},
            ensure_ascii=False,
        )

    # 扫描换行符建偏移列表
    offsets = [0]
    for i, byte_val in enumerate(data):
        if byte_val == 0x0A:  # \n
            offsets.append(i + 1)

    total_lines = len(offsets)

    # 空文件
    if total_lines <= 1 and fsize == 0:
        rel = str(raw.relative_to(root) if raw.is_relative_to(root) else raw)
        return json.dumps({
            "file": rel, "offset": 1, "lines_read": 0,
            "next_offset": 1, "total_lines": 0, "total_bytes": 0,
            "has_more": False, "truncated": False, "content": "",
        }, ensure_ascii=False)

    # 5. 行号 → 字节范围
    start_idx = max(0, offset - 1)
    if start_idx >= len(offsets):
        rel = str(raw.relative_to(root) if raw.is_relative_to(root) else raw)
        return json.dumps({
            "error": "offset_out_of_range",
            "message": f"offset {offset} 超出文件行数范围（共 {total_lines} 行），请调小 offset",
            "file": rel, "total_lines": total_lines, "total_bytes": fsize,
        }, ensure_ascii=False)

    byte_start = offsets[start_idx]

    end_idx_pos = start_idx + limit
    if end_idx_pos >= len(offsets):
        byte_end = fsize
        next_offset = offset + (len(offsets) - start_idx - 1)
        has_more = False
    else:
        byte_end = offsets[end_idx_pos]
        next_offset = offset + limit
        has_more = True

    # 6. 切片 + 解码
    raw_bytes = data[byte_start:byte_end]
    del data  # 尽早释放全量内存
    content = raw_bytes.decode("utf-8", errors="replace")
    lines_read = content.count("\n")

    # 7. 截断
    truncated = False
    if len(content) > MAX_OUTPUT_CHARS:
        content = content[:MAX_OUTPUT_CHARS]
        nl = content.rfind("\n")
        if nl > 0:
            content = content[:nl]
        lines_read = content.count("\n")
        content += f"\n... [截断。使用 offset={next_offset} 续读，或用 search 关键词缩小范围]"
        truncated = True

    rel = str(raw.relative_to(root) if raw.is_relative_to(root) else raw)
    end_line = offset + lines_read - 1
    content = f"[第 {offset}-{end_line} 行 / 共 {total_lines} 行]\n\n{content}"

    return json.dumps({
        "file": rel, "offset": offset, "lines_read": lines_read,
        "next_offset": next_offset, "total_lines": total_lines,
        "total_bytes": fsize, "has_more": has_more,
        "truncated": truncated, "content": content,
    }, ensure_ascii=False)


# ====== 符号解析 ======

def _resolve_symbol(root: Path, file_path: Path, symbol: str) -> int:
    """解析符号 → 行号（1-based），未找到返回 0。"""
    suffix = file_path.suffix.lower()
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
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
        return resolve_text(source, symbol)
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


# ====== 文件内搜索（read_file search 模式用） ======

def _search_in_file(
    file_path: Path, pattern: str, skip_first: int = 0,
    context_lines: int = 0, root: Optional[Path] = None,
) -> Optional[str]:
    """在单个文件中搜索纯文本关键词。返回结果字符串。"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    lines = content.splitlines()
    matches = []
    for i, line in enumerate(lines, 1):
        if pattern in line:  # 纯文本搜索（非正则）
            matches.append((i, line))

    total = len(matches)
    if total == 0:
        return None

    page = matches[skip_first:skip_first + 200]
    if not page:
        return None

    parts = []
    for lineno, text in page:
        if context_lines > 0:
            ctx_start = max(1, lineno - context_lines)
            ctx_end = min(len(lines), lineno + context_lines)
            parts.append(f"{file_path}:{lineno}: {text}")
            for ci in range(ctx_start, ctx_end + 1):
                if ci == lineno:
                    continue
                marker = ">"
                parts.append(f"  {marker} {ci}:{lines[ci - 1]}")
        else:
            parts.append(f"{file_path}:{lineno}: {text}")

    header = f'[search] "{pattern}" | 共 {total} 条匹配 | skip_first={skip_first}'
    if total > skip_first + 200:
        header += f"\n... 还有 {total - skip_first - 200} 条，用 skip_first={skip_first + 200} 续读"

    return f"{header}\n" + "\n".join(parts)


# ====== grep 实现（ripgrep → grep → Python re 三级降级） ======

def _run_grep(
    pattern: str,
    search_root: Path,
    include_glob: str,
    offset: int,
    limit: int,
    context_lines: int,
) -> str:
    """搜索文件。ripgrep → system grep → Python re 逐级降级。"""
    # 防御：LLM streaming 可能传字符串，强制转换
    offset = int(offset)
    limit = int(limit)
    context_lines = int(context_lines)
    result = _try_ripgrep(pattern, search_root, include_glob,
                          offset, limit, context_lines)
    if result is not None:
        return result
    result = _try_system_grep(pattern, search_root, include_glob,
                              offset, limit, context_lines)
    if result is not None:
        return result
    return _python_grep(pattern, search_root, include_glob,
                        offset, limit, context_lines)


def _try_ripgrep(pattern, search_root, include_glob, offset, limit, context_lines):
    """尝试 ripgrep 子进程。"""
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never"]
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    if include_glob and include_glob != "*":
        cmd.extend(["--glob", include_glob])
    cmd.extend(["--", pattern, str(search_root)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode > 1 or not result.stdout.strip():
            return None
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        global _rg_warned
        if not _rg_warned:
            _rg_warned = True
            logger.warning("ripgrep (rg) 不可用，已降级为 Python/grep 搜索。安装 rg 可大幅加速：brew install ripgrep")
        return None

    return _paginate_grep_output(output, pattern, offset, limit)


def _try_system_grep(pattern, search_root, include_glob, offset, limit, context_lines):
    """尝试系统 grep 子进程。"""
    cmd = ["grep", "-rnE", "--include", include_glob or "*"]
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    cmd.extend(["--", pattern, str(search_root)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode > 1 or not result.stdout.strip():
            return None
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None

    return _paginate_grep_output(output, pattern, offset, limit)


def _paginate_grep_output(output: str, pattern: str, offset: int, limit: int) -> str:
    """分页 grep 输出并加头部信息。"""
    # 防御：LLM streaming 可能传字符串，强制转换
    offset = int(offset)
    limit = int(limit)
    lines = output.splitlines()
    total = len(lines)
    page = lines[offset:offset + limit]

    result = "\n".join(page)
    if len(result) > MAX_OUTPUT_CHARS:
        result = result[:MAX_OUTPUT_CHARS]
        nl = result.rfind("\n")
        if nl > 0:
            result = result[:nl]
        result += "\n... [输出截断，请缩小 grep 模式或减少 context_lines]"

    header = f'[grep] 模式: "{pattern}" | 共 {total} 条匹配 | offset={offset} limit={limit}'
    remaining = total - (offset + limit)
    if remaining > 0:
        header += f"\n... 还有 {remaining} 条，使用 offset={offset + limit} 查看下一页"

    return f"{header}\n{result}"


def _python_grep(pattern, search_root, include_glob, offset, limit, context_lines):
    """Python 正则搜索（最后兜底）。"""
    compiled = re.compile(pattern)
    
    files = _collect_files(search_root, include_glob)
    
    all_matches = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if compiled.search(line):
                all_matches.append((str(f), i, line))
    
    total = len(all_matches)
    if total == 0:
        return f'未找到匹配 "{pattern}" 的结果。'
    
    page = all_matches[offset:offset + limit]
    
    parts = []
    for fpath, lineno, text in page:
        if context_lines > 0:
            parts.append(f"{fpath}:{lineno}: {text}")
        else:
            parts.append(f"{fpath}:{lineno}: {text}")
    
    output = "\n".join(parts)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS]
        nl = output.rfind("\n")
        if nl > 0:
            output = output[:nl]
        output += "\n... [输出截断，使用更精确的搜索词或减少 context_lines]"
    
    header = f'[grep] 模式: "{pattern}" | 共 {total} 条匹配 | offset={offset} limit={limit}'
    remaining = total - (offset + limit)
    if remaining > 0:
        header += f"\n... 还有 {remaining} 条，使用 offset={offset + limit} 查看下一页"
    
    return f"{header}\n{output}"


def _collect_files(search_root: Path, include_glob: str) -> List[Path]:
    """收集待搜索文件。"""
    if search_root.is_file():
        return [search_root]
    files = []
    for f in search_root.rglob("*"):
        if not f.is_file():
            continue
        if any(p in _SKIP_DIRS for p in f.parts):
            continue
        if include_glob and include_glob != "*":
            if not fnmatch.fnmatch(f.name, include_glob):
                continue
        files.append(f)
    return files


# ====== 工具类 ======

class ReadFileTool(BaseTool):
    """读取文件：read_file(path, offset?, limit?, symbol?, context_lines?, search?, skip_first?)"""

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
        # 防御：LLM streaming 可能传字符串，强制转换
        offset = int(offset)
        limit = int(limit)
        context_lines = int(context_lines)
        skip_first = int(skip_first)
        # 符号跳转优先级最高
        if symbol:
            raw = (Path(path).resolve() if Path(path).is_absolute()
                   else (self._root / path).resolve())
            try:
                raw.relative_to(self._root)
            except ValueError:
                return json.dumps(
                    {"error": "access_denied", "message": "路径超出项目根目录"},
                    ensure_ascii=False,
                )
            if not raw.exists() or not raw.is_file():
                return json.dumps(
                    {"error": "not_found", "message": f"文件不存在: {path}"},
                    ensure_ascii=False,
                )

            symbol_line = _resolve_symbol(self._root, raw, symbol)
            if symbol_line == 0:
                return json.dumps(
                    {"error": "symbol_not_found",
                     "message": f"未找到符号: {symbol} in {path}"},
                    ensure_ascii=False,
                )

            ctx = max(context_lines, 0)
            calc_offset = max(1, symbol_line - ctx)
            calc_limit = 2 * ctx + 1

            result_str = _read_by_offset(
                str(raw), offset=calc_offset, limit=calc_limit, root=self._root,
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
                    "error": "access_denied", "message": "路径超出项目根目录",
                }, ensure_ascii=False)
            if not raw.exists() or not raw.is_file():
                return json.dumps({
                    "error": "not_found", "message": f"文件不存在: {path}",
                }, ensure_ascii=False)

            # Step 1: 先获取匹配行（无上下文），确定匹配数
            result_no_ctx = _search_in_file(raw, search, skip_first=skip_first,
                                            context_lines=0)
            if result_no_ctx is None:
                return json.dumps({
                    "error": "not_found",
                    "message": f"未找到匹配: '{search}' in {path}",
                }, ensure_ascii=False)

            # 从 header 提取总数
            mcnt = re.search(r"共 (\d+) 条匹配", result_no_ctx)
            total = int(mcnt.group(1)) if mcnt else 0

            if total == 0:
                return json.dumps({
                    "error": "not_found",
                    "message": f"未找到匹配: '{search}' in {path}",
                }, ensure_ascii=False)

            # 单条匹配 → 像 symbol 模式一样用行号锚定
            if total == 1:
                lines_out = result_no_ctx.split("\n")
                # 找到第一条非 header 的行
                first_match_line = ""
                for l in lines_out:
                    if ":" in l and not l.startswith("[search]"):
                        first_match_line = l
                        break
                parts = first_match_line.split(":", 2)
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

                result_str = _read_by_offset(
                    str(raw), offset=actual_offset, limit=actual_limit,
                    root=self._root,
                )
                result = json.loads(result_str)
                result["search"] = search
                result["match_line"] = match_lineno
                result["total_matches"] = 1
                return json.dumps(result, ensure_ascii=False)

            # 少量匹配 → 带上下文完整展开
            if total <= 10 and context_lines > 0:
                result_ctx = _search_in_file(
                    raw, search, skip_first=skip_first,
                    context_lines=context_lines,
                )
                return result_ctx or result_no_ctx

            # 大量匹配 → 返回摘要
            lines_out = result_no_ctx.split("\n")
            display_count = min(20, len([l for l in lines_out if not l.startswith("[search]")]))
            summary_parts = [
                f'[search] "{search}" | 共 {total} 条匹配 | 显示前 {display_count} 条摘要:',
            ]
            for line in lines_out:
                if line.startswith("[search]"):
                    continue
                if len(summary_parts) - 1 >= display_count:
                    break
                summary_parts.append(line)

            if total > display_count:
                summary_parts.append(
                    f"\n... 还有 {total - display_count} 条匹配。"
                    f'用 read_file(path="{path}", search="{search}", skip_first={display_count}, context_lines=5) 查看更多，'
                    f'或用 read_file(path="{path}", offset=行号, context_lines=5) 展开具体匹配。'
                )

            return "\n".join(summary_parts)

        # 正常读取 — context_lines 与 offset 配合
        if context_lines > 0:
            actual_offset = max(1, offset - context_lines)
            actual_limit = 2 * context_lines + 1
            return _read_by_offset(
                path, offset=actual_offset, limit=actual_limit, root=self._root,
            )
        return _read_by_offset(
            path, offset=offset, limit=limit, root=self._root,
        )


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

    async def execute(self, pattern: str, path: str = "",
                      include_glob: str = "*.py",
                      offset: int = 0, limit: int = 200,
                      context_lines: int = 0) -> str:
        # 防御：LLM streaming 可能传字符串，强制转换
        offset = int(offset)
        limit = int(limit)
        context_lines = int(context_lines)
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

        return _run_grep(pattern, search_root, include_glob,
                         offset, limit, context_lines)


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
        # 防御：LLM streaming 可能传字符串，强制转换
        offset = int(offset)
        limit = int(limit)
        parts: list = []
        total_chars = 0
        for p in paths:
            result_str = _read_by_offset(
                p, offset=offset, limit=limit, root=self._root,
            )
            result = json.loads(result_str)

            if "error" in result:
                err_msg = result.get("message", result["error"])
                block = f"=== {p} ===\n[错误] {err_msg}"
            else:
                header = (f"=== {p} ({result['total_lines']}行, "
                          f"行 {result['offset']}-"
                          f"{result['offset'] + result['lines_read'] - 1}) ===")
                block = f"{header}\n{result['content']}"

            if total_chars + len(block) > MAX_OUTPUT_CHARS:
                remaining = MAX_OUTPUT_CHARS - total_chars
                block = block[:remaining] + "\n... [输出截断，减少一次读取的文件数量]"
                parts.append(block)
                parts.append(f"... [还有 {len(paths) - len(parts)} 个文件未读取]")
                break
            parts.append(block)
            total_chars += len(block)
        return "\n\n".join(parts)
