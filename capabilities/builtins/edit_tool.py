"""
edit_tool.py — EditTool: 精确文本替换（外科手术式编辑）。

old_string 精确匹配，默认替换第一处，replace_all 全部替换。
"""

from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool
from capabilities.result import ToolResult


class EditTool(BaseTool):
    """精确文本替换，默认替换第一处匹配。"""

    name = "edit"
    description = (
        "精确替换文件中的文本。old_string 必须精确匹配（含空白符），"
        "默认替换第一处，replace_all=true 全部替换。"
        "0 处匹配报错。不备份。"
        "old_string 与 new_string 必须不同，完全相同为无效调用。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于项目根目录或绝对路径）",
            },
            "old_string": {
                "type": "string",
                "description": "要替换的原文本（必须精确匹配，含空白符和缩进）",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的新文本（必须与 old_string 不同）",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换所有匹配项（默认仅替换第一处）",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    risk = "high"

    MAX_FILE_SIZE = 20 * 1024 * 1024
    BINARY_CHECK_BYTES = 512
    _NULL = bytes([0])

    def __init__(self):
        super().__init__()
        self.project_root: Optional[Path] = None

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        if self.project_root is None:
            return ToolResult(
                status="error", content="",
                error="工具未初始化：project_root 未设置",
                error_type="internal_error",
                data={"path": path},
            )
        return self._execute(path, old_string, new_string, replace_all)

    def _execute(
        self, path: str, old_string: str, new_string: str, replace_all: bool
    ) -> ToolResult:
        if not old_string:
            return ToolResult(
                status="error", content="",
                error="old_string 不能为空",
                error_type="old_string_empty",
                data={"path": path},
            )
        if old_string == new_string:
            preview = old_string[:120] + ("..." if len(old_string) > 120 else "")
            return ToolResult(
                status="error", content="",
                error=(
                    f"old_string 与 new_string 完全相同。"
                    f"这通常是参数构造时的复制粘贴错误——"
                    f"new_string 被误填成了 old_string 的内容。"
                    f"请确认 new_string 为修改后的目标文本。"
                    f"\n相同文本预览：{preview}"
                ),
                error_type="old_equals_new",
                data={"path": path, "identical_preview": preview},
            )

        raw = Path(path)
        if not raw.is_absolute():
            raw = self.project_root / raw
        resolved = raw.resolve()

        try:
            resolved.relative_to(self.project_root.resolve())
        except ValueError:
            return ToolResult(
                status="error", content="",
                error=f"路径越界：{path}",
                error_type="path_out_of_bounds",
                data={"path": path},
            )

        if not resolved.exists():
            return ToolResult(
                status="error", content="",
                error=f"文件不存在：{path}",
                error_type="file_not_found",
                data={"path": path},
            )
        if not resolved.is_file():
            return ToolResult(
                status="error", content="",
                error=f"路径不是文件：{path}",
                error_type="invalid_argument",
                data={"path": path},
            )

        size = resolved.stat().st_size
        if size > self.MAX_FILE_SIZE:
            return ToolResult(
                status="error", content="",
                error=f"文件过大 ({size / 1024 / 1024:.1f}MB)，最大支持 {self.MAX_FILE_SIZE / 1024 / 1024:.0f}MB",
                error_type="file_too_large",
                data={"path": path, "size_bytes": size},
            )

        head = resolved.read_bytes()[:self.BINARY_CHECK_BYTES]
        if self._NULL in head:
            return ToolResult(
                status="error", content="",
                error="无法编辑二进制文件",
                error_type="binary_file",
                data={"path": path},
            )

        try:
            content = resolved.read_text("utf-8")
        except UnicodeDecodeError as e:
            return ToolResult(
                status="error", content="",
                error=f"非 UTF-8 编码：{e.reason}",
                error_type="decode_error",
                data={"path": path},
            )

        count = content.count(old_string)
        if count == 0:
            preview = old_string[:80] + ("..." if len(old_string) > 80 else "")
            return ToolResult(
                status="error", content="",
                error=(
                    f"未找到匹配文本。检查 old_string 是否正确、"
                    f"缩进是否一致（空格 vs Tab）。\n"
                    f"old_string 预览：{preview}"
                ),
                error_type="old_string_not_found",
                data={"path": path, "old_string_preview": preview},
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            tmp = resolved.with_suffix(resolved.suffix + ".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            tmp.replace(resolved)
        except PermissionError:
            return ToolResult(
                status="error", content="",
                error=f"权限不足：{path}",
                error_type="permission_denied",
                data={"path": path},
            )
        except OSError as e:
            return ToolResult(
                status="error", content="",
                error=f"写入失败：{e}",
                error_type="io_error",
                data={"path": path},
            )

        replaced_count = count if replace_all else 1
        remaining = count - 1 if not replace_all else 0
        msg = f"已替换 {replaced_count} 处"
        if remaining > 0:
            msg += f"，文件还有 {remaining} 处匹配。用 replace_all=true 全部替换"

        return ToolResult(
            status="success",
            content=msg,
            data={
                "path": path,
                "replacements": replaced_count,
                "remaining_matches": remaining,
            },
        )
