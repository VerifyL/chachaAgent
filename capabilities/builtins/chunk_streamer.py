"""
capabilities/builtins/chunk_streamer.py
ReadFile + Grep — 文件读写工具（BaseTool）。

用法:
  read_file(path, start_line?, end_line?)  → 读取文件内容
  grep(pattern, path?, include_glob?)       → 搜索文件内容
"""

import logging
import re
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 100_000


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
        self._root = root or Path.cwd()

    async def execute(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        full_path = (Path(path) if Path(path).is_absolute() else self._root / path).resolve()
        if not full_path.exists():
            return f"[错误] 文件不存在: {path}"

        try:
            lines = full_path.read_text(encoding="utf-8").split("\n")
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        if start_line > 0 or end_line > 0:
            start = max(0, start_line - 1) if start_line else 0
            end = end_line if end_line else len(lines)
            lines = lines[start:end]

        output = "\n".join(lines)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [输出截断]"
        return output


class GrepTool(BaseTool):
    """搜索文件内容：grep(pattern, path?, include_glob?)"""

    name = "grep"
    description = "在文件中搜索匹配模式。支持正则表达式，可限制搜索路径和文件范围。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "搜索模式（支持 Python 正则表达式）"},
            "path": {"type": "string", "description": "搜索目录或文件路径（可选，默认项目根目录）"},
            "include_glob": {"type": "string", "description": "文件匹配模式（如 *.py），可选"},
        },
        "required": ["pattern"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = root or Path.cwd()

    async def execute(self, pattern: str, path: str = "", include_glob: str = "*.py") -> str:
        search_root = (Path(path) if path else self._root).resolve()
        if not search_root.exists():
            return f"[错误] 路径不存在: {path}"

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"[错误] 无效正则: {e}"

        results: list[str] = []
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
                        results.append(f"{f}:{i}: {line.strip()}")
                        total_matches += 1
                        if total_matches >= 200:
                            break
            except Exception:
                continue
            if total_matches >= 200:
                break

        if not results:
            return f"未找到匹配 '{pattern}' 的结果。"

        output = "\n".join(results)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [截断，共 {total_matches} 条结果]"
        return output
