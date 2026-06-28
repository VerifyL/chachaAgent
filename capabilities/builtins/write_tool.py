"""
capabilities/builtins/write_tool.py
WriteTool — 创建或整体覆盖文件。

write 负责整体创建/覆盖，edit 负责精确文本替换。
不备份（git 是更好的备份），不限制 content 大小。
"""

from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool
from capabilities.result import ToolResult


class WriteTool(BaseTool):
    name = "write"
    description = "创建新文件或整体覆盖已有文件"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于项目根目录或绝对路径）",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整文件内容",
            },
        },
        "required": ["path", "content"],
    }

    risk = "medium"

    def __init__(self):
        super().__init__()
        self.project_root: Optional[Path] = None

    async def execute(self, path: str, content: str) -> ToolResult:
        if self.project_root is None:
            return ToolResult(
                status="error",
                content="",
                error="工具未初始化：project_root 未设置",
                error_type="internal_error",
                data={"path": path},
            )

        # 1. 路径解析
        raw = Path(path)
        if not raw.is_absolute():
            raw = self.project_root / raw
        resolved = raw.resolve()

        # 2. 安全：必须在项目根内
        try:
            resolved.relative_to(self.project_root.resolve())
        except ValueError:
            return ToolResult(
                status="error",
                content="",
                error=f"路径越界：{path}",
                error_type="path_out_of_bounds",
                data={"path": path},
            )

        # 3. 路径是目录？
        if resolved.exists() and resolved.is_dir():
            return ToolResult(
                status="error",
                content="",
                error=f"路径是目录：{path}",
                error_type="path_is_directory",
                data={"path": path},
            )

        # 4. 父目录自动创建
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # 5. 写入
        existed = resolved.exists()
        try:
            tmp = resolved.with_suffix(resolved.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(resolved)
        except PermissionError:
            return ToolResult(
                status="error",
                content="",
                error=f"权限不足：{path}",
                error_type="permission_denied",
                data={"path": path},
            )
        except OSError as e:
            return ToolResult(
                status="error",
                content="",
                error=f"写入失败：{e}",
                error_type="io_error",
                data={"path": path},
            )

        # 6. 统计
        lines = content.count("\n") + (1 if content else 0)
        bytes_written = len(content.encode("utf-8"))

        action = "已覆盖" if existed else "已创建"
        return ToolResult(
            status="success",
            content=f"{action} {path}，{lines} 行，{bytes_written} 字节。",
            data={
                "path": path,
                "lines": lines,
                "bytes": bytes_written,
                "created": not existed,
            },
        )
