"""
capabilities/builtins/code_patcher.py
EditFile — 精确文件编辑工具（BaseTool）。

用法:
  edit_file(path, old_string, new_string)  → 精确替换（首处匹配即替换）
  edit_file(path, old_string, new_string, replace_all=True)  → 全部替换

底层：
  - 精确匹配，对齐 Claude Code：不 strip、不 fuzzy
  - 多匹配时列出上下文供 LLM 选择
  - 写入走 AtomicWriter（原子 rename + 版本化备份 + 回读验证）
"""

import logging
from pathlib import Path
from typing import Optional

from capabilities.atomic_writer import AtomicWriter
from capabilities.base import BaseTool

CHUNK_SIZE = 64 * 1024        # 64KB chunks for streaming
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10MB

logger = logging.getLogger(__name__)


class EditFileTool(BaseTool):
    """精确文件编辑，底层原子写入。"""

    name = "edit_file"
    description = (
        "精确替换文件内容。old_string 必须唯一匹配（避免误改），"
        "自动备份到 .chacha_agent/backups/"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原始文本（必须精确匹配）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配项（默认仅替换首处）"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    risk = "medium"
    requires_approval = True

    def __init__(self, root: Optional[Path] = None):
        self._root = (root or Path.cwd()).resolve()
        self._writer = AtomicWriter(root=self._root)

    async def execute(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        # 1. 路径校验
        full_path = (
            Path(path) if Path(path).is_absolute() else self._root / path
        ).resolve()
        try:
            full_path.relative_to(self._root)
        except ValueError:
            return "[错误] 访问被拒绝: 路径超出项目根目录"

        if not full_path.exists():
            return f"[错误] 文件不存在: {path}"

        # 2. Read: chunked streaming for large files
        try:
            file_size = full_path.stat().st_size
            if file_size > LARGE_FILE_THRESHOLD:
                # 大文件：分块搜索 + 累积内容，一次 IO 完成搜索和加载
                found = False
                chunks = []
                with open(full_path, "r", encoding="utf-8") as f:
                    prev = ""
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if old_string in prev + chunk:
                            found = True
                        prev = chunk[-len(old_string):] if len(old_string) > 0 else ""
                content = "".join(chunks)
                if not found:
                    # 找不到时显示文件头部，帮助 LLM 修正
                    lines = content.split("\n")
                    context_lines = "\n".join(f"  {i+1}: {l}" for i, l in enumerate(lines[:15]))
                    return (
                        f"[Error] old_string not found. "
                        f"File starts with:\n"
                        f"{context_lines}\n"
                        f"  ... ({len(lines)} lines total)\n"
                        f"Please verify the exact text with read_file and try again."
                    )
            else:
                content = full_path.read_text(encoding="utf-8")
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        # 3. 精确匹配（对齐 Claude Code：不做任何 strip/fuzzy）
        old = old_string
        new = new_string
        count = content.count(old)

        if count == 0:
            snippet = old[:80] + ("..." if len(old) > 80 else "")
            return (
                f"[错误] old_string 未找到。文件可能已被修改，"
                f"请用 read_file 确认当前内容后重试。\n"
                f"搜索片段: '{snippet}'"
            )

        if not replace_all and count > 1:
            # 列出上下文帮 LLM 选择
            occurrences = []
            pos = -1
            for _ in range(count):
                pos = content.find(old, pos + 1)
                line_no = content[:pos].count("\n") + 1
                start = max(0, pos - 30)
                end = min(len(content), pos + len(old) + 30)
                ctx = content[start:end].replace("\n", "\\n")
                occurrences.append(f"  行 {line_no}: ...{ctx}...")
                if len(occurrences) >= 5:
                    break

            occ_list = "\n".join(occurrences)
            return (
                f"[错误] old_string 匹配了 {count} 处（不唯一）。\n"
                f"前 {min(count, 5)} 处匹配位置：\n{occ_list}\n"
                f"请提供更多上下文确保唯一匹配，或设置 replace_all=true"
            )

        # 5. 执行替换
        new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        replaced = count if replace_all else 1

        # 6. 原子写入
        result = self._writer.write(full_path, new_content, backup=True)

        if result.ok:
            return (
                f"✅ 已编辑 {path}: 替换了 {replaced} 处匹配。\n"
                f"   校验: {'通过' if result.verified else '⚠️ 校验失败，请检查文件'}\n"
                f"   备份: {result.backup}"
            )
        else:
            return f"[错误] 写入失败: {result.error}\n   备份: {result.backup}"
