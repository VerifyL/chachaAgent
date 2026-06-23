"""
capabilities/builtins/code_patcher.py
EditFile — 精确文件编辑工具（BaseTool）。

用法:
  edit_file(path, old_string, new_string)  → 精确替换（首处匹配即替换）
  edit_file(path, old_string, new_string, replace_all=True)  → 全部替换

底层：
  - 精确匹配优先（当前行为，对 LLM 最友好）
  - 精确失败时用 difflib.SequenceMatcher 模糊匹配，容忍空白差异
  - 写入走 AtomicWriter（原子 rename + 版本化备份 + 回读验证）
"""

import difflib
import logging
from pathlib import Path
from typing import Optional

from capabilities.atomic_writer import AtomicWriter, WriteResult
from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.80  # SequenceMatcher 相似度阈值


class EditFileTool(BaseTool):
    """精确文件编辑，底层原子写入 + 模糊匹配。"""

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

        # 2. 读取
        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        old = old_string.strip("\n")
        new = new_string.strip("\n")

        # 3. 精确匹配优先
        count = content.count(old)

        if count == 0:
            # 4. 模糊匹配 fallback
            fuzzy = self._fuzzy_find(content, old)
            if fuzzy is None:
                snippet = old[:80] + ("..." if len(old) > 80 else "")
                return (
                    f"[错误] 未找到 old_string 匹配。"
                    f"文件可能已被修改，请用 read_file 确认当前内容。\n"
                    f"搜索片段: '{snippet}'"
                )
            return (
                f"[提示] 精确匹配失败，但找到相似文本（相似度 {fuzzy['ratio']:.0%}）：\n"
                f"--- 文件中的实际内容 (行 {fuzzy['line']}) ---\n"
                f"{fuzzy['text'][:200]}\n"
                f"--- 你提供的 old_string ---\n"
                f"{old[:200]}\n"
                f"请用文件中的实际内容作为 old_string 重试。"
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

    def _fuzzy_find(self, content: str, target: str) -> Optional[dict]:
        """用 difflib 在文件中找最相似的文本块。

        将 target 按行分割，在 content 中逐行滑动窗口比对。
        """
        target_lines = target.strip().split("\n")
        content_lines = content.split("\n")

        if len(target_lines) > len(content_lines):
            return None

        best_ratio = 0.0
        best_start = -1
        best_end = -1

        window = len(target_lines)
        for i in range(len(content_lines) - window + 1):
            window_text = "\n".join(content_lines[i : i + window])
            ratio = difflib.SequenceMatcher(None, target, window_text).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i
                best_end = i + window

        if best_ratio >= FUZZY_THRESHOLD:
            return {
                "ratio": best_ratio,
                "line": best_start + 1,
                "text": "\n".join(content_lines[best_start:best_end]),
            }
        return None
