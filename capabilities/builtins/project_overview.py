"""
capabilities/builtins/project_overview.py
ProjectOverview — 项目结构总览工具（BaseTool）。

快速了解项目结构、配置、文档，避免 LLM 盲目 read_file/grep。
Phase 3: 目录树自动标注 git 状态 (M=已修改, A=已暂存, ??=未跟踪, D=已删除)
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 3.0


def _get_git_status_map(root: Path) -> Dict[str, str]:
    """采集 git status --short，返回 {相对路径: 状态码} 映射。"""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(root),
            capture_output=True, text=True,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            return {}
        status_map: Dict[str, str] = {}
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            code = line[:2].strip()
            file_path = line[3:].strip()
            status_map[file_path] = code
        return status_map
    except Exception:
        return {}


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
        "properties": {
            "max_items": {
                "type": "integer",
                "description": "目录树最多显示条目数，默认 200",
                "default": 200
            },
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = root or Path.cwd()

    async def execute(self, max_items: int = 200) -> str:
        """执行项目概览，max_items 控制目录树最多显示条目数（默认 200，最大 1000）。"""
        lines: list[str] = []
        root = self._root.resolve()

        # 硬上限 1000，防止极端情况
        max_items = min(max(max_items, 1), 1000)

        # 0. git 状态映射（用于文件标注）
        git_status = _get_git_status_map(root)

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
        lines.append(f"目录结构 (M=已修改 A=已暂存 ??=未跟踪, 最多 {max_items} 条目):")
        lines.extend(_tree(root, prefix="  ", max_items=max_items, skip_dirs=skip_dirs,
                           git_status=git_status, root=root))

        return "\n".join(lines)


def _tree(path: Path, prefix: str = "", max_items: int = 200,
          skip_dirs: set = None, _count: list = None,
          git_status: Dict[str, str] = None, root: Path = None) -> list[str]:
    if _count is None:
        _count = [0]
    if _count[0] >= max_items:
        return [f"  ... (达到 {max_items} 条上限，使用 max_items 参数增加)"]
    skip_dirs = skip_dirs or {".venv", "__pycache__", "node_modules", ".git"}
    git_status = git_status or {}
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

        # 计算 git 状态标注
        status_tag = ""
        if root:
            try:
                rel = str(full.relative_to(root))
                code = git_status.get(rel, "")
                if code:
                    status_tag = f" [{code}]"
            except ValueError:
                pass

        if full.is_dir():
            lines.append(f"{prefix}📁 {name}/{status_tag}")
            lines.extend(_tree(full, prefix + "  ", max_items, skip_dirs, _count,
                              git_status=git_status, root=root))
        else:
            lines.append(f"{prefix}📄 {name}{status_tag}")
    return lines
