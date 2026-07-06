"""
core/context/memory_manager.py
MemoryManager — 纯文件 I/O，为 memory 工具提供底层存储。

内部方法：
  search(query)           搜索所有记忆文件
  list_days() + read(today)  加载当日记忆
  remember(content)        追加记忆条目

MEMORY.md 由 autoDream 管道定期构建（v1.0），作为轻量索引注入上下文。

v2.0 新增:
  - CHACHA_MEMORY.md 永久记忆（无条目上限，保护区，永不删除）
  - session 隔离的每日记忆
  - 老化时间缩短为 7 天
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from capabilities.atomic_writer import AtomicWriter

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

        # memory/topics/ — 主题记忆（session 级别）
        self._topics_dir = self._session_dir / "topics" if self._session_dir else None
        if self._topics_dir:
            self._topics_dir.mkdir(parents=True, exist_ok=True)

        # 原子写入器：tmp+rename + 文件锁 + 回读验证
        self._writer = AtomicWriter(root=self._project_dir)

    # ====== 永久记忆 (CHACHA_MEMORY.md) ======

    @property
    def session_dir(self):
        return self._session_dir

    @property
    def topics_dir(self):
        return self._topics_dir

    def read_permanent_memory(self) -> str:
        """读取 CHACHA_MEMORY.md 永久记忆（保护区，永不删除）。"""
        path = self._project_dir / _PERMANENT_MEMORY_FILENAME
        return self._read(path)

    def write_permanent_memory(self, content: str) -> Path:
        """覆盖式写入 CHACHA_MEMORY.md 永久记忆（autoDream 输出）。"""
        path = self._project_dir / _PERMANENT_MEMORY_FILENAME
        full_content = content.strip() + "\n"
        result = self._writer.write(path, full_content, backup=False)
        if not result.ok:
            logger.error("永久记忆写入失败: %s -> %s", path, result.error)
            raise IOError(f"写入永久记忆失败: {result.error}")
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
        """追加内容到指定主题文件，并实时同步 MEMORY.md 索引。"""
        if self._topics_dir is None:
            raise RuntimeError("话题目录未初始化")
        self._topics_dir.mkdir(parents=True, exist_ok=True)  # 确保目录存在
        path = self._topics_dir / f"{topic_name}.md"
        ts = datetime.now(tz=timezone(timedelta(hours=8))).isoformat()
        entry = f"\n## {ts}\n{content.strip()}\n"
        existing = self._read(path)
        full_content = (existing + entry).strip() + "\n"
        result = self._writer.write(path, full_content, backup=False)
        if not result.ok:
            logger.error("主题记忆写入失败: %s -> %s", topic_name, result.error)
            raise IOError(f"写入主题记忆失败: {result.error}")
        logger.info("主题记忆已写入: %s -> %s", topic_name, path)

        # 实时同步 MEMORY.md 索引（DreamPipeline 后续全量重写时会自然消化）
        self._append_to_index(topic_name, content)
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

    def read_index(self) -> str:
        """读取 MEMORY.md 索引（autoDream 产物，轻量摘要）。"""
        if self._session_dir is None:
            return ""
        path = self._index_path()
        return self._read(path)


    def read_recent_days(self, n_days: int = 3) -> str:
        """读取最近 N 天的会话记忆。"""
        if self._session_dir is None:
            return ""
        parts = []
        for i in range(n_days):
            from datetime import timedelta
            d = datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(days=i)
            content = self.read_day(d.strftime("%Y-%m-%d"))
            if content.strip():
                parts.append(content.strip())
        return "\n\n---\n\n".join(parts) if parts else ""

    # ====== 缺失的基础方法 ======

    @staticmethod
    def _today_str() -> str:
        return datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _require_session(self) -> bool:
        """无 session_id 时返回 False，调用方应安全降级。"""
        return self._session_dir is not None

    def _index_path(self) -> Path:
        return self._session_dir / "MEMORY.md" if self._session_dir else self._base / "MEMORY.md"

    def _day_path(self, date_str: str) -> Path:
        return self._session_dir / f"{date_str}.md" if self._session_dir else Path()

    def read_day(self, date_str: str) -> str:
        """读取指定日期的完整记忆文件。"""
        return self._read(self._day_path(date_str))

    def list_days(self, limit: int = 50) -> list:
        """列出可用日期文件（按时间倒序）。"""
        if not self._session_dir:
            return []
        files = sorted(self._session_dir.glob("????-??-??.md"), reverse=True)
        return [f.stem for f in files if f.name != "MEMORY.md"][:limit]

    def search(self, query: str) -> str:
        """跨所有日期文件搜索关键词。"""
        if not self._session_dir:
            return ""
        results = []
        for day_file in sorted(self._session_dir.glob("????-??-??.md"), reverse=True):
            if day_file.name == "MEMORY.md":
                continue
            content = self._read(day_file)
            if query.lower() in content.lower():
                preview = content[:500]
                results.append(f"--- {day_file.stem} ---\n{preview}")
                if len(results) >= 5:
                    break
        return "\n\n".join(results) if results else ""

    def remember(self, content: str) -> Path:
        """追加内容到今日记忆文件。"""
        if self._session_dir is None:
            raise RuntimeError("会话目录未初始化（session_dir is None）")
        ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%H:%M")
        path = self._session_dir / f"{self._today_str()}.md"
        existing = self._read(path)
        entry = f"\n## {ts}\n{content.strip()}"
        full_content = (existing + entry).strip() + "\n"
        result = self._writer.write(path, full_content, backup=False)
        if not result.ok:
            logger.error("会话记忆写入失败: %s -> %s", path, result.error)
            raise IOError(f"写入会话记忆失败: {result.error}")
        logger.info("会话记忆已保存: %s", path)
        return path

    def list_all_sessions(self) -> list:
        """列出项目中所有 session ID（按时间倒序）。"""
        sessions_dir = self._base / "sessions"
        if not sessions_dir.exists():
            return []
        return sorted(
            (d.name for d in sessions_dir.iterdir() if d.is_dir()),
            reverse=True,
        )

    def delete_session(self, session_id: str) -> bool:
        """递归删除指定 session 目录及其所有内容。"""
        import shutil
        target = self._base / "sessions" / session_id
        if not target.exists():
            return False
        shutil.rmtree(target)
        logger.info("Session 已删除: %s", session_id)
        return True

    # ====== 索引实时同步（write_topic → MEMORY.md 追加一行） ======

    # MEMORY.md 索引长度保护常量
    _INDEX_MAX_SUMMARY_CHARS = 200  # 每条摘要最多 200 字符（索引，不是全文）
    _INDEX_MAX_LINES = 200          # 超过则裁剪最旧条目

    def _append_to_index(self, topic_name: str, content: str) -> None:
        """向 MEMORY.md 追加一行索引摘要（不影响 Dream 后续全量重写）。

        实时路径：write_topic 时自动调用，让后续会话能立即感知新 topic。
        DreamPipeline 后续全量重写 MEMORY.md 时会自然消化这些行。

        长度保护：
        - 摘要最长 200 字符 + 省略号（索引只做指针，完整内容在 topics/）
        - 条目严格单行（换行替换为空格）
        - MEMORY.md 超过 200 行时裁剪最旧条目
        """
        from datetime import datetime, timedelta

        ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        # 首行作为摘要，替换换行为空格，截断到上限
        raw_summary = content.strip().split("\n")[0].replace("\n", " ")
        max_chars = self._INDEX_MAX_SUMMARY_CHARS
        if len(raw_summary) > max_chars:
            summary = raw_summary[:max_chars] + "…"
        else:
            summary = raw_summary

        cat_map = {
            "user-preferences":   "User Preferences",
            "project-decisions":  "Project Decisions",
            "lessons-learned":    "Lessons Learned",
            "errors-fixed":       "Errors Fixed",
            "project-progress":   "Project Progress",
        }
        cat = cat_map.get(topic_name, topic_name)
        entry = f"- [{ts}] {summary}  → topics/{topic_name}.md\n"

        idx_path = self._index_path()
        existing = self._read(idx_path)
        heading = f"### {cat}"

        if heading in existing:
            new_content = self._insert_under_heading(existing, heading, entry)
        else:
            new_content = (existing.strip() + f"\n\n{heading}\n{entry}"
                           if existing.strip() else f"{heading}\n{entry}")

        # 行数保护：超过上限则裁剪最旧条目（保留前导注释行）
        lines = new_content.strip().split("\n")
        if len(lines) > self._INDEX_MAX_LINES:
            kept = lines[:self._INDEX_MAX_LINES]
            new_content = "\n".join(kept).strip() + "\n"
            logger.info("MEMORY.md 超过 %d 行，已裁剪至 %d 行",
                         self._INDEX_MAX_LINES, len(kept))

        result = self._writer.write(idx_path, new_content.strip() + "\n", backup=False)
        if not result.ok:
            logger.warning("MEMORY.md 索引同步失败: %s -> %s", topic_name, result.error)
        else:
            logger.debug("MEMORY.md 索引已同步: %s", topic_name)

    @staticmethod
    def _insert_under_heading(text: str, heading: str, entry: str) -> str:
        """在指定 heading 行后插入条目（下一 heading 或文件末尾之前）。"""
        idx = text.find(heading)
        if idx == -1:
            return text + "\n" + entry

        # heading 后的换行位置
        newline_idx = text.find("\n", idx + len(heading))
        if newline_idx == -1:
            return text + "\n" + entry

        insert_pos = newline_idx + 1
        return text[:insert_pos] + entry + text[insert_pos:]

    # ====== 索引 & 清理 ======

    def write_index(self, content: str) -> Path:
        """写入 MEMORY.md 索引（autoDream 产物）。"""
        path = self._index_path()
        full_content = content.strip() + "\n"
        result = self._writer.write(path, full_content, backup=False)
        if not result.ok:
            logger.error("索引写入失败: %s -> %s", path, result.error)
            raise IOError(f"写入索引失败: {result.error}")
        logger.info("索引已更新: %s", path)
        return path

    def prune_old_days(self) -> int:
        """删除超过 7 天的旧每日记忆文件，返回删除数量。"""
        if self._session_dir is None:
            return 0
        cutoff = datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(days=_PRUNE_DAYS)
        deleted = 0
        for day_file in self._session_dir.glob("????-??-??.md"):
            try:
                dt = datetime.strptime(day_file.stem, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
            except ValueError:
                continue
            if dt < cutoff:
                day_file.unlink()
                deleted += 1
                logger.debug("清理过期记忆: %s", day_file.name)
        if deleted:
            logger.info("prune_old_days: 已删除 %d 个过期文件", deleted)
        return deleted
