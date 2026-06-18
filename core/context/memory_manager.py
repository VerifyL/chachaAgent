"""
core/context/memory_manager.py
MemoryManager — 纯文件 I/O，LLM 通过工具自主驱动。

注册为内置工具（阶段 5）：
  load_memory(query) → search(query)     搜索所有记忆文件
  load_memory()      → list_days() + read(today)
  remember(content)  → remember(content)

MEMORY.md 由 autoDream 管道定期构建（v1.0），作为轻量索引注入上下文。

v2.0 新增:
  - CHACHA_MEMORY.md 永久记忆（≤100条，保护区，永不删除）
  - session 隔离的每日记忆 + tool_cache 缓存
  - 老化时间缩短为 7 天
"""

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE = Path(".chacha_agent/memory")
_PERMANENT_MEMORY_FILENAME = "CHACHA_MEMORY.md"
_MAX_PERMANENT_ENTRIES = 100
_PRUNE_DAYS = 7


class MemoryManager:
    """按日期+会话分散存储，支持搜索/去重/修剪。

    session_id 为空时 → projects/{project_id}/memory/
    session_id 提供时 → projects/{project_id}/sessions/{session_id}/

    文件结构:
        projects/{project_id}/
            CHACHA_MEMORY.md           ← 永久记忆（保护区）
            memory/
                MEMORY.md               ← autoDream 轻量索引
                {YYYY-MM-DD}.md         ← 项目级每日记忆（无 session 回退）
            sessions/{session_id}/
                {YYYY-MM-DD}.md         ← 会话每日记忆（user+assistant）
                tool_cache/             ← 工具结果缓存（会话结束删除）
    """

    def __init__(
        self,
        project_id: str = "default",
        base_dir: Optional[Path] = None,
        session_id: str = "",
    ):
        self._project_id = project_id
        root = base_dir or _DEFAULT_BASE
        self._project_dir = root / "projects" / project_id

        if session_id:
            self._base = self._project_dir / "sessions" / session_id
        else:
            self._base = self._project_dir / "memory"
        self._base.mkdir(parents=True, exist_ok=True)

        # tool_cache 目录
        self._tool_cache_dir = self._base / "tool_cache"
        self._tool_cache_dir.mkdir(parents=True, exist_ok=True)

    # ====== 永久记忆 (CHACHA_MEMORY.md) ======

    def read_permanent_memory(self) -> str:
        """读取 CHACHA_MEMORY.md 永久记忆（保护区，永不删除）。"""
        path = self._project_dir / _PERMANENT_MEMORY_FILENAME
        return self._read(path)

    def write_permanent_memory(self, content: str) -> Path:
        """覆盖式写入 CHACHA_MEMORY.md 永久记忆（autoDream 输出）。"""
        path = self._project_dir / _PERMANENT_MEMORY_FILENAME
        path.write_text(content.strip() + "\n", encoding="utf-8")
        logger.info("永久记忆已更新: %s", path)
        return path

    def permanent_memory_path(self) -> Path:
        """返回 CHACHA_MEMORY.md 路径。"""
        return self._project_dir / _PERMANENT_MEMORY_FILENAME

    @property
    def max_permanent_entries(self) -> int:
        return _MAX_PERMANENT_ENTRIES

    # ====== 读（轻量索引，ContextManager 用） ======

    def read(self) -> str:
        """读取 MEMORY.md 索引（autoDream 产物，轻量摘要）。"""
        path = self._index_path()
        return self._read(path)

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

    # ====== 跨 session 收集（autoDream 用） ======

    def list_all_session_days(self, limit_days: int = 7) -> dict[str, list[Path]]:
        """收集所有 session 目录下最近 N 天的每日文件。
        
        Returns:
            {session_id: [Path, ...]} 每个 session 的每日文件列表
        """
        sessions_dir = self._project_dir / "sessions"
        if not sessions_dir.exists():
            return {}

        cutoff = datetime.now(tz=timezone.utc)
        result: dict[str, list[Path]] = {}

        for session_dir in sorted(sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            day_files = sorted(session_dir.glob("????-??-??.md"))
            recent = []
            for f in day_files:
                try:
                    dt = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if (cutoff - dt).days <= limit_days:
                        recent.append(f)
                except ValueError:
                    continue
            if recent:
                result[session_dir.name] = recent

        return result

    # ====== 搜索（LLM 工具 load_memory 调用） ======

    def search(
        self, query: str, limit: int = 10,
        max_chars: int = 6000, across_sessions: bool = False,
    ) -> str:
        """搜索记忆文件，返回匹配 query 的条目。"""
        keywords = query.lower().split()
        scored: list[tuple[str, float]] = []

        search_dirs = [self._base]
        if across_sessions:
            sessions_dir = self._project_dir / "sessions"
            if sessions_dir.exists():
                search_dirs.extend(
                    d for d in sessions_dir.iterdir() if d.is_dir()
                )

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
                        source = f"{sd.parent.name}/{sd.name}/{day_file.stem}" if sd != self._base else day_file.stem
                        scored.append((
                            f"[{source}] {entry.strip()}",
                            score / len(keywords),
                        ))

        scored.sort(key=lambda x: x[1], reverse=True)
        result = "\n".join(entry for entry, _ in scored[:limit])
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... [截断]"
        return result

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

    # ====== 工具结果缓存 ======

    def cache_tool_result(
        self, tool_use_id: str, tool_name: str, result: str,
    ) -> Path:
        """缓存工具结果到 tool_cache/ 目录。返回缓存路径。"""
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{tool_name}_{tool_use_id}_{ts}.json"
        path = self._tool_cache_dir / filename

        data = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "cached_at": datetime.now(tz=timezone.utc).isoformat(),
            "result": result,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_cached_tool_result(self, cache_key: str) -> Optional[str]:
        """读取缓存的工具结果。cache_key 可以是文件名或路径。"""
        # 尝试在 tool_cache/ 中查找
        candidates = [
            self._tool_cache_dir / cache_key,
            self._tool_cache_dir / f"{cache_key}.json",
        ]
        if cache_key.startswith("tool_cache/"):
            candidates.insert(0, self._base / cache_key)
        for p in candidates:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data.get("result", "")
        return None

    def cleanup_tool_cache(self) -> int:
        """清空 tool_cache 目录。返回删除文件数。"""
        count = 0
        if self._tool_cache_dir.exists():
            for f in self._tool_cache_dir.iterdir():
                f.unlink()
                count += 1
            logger.info("已清理 tool_cache: %d 个文件", count)
        return count

    @property
    def tool_cache_dir(self) -> Path:
        return self._tool_cache_dir

    # ====== 维护（去重/更新/修剪） ======

    def deduplicate(self, date_str: Optional[str] = None) -> int:
        """去除指定日期文件内的重复条目。返回删除数。"""
        path = self._day_path(date_str or self._date_str())
        entries = self._split_entries(self._read(path))
        seen: set[str] = set()
        unique: list[str] = []
        removed = 0

        for entry in entries:
            lines = entry.strip().split("\n")
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
        self, old_text: str, new_content: str, date_str: Optional[str] = None,
    ) -> bool:
        """更新指定日期文件中的某条记忆。返回是否成功。"""
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

    def prune_old_days(self) -> int:
        """删除超过 7 天的每日记忆文件（MEMORY.md 和 CHACHA_MEMORY.md 不动）。返回删除数。"""
        cutoff = datetime.now(tz=timezone.utc)
        deleted = 0

        # 扫描所有可能包含日文件的目录
        dirs_to_scan = [self._base]
        sessions_dir = self._project_dir / "sessions"
        if sessions_dir.exists():
            dirs_to_scan.extend(d for d in sessions_dir.iterdir() if d.is_dir())

        for scan_dir in dirs_to_scan:
            for f in scan_dir.glob("????-??-??.md"):
                try:
                    dt = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if (cutoff - dt).days > _PRUNE_DAYS:
                        f.unlink()
                        deleted += 1
                except ValueError:
                    continue

        if deleted:
            logger.info("Prune: 删除 %d 个旧记忆文件 (>%d 天)", deleted, _PRUNE_DAYS)
        return deleted

    # ====== 内部 ======

    @staticmethod
    def _split_entries(text: str) -> list[str]:
        parts = text.split("\n## ")
        return [
            p if p.startswith("##") or i == 0 else "## " + p
            for i, p in enumerate(parts) if p.strip()
        ]

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
