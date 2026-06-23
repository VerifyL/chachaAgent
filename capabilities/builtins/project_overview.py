"""
capabilities/builtins/project_overview.py
ProjectOverview — 项目结构总览工具（BaseTool）。

快速了解项目结构、配置、文档，避免 LLM 盲目 read_file/grep。
"""

import logging
import os
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)


class ProjectOverviewTool(BaseTool):
    """获取项目结构总览：project_overview(root?) → 目录树 + README + 关键配置"""

    name = "project_overview"
    description = (
        "获取项目结构总览（目录树 + README + 关键元数据）。"
        "首次了解项目时优先使用此工具。后续探索建议流程：\n"
        "  1. file_outline(关键文件) — 获取文件骨架\n"
        "  2. grep(关键词) — 定位目标\n"
        "  3. read_file(文件, 起始行) — 读具体片段\n"
        "最小化 token 消耗，避免一次性读太多大文件。"
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = root or Path.cwd()

    async def execute(self) -> str:
        lines: list[str] = []
        root = self._root.resolve()

        # 1. 关键元数据
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            for line in text.split("\n")[:15]:
                s = line.strip()
                if s.startswith("name") or s.startswith("version") or s.startswith("description"):
                    lines.append(s)

        # 2. README
        readme = root / "README.md"
        if readme.exists():
            content = readme.read_text(encoding="utf-8").strip()
            lines.append(f"README: {content[:300]}")

        # 3. 目录结构（排除 .venv, __pycache__, node_modules, .git）
        skip_dirs = {".venv", "__pycache__", "node_modules", ".git", ".idea", ".codebuddy"}
        lines.append("目录结构:")
        lines.extend(_tree(root, prefix="  ", max_items=80, skip_dirs=skip_dirs))

        return "\n".join(lines)


def _tree(path: Path, prefix: str = "", max_items: int = 80,
          skip_dirs: set = None, _count: list = None) -> list[str]:
    if _count is None:
        _count = [0]
    if _count[0] >= max_items:
        return ["  ... (截断)"]
    skip_dirs = skip_dirs or {".venv", "__pycache__", "node_modules", ".git"}
    lines: list[str] = []
    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return [f"{prefix}[权限不足]"]
    for name in entries:
        if name in skip_dirs:
            continue
        if name.startswith("."):
            continue
        full = path / name
        if _count[0] >= max_items:
            break
        _count[0] += 1
        if full.is_dir():
            lines.append(f"{prefix}📁 {name}/")
            lines.extend(_tree(full, prefix + "  ", max_items, skip_dirs, _count))
        else:
            lines.append(f"{prefix}📄 {name}")
    return lines
