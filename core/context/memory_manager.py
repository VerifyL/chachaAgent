"""
core/context/memory_manager.py
MemoryManager — 纯文件 I/O，LLM 通过工具自主驱动。

注册为内置工具（阶段 5）：
  load_memory(query) → search(query)     搜索所有记忆文件
  load_memory()      → list_days() + read(today)
  remember(content)  → remember(content)

MEMORY.md 由 autoDream 管道定期构建（v1.0），作为轻量索引注入上下文。

v2.0 新增:
  - CHACHA_MEMORY.md 永久记忆（无条目上限，保护区，永不删除）
  - session 隔离的每日记忆 + tool_cache 缓存
  - 老化时间缩短为 7 天
"""

import logging
import json
from datetime import timedelta,  datetime, timezone
from pathlib import Path
from typing import Optional
import hashlib

logger = logging.getLogger(__name__)

_DEFAULT_BASE = Path.home() / ".chacha" 
_PERMANENT_MEMORY_FILENAME = "CHACHA_MEMORY.md"
_PRUNE_DAYS = 7

# 主题名常量
_TOPICS = [
    "user-preferences",
    "project-decisions",
    "lessons-learned",
    "errors-fixed",
    "project-progress",
]

class MemoryManager:
    """按日期分散存储，支持搜索/去重/修剪。

    文件结构 (v2.1):
        projects/{project_id}/
            CHACHA_MEMORY.md           ← 永久记忆（保护区，跨 session 共享）
            memory/
                sessions/
                    {session_id}/
                        MEMORY.md       ← 该 session 的 autoDream 产物
                        {YYYY-MM-DD}.md ← 每日对话记忆
                        topics/         ← 主题记忆
                        tool_cache/     ← 工具结果缓存
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        project_id: str = "",
        base_dir: Optional[Path] = None,
        session_id: str = "",
    ):
        if project_id:
            self._project_id = project_id
        elif project_root:
            self._project_id = hashlib.sha256(
                str(project_root.resolve()).encode()
            ).hexdigest()[:12]
        else:
            self._project_id = "default"

        self._session_id = session_id
        root = base_dir or _DEFAULT_BASE
        self._project_dir = root / "projects" / self._project_id

        # memory/ 为统一记忆根目录
        self._base = self._project_dir / "memory"
        self._base.mkdir(parents=True, exist_ok=True)

        # session 隔离: sessions/{sid}/。无 session_id → 不创建目录（仅用于读/project级操作）
        if session_id:
            self._session_dir = self._base / "sessions" / session_id
            self._session_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._session_dir = None

        # memory/tool_cache/ — 工具结果缓存（session 级别）
        self._tool_cache_dir = self._session_dir / "tool_cache" if self._session_dir else None
        if self._tool_cache_dir:
            self._tool_cache_dir.mkdir(parents=True, exist_ok=True)

        # memory/topics/ — 主题记忆（session 级别）
        self._topics_dir = self._session_dir / "topics" if self._session_dir else None
        if self._topics_dir:
            self._topics_dir.mkdir(parents=True, exist_ok=True)

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

    # ====== 主题记忆（Agent 自主写入） ======

    def read_topic(self, topic_name: str) -> str:
        """读取指定主题的完整内容。"""
        path = self._topics_dir / f"{topic_name}.md"
        return self._read(path)

    def write_topic(self, topic_name: str, content: str) -> Path:
        """追加内容到指定主题文件。"""
        path = self._topics_dir / f"{topic_name}.md"
        ts = datetime.now(tz=timezone(timedelta(hours=8))).isoformat()
        entry = f"\n## {ts}\n{content.strip()}\n"
        existing = self._read(path)
        path.write_text((existing + entry).strip() + "\n", encoding="utf-8")
        logger.info("主题记忆已写入: %s -> %s", topic_name, path)
        return path

    def list_topics(self) -> list[str]:
        """列出所有已存在的主题名称。"""
        if not self._topics_dir.exists():
            return []
        return sorted([
            f.stem for f in self._topics_dir.glob("*.md")
        ])

    def all_topics_content(self) -> str:
        """收集所有主题文件内容（供 DreamPipeline 使用）。"""
        parts = []
        for name in self.list_topics():
            text = self.read_topic(name)
            if text.strip():
                parts.append(f"## topics/{name}.md\n{text}")
        return "\n\n".join(parts)

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
            [f.stem for f in self._session_dir.glob("????-??-??.md")],
            reverse=True,
        )
        return files[:limit]

    # ====== Session 管理 ======

    def list_all_sessions(self) -> list[str]:
        """列出所有 session ID（排除 _default）。"""
        sessions_dir = self._base / "sessions"
        if not sessions_dir.exists():
            return []
        return sorted(
            [d.name for d in sessions_dir.iterdir()
             if d.is_dir() and d.name != "_default"],
            reverse=True,
        )

    def delete_session(self, session_id: str) -> bool:
        """删除指定 session 目录。"""
        import shutil
        session_path = self._base / "sessions" / session_id
        if session_path.exists():
            shutil.rmtree(session_path)
            logger.info("已删除 session: %s", session_id)
            return True
        return False

    def read_session_memory(self, session_id: str) -> str:
        """读取指定 session 的 MEMORY.md（供 GlobalDream 收集）。"""
        path = self._base / "sessions" / session_id / "MEMORY.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def list_all_session_dirs(self) -> list[Path]:
        """收集所有 session 的 MEMORY.md（排除 _default）。"""
        sessions_dir = self._base / "sessions"
        if not sessions_dir.exists():
            return []
        result = []
        for sd in sorted(sessions_dir.iterdir(), reverse=True):
            if not sd.is_dir() or sd.name == "_default":
                continue
            mem = sd / "MEMORY.md"
            if mem.exists():
                result.append(mem)
        return result

    def list_all_session_days(self, limit_days: int = 7) -> list[Path]:
        """收集 memory/session/ 下最近 N 天的每日文件。
        
        Returns:
            每日文件路径列表
        """
        if not self._session_dir.exists():
            return []

        cutoff = datetime.now(tz=timezone(timedelta(hours=8)))
        day_files = sorted(self._session_dir.glob("????-??-??.md"))
        recent = []
        for f in day_files:
            try:
                dt = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
                if (cutoff - dt).days <= limit_days:
                    recent.append(f)
            except ValueError:
                continue
        return recent


    # ====== 搜索（LLM 工具 load_memory 调用） ======

    def search(
        self, query: str, limit: int = 10,
        max_chars: int = 6000,
    ) -> str:
        """搜索记忆文件，返回匹配 query 的条目。"""
        keywords = query.lower().split()
        scored: list[tuple[str, float]] = []

        # 搜索 memory/session/ 下的日文件
        for day_file in sorted(self._session_dir.glob("????-??-??.md"), reverse=True):
            text = self._read(day_file)
            entries = self._split_entries(text)
            for entry in entries:
                entry_lower = entry.lower()
                score = sum(1 for kw in keywords if kw in entry_lower)
                if score > 0:
                    source = f"session/{day_file.stem}"
                    scored.append((
                        f"[{source}] {entry.strip()}",
                        score / len(keywords),
                    ))

        # 搜索 memory/topics/ 下的主题文件
        if self._topics_dir.exists():
            for topic_file in sorted(self._topics_dir.glob("*.md")):
                text = self._read(topic_file)
                entries = self._split_entries(text)
                for entry in entries:
                    entry_lower = entry.lower()
                    score = sum(1 for kw in keywords if kw in entry_lower)
                    if score > 0:
                        source = f"topics/{topic_file.stem}"
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

        timestamp = datetime.now(tz=timezone(timedelta(hours=8))).isoformat()
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
        ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{tool_name}_{tool_use_id}_{ts}.json"
        path = self._tool_cache_dir / filename

        data = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "cached_at": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
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
        cutoff = datetime.now(tz=timezone(timedelta(hours=8)))
        deleted = 0

        # 只扫描 memory/session/ 下的日文件
        for f in self._session_dir.glob("????-??-??.md"):
            try:
                dt = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
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
        if not self._session_dir:
            raise RuntimeError("MemoryManager 需要 session_id")
        return self._session_dir / f"{date_str}.md"

    def _index_path(self) -> Path:
        """MEMORY.md 现在是 session 级的。"""
        return self._session_dir / "MEMORY.md"


    @staticmethod
    def _date_str() -> str:
        return datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    @staticmethod
    def project_hash(project_root: Path) -> str:
        return hashlib.sha256(
            str(project_root.resolve()).encode()
        ).hexdigest()[:12]
