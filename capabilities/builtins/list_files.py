"""
capabilities/builtins/list_files.py
ListFiles — 目录列表工具。

返回目录结构树，支持 glob 过滤和深度控制。
"""

import logging
import os
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 500
_DEFAULT_MAX_DEPTH = 2
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "node_modules", ".idea", ".codebuddy"})


class ListFilesTool(BaseTool):
    """列出目录结构：list_files(path?, max_depth?, pattern?, include_hidden?)"""

    name = "list_files"
    description = "列出项目目录结构，支持 glob 过滤和深度控制。返回树形结构，每行标注 [dir]/[file]。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径（相对或绝对），默认项目根目录"},
            "max_depth": {"type": "integer", "description": "最大递归深度，默认 2"},
            "pattern": {"type": "string", "description": "glob 过滤模式（如 *.py），可选"},
            "include_hidden": {"type": "boolean", "description": "是否显示隐藏文件，默认 false"},
            "git_status": {"type": "boolean", "description": "是否标注文件 git 状态（M/A/??/D），默认 false"},
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, path: str = "", max_depth: int = _DEFAULT_MAX_DEPTH,
                      pattern: str = "", include_hidden: bool = False) -> str:
        search_root = (Path(path).resolve() if path else self._root)
        try:
            search_root.relative_to(self._root)
        except ValueError:
            return "[错误] 路径超出项目根目录"
        if not search_root.exists():
            return f"[错误] 路径不存在: {path}"
        if not search_root.is_dir():
            return f"[错误] 不是目录: {path}"

        lines: list[str] = []
        lines.append(f"[目录] {search_root.relative_to(self._root) if search_root != self._root else '.'}/")
        _count = [0]

        def _walk(current: Path, depth: int, prefix: str):
            if _count[0] >= _MAX_ENTRIES:
                return
            if depth > max_depth:
                return
            try:
                entries = sorted(os.listdir(current))
            except PermissionError:
                lines.append(f"{prefix}[权限不足]")
                return

            for name in entries:
                if _count[0] >= _MAX_ENTRIES:
                    break
                if not include_hidden and name.startswith("."):
                    continue
                if name in _SKIP_DIRS:
                    continue
                full = current / name
                _count[0] += 1

                if pattern and not full.match(pattern) and not full.is_dir():
                    if depth < max_depth and full.is_dir():
                        pass
                    else:
                        continue

                is_last = (name == entries[-1])
                branch = "└── " if is_last else "├── "
                if full.is_dir():
                    lines.append(f"{prefix}{branch}📁 {name}/")
                    ext = "    " if is_last else "│   "
                    _walk(full, depth + 1, prefix + ext)
                elif full.is_symlink():
                    lines.append(f"{prefix}{branch}🔗 {name}")
                else:
                    size = full.stat().st_size
                    size_str = f"{size // 1024}KB" if size > 1024 else f"{size}B"
                    lines.append(f"{prefix}{branch}📄 {name} ({size_str})")

        _walk(search_root, 0, "")
        if _count[0] >= _MAX_ENTRIES:
            lines.append(f"... [达到上限 {_MAX_ENTRIES} 条]")
        return "\n".join(lines)
