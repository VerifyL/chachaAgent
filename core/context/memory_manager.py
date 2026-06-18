"""
core/context/memory_manager.py
MemoryManager — 纯文件 I/O，LLM 通过工具自主驱动。

注册为内置工具（阶段 5）：
  load_memory(query) → search(query)     搜索所有记忆文件
  load_memory()      → list_days() + read(today)
  remember(content)  → remember(content)

MEMORY.md 由 autoDream 管道定期构建（v1.0），作为轻量索引注入上下文。

TODO(v1.0): autoDream — Orient(读)→Gather(去重)→Consolidate(摘要)→Prune(删旧)
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE = Path(".chacha_agent/memory")


class MemoryManager:
    """按日期+会话分散存储，支持搜索/去重/修剪。

    session_id 为空时 → projects/{project_id}/memory/
    session_id 提供时 → projects/{project_id}/sessions/{session_id}/
    """

    def __init__(
        self,
        project_id: str = "default",
        base_dir: Optional[Path] = None,
        session_id: str = "",
    ):
        if session_id:
            self._base = (
                (base_dir or _DEFAULT_BASE)
                / "projects" / project_id / "sessions" / session_id
            )
        else:
            self._base = (
                (base_dir or _DEFAULT_BASE)
                / "projects" / project_id / "memory"
            )
        self._base.mkdir(parents=True, exist_ok=True)

    # ====== 读（轻量索引，ContextManager 用） ======

    def read(self) -> str:
        """读取 MEMORY.md 索引（autoDream 产物，轻量 200 条摘要）。"""
        path = self._index_path()
        return self._read(path)

    # ====== 搜索（LLM 工具 load_memory 调用） ======

    def search(self, query: str, limit: int = 10, max_chars: int = 6000, across_sessions: bool = False) -> str:
        """搜索今日文件（+ 可选跨 session 搜索），返回匹配 query 的条目。"""
        keywords = query.lower().split()
        scored: list[tuple[str, float]] = []

        # 搜索当前 session 的文件
        search_dirs = [self._base]
        if across_sessions:
            sessions_dir = self._base.parents[1] / "sessions" if "sessions" in str(self._base) else self._base.parent / "sessions"
            if sessions_dir.exists():
                search_dirs.extend(sessions_dir.iterdir())

        for sd in search_dirs:
            if not sd.is_dir():
                continue
            for day_file in sorted(sd.glob("????-??-??.md"), reverse=True):
                text = self._read(day_file)
                entries = self._split_entries(text)
                for entry in entries:
                    entry_lower = entry.lower()
                    score = sum(1 for kw in keywords if kw in entry_lower)
                    if score > 0:
                        scored.append((
                            f"[{day_file.stem}] {entry.strip()}",
                            score / len(keywords),
                        ))

        scored.sort(key=lambda x: x[1], reverse=True)
        result = "\n".join(entry for entry, _ in scored[:limit])
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... [截断]"
        return result

    def read_day(self, date_str: str) -> str:
        """读取指定日期的完整记忆文件。"""
        return self._read(self._day_path(date_str))

    def update_index(self, content: str) -> Path:
        """覆盖式写入 MEMORY.md 索引（autoDream 输出）。"""
        path = self._index_path()
        path.write_text(content.strip() + "\n", encoding="utf-8")
        return path

    def list_days(self, limit: int = 30) -> list[str]:
        """列出最近 N 天（按日期降序）。"""
        files = sorted(
            [f.stem for f in self._base.glob("????-??-??.md")],
            reverse=True,
        )
        return files[:limit]

    # ====== 写 ======

    def remember(self, content: str, date_str: Optional[str] = None) -> Path:
        """追加一条记忆到日期文件。返回文件路径。"""
        date = date_str or self._date_str()
        path = self._day_path(date)

        timestamp = datetime.now(tz=timezone.utc).isoformat()
        entry = f"\n## {timestamp}\n{content.strip()}\n"

        existing = self._read(path)
        path.write_text((existing + entry).strip() + "\n", encoding="utf-8")
        logger.info("记忆已写入: %s", path)
        return path

    # ====== 维护（去重/更新/修剪，LLM 不需要调用，autoDream 用） ======

    def deduplicate(self, date_str: Optional[str] = None) -> int:
        """去除指定日期文件内的重复条目（按内容去重，忽略时间戳）。返回删除数。"""
        path = self._day_path(date_str or self._date_str())
        entries = self._split_entries(self._read(path))
        seen: set[str] = set()
        unique: list[str] = []
        removed = 0

        for entry in entries:
            lines = entry.strip().split("\n")
            # 取最后一行作为内容（跳过 ## timestamp）
            content_key = lines[-1].strip().lower() if len(lines) > 1 else entry.strip().lower()
            if content_key in seen:
                removed += 1
            else:
                seen.add(content_key)
                unique.append(entry)

        if removed > 0:
            path.write_text("\n".join(unique) + "\n" if unique else "", encoding="utf-8")
            logger.info("去重完成: %s (删除 %d 条)", path, removed)
        return removed

    def trim(self, date_str: Optional[str] = None, keep_lines: int = 500) -> int:
        """裁剪指定日期文件，只保留最后 keep_lines 行。返回删除行数。"""
        path = self._day_path(date_str or self._date_str())
        lines = self._read(path).split("\n")
        if len(lines) <= keep_lines:
            return 0

        removed = len(lines) - keep_lines
        path.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
        logger.info("修剪完成: %s (删除 %d 行)", path, removed)
        return removed

    def update_entry(
        self, old_text: str, new_content: str, date_str: Optional[str] = None
    ) -> bool:
        """更新指定日期文件中的某条记忆。old_text 用于模糊匹配。返回是否成功。"""
        path = self._day_path(date_str or self._date_str())
        entries = self._split_entries(self._read(path))
        updated = False

        for i, entry in enumerate(entries):
            if old_text.strip().lower() in entry.lower():
                entries[i] = new_content.strip()
                updated = True
                break

        if updated:
            path.write_text("\n".join(entries) + "\n", encoding="utf-8")
            logger.info("记忆已更新: %s", path)
        return updated

    # ====== 内部 ======

    @staticmethod
    def _split_entries(text: str) -> list[str]:
        """按 ## 分割条目"""
        parts = text.split("\n## ")
        return [p if p.startswith("##") or i == 0 else "## " + p for i, p in enumerate(parts) if p.strip()]

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def _day_path(self, date_str: str) -> Path:
        return self._base / f"{date_str}.md"

    def _index_path(self) -> Path:
        return self._base / "MEMORY.md"

    @staticmethod
    def _date_str() -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
