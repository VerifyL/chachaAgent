
"""
capabilities/builtins/code_patcher.py
EditFile — 精确文件编辑工具（BaseTool）。

用法:
  edit_file(path, old_string='', new_string)  → 创建新文件（old_string 为空）
  edit_file(path, old_string, new_string)  → 精确替换（首处匹配即替换）
  edit_file(path, old_string, new_string, replace_all=True)  → 全部替换

底层：
  - 精确匹配，对齐 Claude Code：不 strip、不 fuzzy
  - 分块流式读取 + 双 chunk 窗口搜索（支持跨 chunk 边界匹配）
  - 局部替换（只重建受影响的 chunk，其余复用引用）
  - 流式写入 AtomicWriter（sha256 + stat 校验）
  - 多匹配时列出上下文供 LLM 选择
  - 新文件自动创建父目录，无需备份
"""

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from capabilities.atomic_writer import AtomicWriter
from capabilities.base import BaseTool

CHUNK_SIZE = 64 * 1024              # 64KB chunks for streaming
OLD_STRING_MAX_LENGTH = CHUNK_SIZE  # 64KB，保证最多跨 1 个 chunk 边界
LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100MB (chunk 窗口下安全)

logger = logging.getLogger(__name__)


class EditFileTool(BaseTool):
    """精确文件编辑，分块流式读写，局部替换。"""

    name = "edit_file"
    description = (
        "精确替换文件内容，适合小范围修改（<50行）。"
        "old_string 必须唯一匹配（避免误改），"
        "old_string 长度不能超过 64KB。"
        "自动备份到 .chacha_agent/backups/。"
        "old_string='' 时可创建新文件。"
        "大范围或多文件修改请使用 apply_patch。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原始文本（必须精确匹配，不超过 64KB）"},
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
        """执行文件编辑。

        流程: 校验 → 分块读取 → 双 chunk 窗口搜索 → 局部替换 → 流式写入
        """
        # 1. 路径校验
        full_path = (
            Path(path) if Path(path).is_absolute() else self._root / path
        ).resolve()
        try:
            full_path.relative_to(self._root)
        except ValueError:
            if Path.home() / ".chacha" in full_path.parents or full_path == Path.home() / ".chacha":
                pass
            else:
                return "[错误] 访问被拒绝: 路径超出项目根目录"

        # 2. 新文件创建
        if not full_path.exists():
            if old_string == "":
                full_path.parent.mkdir(parents=True, exist_ok=True)
                result = self._writer.write(full_path, new_string, backup=False)
                if result.ok:
                    preview = new_string.strip()[:80]
                    if len(new_string.strip()) > 80:
                        preview += "..."
                    return (
                        f"✅ 已创建新文件: {path}\n"
                        f"   内容: {preview}"
                    )
                else:
                    return f"[错误] 创建文件失败: {result.error}"
            else:
                return f"[错误] 文件不存在: {path}（新建文件请传 old_string=''）"

        # 3. old_string 上限检查
        if len(old_string) > OLD_STRING_MAX_LENGTH:
            return (
                f"[错误] old_string 长度 ({len(old_string)} 字节) 超过上限 "
                f"({OLD_STRING_MAX_LENGTH} 字节)。请缩小替换范围后重试。"
            )

        # 4. 文件大小检查
        file_size = full_path.stat().st_size
        if file_size > LARGE_FILE_THRESHOLD:
            return (
                f"[错误] 文件过大 ({file_size} 字节 > {LARGE_FILE_THRESHOLD} 字节)。"
                f"请使用 grep + 行号定位方式修改。"
            )

        # 5. 分块读取
        try:
            chunks = self._read_chunks(full_path)
        except Exception as e:
            return f"[错误] 读取失败: {e}"

        if not chunks or (len(chunks) == 1 and not chunks[0]):
            return "[错误] 文件为空"

        # 6. 搜索（双 chunk 窗口）
        old = old_string
        new = new_string

        if replace_all:
            matches = self._find_all_in_chunks(chunks, old)
            count = len(matches)
        else:
            match = self._find_in_chunks(chunks, old)
            count = 1 if match else 0

        if count == 0:
            first_chunk = chunks[0] if chunks else ""
            lines = first_chunk.split("\n")
            context_lines = "\n".join(f"  {i+1}: {l}" for i, l in enumerate(lines[:15]))
            total_lines = self._count_lines(chunks)
            return (
                f"[Error] old_string not found. "
                f"File starts with:\n"
                f"{context_lines}\n"
                f"  ... ({total_lines} lines total)\n"
                f"Please verify the exact text with read_file and try again."
            )

        if not replace_all and count > 1:
            all_matches = self._find_all_in_chunks(chunks, old)
            occurrences = []
            for i, m in enumerate(all_matches[:5]):
                ci, offset = m
                ctx = self._get_context(chunks, ci, offset, old)
                line_no = self._chunk_offset_to_line(chunks, ci, offset)
                occurrences.append(f"  行 {line_no}: ...{ctx}...")
            occ_list = "\n".join(occurrences)
            return (
                f"[错误] old_string 匹配了 {count} 处（不唯一）。\n"
                f"前 {min(count, 5)} 处匹配位置：\n{occ_list}\n"
                f"请提供更多上下文确保唯一匹配，或设置 replace_all=true"
            )

        # 7. 局部替换
        if replace_all:
            chunks = self._replace_all_in_chunks(chunks, old, new, matches)
            replaced = count
        else:
            chunks = self._replace_in_chunks(chunks, old, new, match)
            replaced = 1

        # 8. 流式写入（逐 chunk，不拼全量）
        result = self._writer.write_chunks(full_path, chunks, backup=True)

        if result.ok:
            return (
                f"✅ 已编辑 {path}: 替换了 {replaced} 处匹配。\n"
                f"   校验: {'通过' if result.verified else '⚠️ 校验失败，请检查文件'}\n"
                f"   备份: {result.backup}"
            )
        else:
            return f"[错误] 写入失败: {result.error}\n   备份: {result.backup}"

    # ── Chunk 读取 ──────────────────────────────────────────

    @staticmethod
    def _read_chunks(path: Path, chunk_size: int = CHUNK_SIZE) -> List[str]:
        """分块读取文件，不拼全量。返回 chunks 列表。"""
        chunks = []
        with open(path, "r", encoding="utf-8") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
        if not chunks:
            return [""]
        EditFileTool._merge_tiny_tail(chunks, min_size=16 * 1024)
        return chunks

    @staticmethod
    def _merge_tiny_tail(chunks: List[str], min_size: int) -> None:
        """如果最后一个 chunk 小于 min_size，合并到前一个。"""
        if len(chunks) > 1 and len(chunks[-1]) < min_size:
            chunks[-2] += chunks.pop()

    # ── Chunk 窗口搜索 ──────────────────────────────────────

    @staticmethod
    def _find_in_chunks(chunks: List[str], old: str) -> Optional[Tuple[int, int]]:
        """双 chunk 窗口搜索 old，返回 (chunk_index, offset) 或 None。

        窗口 = chunks[i][-len(old):] + chunks[i+1][:len(old)]
        保证 old 跨 chunk 边界也能命中。
        old 长度 ≤ CHUNK_SIZE 保证最多跨 1 个边界。
        """
        old_len = len(old)
        for i, chunk in enumerate(chunks):
            pos = chunk.find(old)
            if pos != -1:
                return (i, pos)
            # 跨 chunk 窗口
            if i + 1 < len(chunks):
                tail = chunk[-old_len:] if old_len <= len(chunk) else chunk
                head = chunks[i + 1][:old_len]
                overlap = tail + head
                pos = overlap.find(old)
                if pos != -1:
                    offset = len(chunk) - len(tail) + pos
                    return (i, offset)
        return None

    @staticmethod
    def _find_all_in_chunks(chunks: List[str], old: str) -> List[Tuple[int, int]]:
        """查找所有匹配，返回 [(chunk_index, offset), ...] 列表。"""
        matches = []
        old_len = len(old)
        for i, chunk in enumerate(chunks):
            pos = 0
            while True:
                pos = chunk.find(old, pos)
                if pos == -1:
                    break
                matches.append((i, pos))
                pos += len(old)
            # 跨 chunk 窗口
            if i + 1 < len(chunks):
                tail = chunk[-old_len:] if old_len <= len(chunk) else chunk
                head = chunks[i + 1][:old_len]
                overlap = tail + head
                pos = overlap.find(old)
                if pos != -1 and pos < len(tail):
                    matches.append((i, len(chunk) - len(tail) + pos))
        return matches

    # ── Chunk 局部替换 ──────────────────────────────────────

    @staticmethod
    def _replace_in_chunks(
        chunks: List[str], old: str, new: str, match: Tuple[int, int]
    ) -> List[str]:
        """只在受影响的 chunk 上做替换，其余复用引用。

        match: (chunk_index, offset) — old 在 chunks[chunk_index] 中的偏移。
        """
        i, offset = match
        old_len = len(old)
        chunk = chunks[i]

        if offset + old_len <= len(chunk):
            # 单 chunk：字符串切片拼接
            chunks[i] = chunk[:offset] + new + chunk[offset + old_len:]
        else:
            # 跨 chunk：拼接 → 替换 → 拆回
            if i + 1 >= len(chunks):
                raise ValueError("跨 chunk 匹配但缺少下一个 chunk")
            merged = chunk + chunks[i + 1]
            merged = merged[:offset] + new + merged[offset + old_len:]
            split = offset + len(new)
            chunks[i] = merged[:split]
            chunks[i + 1] = merged[split:]
        return chunks

    @staticmethod
    def _replace_all_in_chunks(
        chunks: List[str], old: str, new: str, matches: List[Tuple[int, int]]
    ) -> List[str]:
        """替换所有匹配，从后往前避免偏移失效。"""
        for ci, offset in reversed(matches):
            chunks = EditFileTool._replace_in_chunks(chunks, old, new, (ci, offset))
        return chunks

    # ── 辅助方法 ────────────────────────────────────────────

    @staticmethod
    def _count_lines(chunks: List[str]) -> int:
        """统计 chunks 总行数。"""
        return sum(c.count("\n") for c in chunks)

    @staticmethod
    def _chunk_offset_to_line(chunks: List[str], chunk_idx: int, offset: int) -> int:
        """计算 chunk 内偏移对应的全局行号（1-based）。"""
        line = 1
        for i in range(chunk_idx):
            line += chunks[i].count("\n")
        if offset > 0:
            line += chunks[chunk_idx][:offset].count("\n")
        return line

    @staticmethod
    def _get_context(chunks: List[str], chunk_idx: int, offset: int, old: str) -> str:
        """获取匹配位置周围 30 字符上下文。"""
        chunk = chunks[chunk_idx]
        start = max(0, offset - 30)
        end = min(len(chunk), offset + len(old) + 30)
        return chunk[start:end].replace("\n", "\\n")


