"""
read_tool.py — ReadTool: 读取文件内容（纯文本，支持分页）。

文件上限 20MB，仅 UTF-8，工具内全量 read_text + splitlines。
"""

import logging
import time as time_mod
from pathlib import Path
from typing import Any

from capabilities.base import BaseTool
from capabilities.result import ToolResult

logger = logging.getLogger(__name__)


class ReadTool(BaseTool):
    """读取文件内容，支持行偏移和行数限制。"""

    name = "read"
    description = "读取文件内容，支持分页和行范围。文件上限 20MB，仅 UTF-8。"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于项目根目录或绝对路径）",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（1-based），默认 1",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "最大读取行数，默认 200",
                "default": 200,
            },
        },
        "required": ["path"],
    }

    risk = "low"
    no_truncate = False

    # ── 常量 ──
    MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
    BINARY_CHECK_BYTES = 512  # 二进制检测采样字节数
    NULL_BYTE = b"\x00"

    async def execute(self, path: str, offset: int = 1, limit: int = 200, **kwargs: Any) -> ToolResult:
        """读取文件指定行范围，返回行号 + 内容。"""
        t0 = time_mod.monotonic()
        try:
            return self._execute(path, offset, limit)
        finally:
            elapsed = int((time_mod.monotonic() - t0) * 1000)
            logger.debug("ReadTool: path=%s, offset=%d, limit=%d, %dms", path, offset, limit, elapsed)

    # ── 核心逻辑 ──

    def _execute(self, path: str, offset: int, limit: int) -> ToolResult:
        # 防御：LLM 可能将 integer 参数传为字符串
        offset = int(offset)
        limit = int(limit)

        # 1. 路径解析 & 安全
        raw = Path(path)
        if not raw.is_absolute():
            if self.project_root is None:
                return ToolResult(
                    status="error",
                    content="",
                    error="项目根目录未设置，无法解析相对路径",
                    error_type="unknown",
                    data={"path": path},
                )
            raw = (self.project_root / raw).resolve()
        else:
            raw = raw.resolve()

        # 路径必须在项目根内
        if self.project_root is not None:
            try:
                raw.relative_to(self.project_root.resolve())
            except ValueError:
                return ToolResult(
                    status="error",
                    content="",
                    error=f"路径越界，不允许访问项目根目录外的文件: {path}",
                    error_type="path_out_of_bounds",
                    data={"path": path, "resolved": str(raw)},
                )

        # 1a. 文件存在
        if not raw.exists():
            return ToolResult(
                status="error",
                content="",
                error=f"文件不存在: {path}",
                error_type="file_not_found",
                data={"path": path},
            )
        if not raw.is_file():
            return ToolResult(
                status="error",
                content="",
                error=f"路径不是文件: {path}",
                error_type="invalid_argument",
                data={"path": path, "type": "directory" if raw.is_dir() else "other"},
            )

        # 1b. 文件大小
        size = raw.stat().st_size
        if size > self.MAX_FILE_SIZE:
            return ToolResult(
                status="error",
                content="",
                error=(
                    f"文件过大 ({size / 1024 / 1024:.1f}MB)，"
                    f"最大支持 {self.MAX_FILE_SIZE / 1024 / 1024:.0f}MB。请用 bash 分块处理。"
                ),
                error_type="file_too_large",
                data={"path": path, "size_bytes": size, "max_bytes": self.MAX_FILE_SIZE},
            )

        # 1c. 二进制检测
        head = raw.read_bytes()[: self.BINARY_CHECK_BYTES]
        if self.NULL_BYTE in head:
            return ToolResult(
                status="error",
                content="",
                error="无法读取二进制文件",
                error_type="binary_file",
                data={"path": path, "size_bytes": size},
            )

        # 2. 解码为 UTF-8
        try:
            text = raw.read_text("utf-8")
        except UnicodeDecodeError as e:
            return ToolResult(
                status="error",
                content="",
                error=f"非 UTF-8 编码: {e.reason}",
                error_type="decode_error",
                data={"path": path, "encoding": "utf-8", "reason": e.reason},
            )

        # 3. 建行
        lines = text.splitlines()
        total = len(lines)

        # 4. 校验 offset
        if offset < 1:
            offset = 1
        if offset > total:
            if total == 0:
                return ToolResult(
                    status="success",
                    content="[文件为空]",
                    data={"path": path, "offset": offset, "limit": limit, "total_lines": 0, "returned_lines": 0},
                )
            return ToolResult(
                status="error",
                content="",
                error=f"offset {offset} 超出文件行数 {total}",
                error_type="offset_out_of_range",
                data={"path": path, "offset": offset, "total_lines": total},
            )

        # 5. 切片 & 格式化
        end = min(offset - 1 + limit, total)
        result_lines = []
        for i in range(offset - 1, end):
            line_num = i + 1
            result_lines.append(f"{line_num:>4}| {lines[i]}")
        content = "\n".join(result_lines)

        # EOF 标记
        if end == total:
            content += "\n[EOF]"

        warnings = []
        if size > 5 * 1024 * 1024:
            warnings.append(f"文件较大 ({size / 1024 / 1024:.1f}MB)，建议用 offset 分页读取")

        return ToolResult(
            status="success",
            content=content,
            data={
                "path": path,
                "offset": offset,
                "limit": limit,
                "total_lines": total,
                "returned_lines": end - offset + 1,
            },
            warnings=warnings,
        )
