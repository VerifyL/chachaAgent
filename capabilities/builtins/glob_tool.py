"""glob_tool.py — find files by pattern."""
from pathlib import Path
from capabilities.base import BaseTool
from capabilities.result import ToolResult


class GlobTool(BaseTool):
    """Find files matching a glob pattern."""

    name = "glob"
    description = "按模式查找文件，返回平铺文件列表"

    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，如 **/*.py 或 src/**/*.ts",
            },
            "path": {
                "type": "string",
                "description": "起始目录，默认项目根",
            },
            "max_items": {
                "type": "integer",
                "description": "最大返回条目数，默认 200",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "是否包含隐藏文件（.开头），默认 false",
            },
        },
        "required": ["pattern"],
    }

    _SKIP_DIRS = {
        ".git", ".chacha", "node_modules", "__pycache__",
        ".venv", ".mypy_cache", ".pytest_cache", ".tox", ".eggs", "build",
    }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        max_items: int = 200,
        include_hidden: bool = False,
    ) -> ToolResult:
        """Search for files matching a glob pattern."""
        max_items = int(max_items)
        # 1. Resolve path
        root = Path(path)
        if not root.is_absolute():
            if self.project_root is None:
                return ToolResult(
                    status="error", content="",
                    error="工具未初始化：project_root 未设置",
                    error_type="internal_error",
                )
            root = self.project_root / root

        try:
            target = root.resolve()
        except Exception:
            return ToolResult(
                status="error", content="",
                error=f"路径不存在: {path}",
                error_type="path_not_found",
                data={"path": path},
            )

        if not target.exists():
            return ToolResult(
                status="error", content="",
                error=f"路径不存在: {path}",
                error_type="path_not_found",
                data={"path": path},
            )

        # 2. Safety: ensure within project root
        if self.project_root and not str(target).startswith(
            str(self.project_root.resolve())
        ):
            return ToolResult(
                status="error", content="",
                error="路径越界：不允许访问项目根目录以外的路径",
                error_type="path_out_of_bounds",
                data={"path": path},
            )

        # 3. Is a file? Guide user to grep instead
        if target.is_file():
            return ToolResult(
                status="error", content="",
                error=f"{path} 是文件，搜内容请用 grep",
                error_type="path_is_file",
                data={"path": path},
            )

        # 4. Walk files
        matches = []
        total = 0

        for p in target.glob(pattern):
            if not p.is_file():
                continue

            parts = p.parts

            # Skip hidden files/dirs unless include_hidden
            if not include_hidden:
                if any(part.startswith(".") for part in parts):
                    continue

            # Skip known noise directories
            if any(d in self._SKIP_DIRS for d in parts):
                continue

            total += 1
            if total <= max_items:
                matches.append(str(p.relative_to(target)))

        # 5. Format output
        content = "\n".join(matches)
        if total > max_items:
            content += f"\n\n共 {total} 个文件，仅显示前 {max_items}。请缩小范围。"
        elif total == 0:
            content = f"未找到匹配 {pattern} 的文件"
        else:
            content += f"\n\n共 {total} 个文件"

        return ToolResult(
            status="success",
            content=content,
            data={
                "matches": total,
                "returned": min(total, max_items),
                "pattern": pattern,
                "path": path,
            },
        )
