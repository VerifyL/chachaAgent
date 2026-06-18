"""
capabilities/builtins/code_patcher.py
EditFile — 行级文件编辑工具（BaseTool）。

用法:
  edit_file(path, old_string, new_string)  → 精确替换（首处匹配即替换）
  edit_file(path, old_string, new_string, replace_all=True)  → 全部替换
"""

import logging
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = ".chacha_agent/backups"


class EditFileTool(BaseTool):
    """行级编辑：edit_file(path, old_string, new_string)"""

    name = "edit_file"
    description = "精确替换文件内容。old_string 必须唯一匹配（避免误改），自动备份到 .chacha_agent/backups/"
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
        self._root = root or Path.cwd()

    async def execute(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        full_path = (Path(path) if Path(path).is_absolute() else self._root / path).resolve()
        if not full_path.exists():
            return f"[错误] 文件不存在: {path}"

        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        old = old_string.strip("\n")
        new = new_string.strip("\n")

        count = content.count(old)
        if count == 0:
            return f"[错误] 未找到 old_string 匹配: '{old[:80]}...'"

        if not replace_all:
            if count > 1:
                return (
                    f"[错误] old_string 匹配了 {count} 处（不唯一）。"
                    "请提供更多上下文确保唯一匹配，或设置 replace_all=true"
                )
            new_content = content.replace(old, new, 1)
            replaced = 1
        else:
            new_content = content.replace(old, new)
            replaced = content.count(old) - new_content.count(old) + new_content.count(new) - content.count(new) + count
            replaced = new_content.count(new) - content.count(new) + content.count(old) - new_content.count(old)
            # 简化：count 处全部替换
            replaced = count
            new_content = content.replace(old, new)

        # 备份
        self._backup(full_path, content)

        full_path.write_text(new_content, encoding="utf-8")
        return f"已编辑 {path}: 替换了 {replaced} 处匹配。"

    def _backup(self, file_path: Path, content: str) -> None:
        backup_dir = self._root / BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        # 尝试用相对路径，否则用文件名
        try:
            rel = str(file_path.relative_to(self._root))
        except ValueError:
            rel = file_path.name
        safe_name = rel.replace("/", "_").replace("\\", "_")
        backup_path = backup_dir / f"{safe_name}.bak"
        backup_path.write_text(content, encoding="utf-8")
