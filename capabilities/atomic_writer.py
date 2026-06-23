"""
capabilities/atomic_writer.py
AtomicWriter — 原子写入器，所有写入工具的底层基类。

保证：
  - 原子性：临时文件 + rename（要么全成功要么全失败）
  - 可恢复：版本化备份（带时间戳，不覆盖）
  - 可验证：写入后回读校验

用法:
    writer = AtomicWriter()
    result = writer.write(path, content)        # 覆盖写入（代码文件）
    result = writer.append(path, content)       # 追加写入（记忆文件）
"""

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = ".chacha_agent/backups"
MAX_BACKUP_VERSIONS = 5
BACKUP_RETENTION_DAYS = 7
PREVIEW_LENGTH = 80


@dataclass
class WriteResult:
    """写入结果摘要，返回给 LLM 的结构化反馈。"""
    ok: bool
    path: str
    action: str                    # created | updated | appended
    verified: bool
    backup: Optional[str] = None   # 备份路径（仅项目文件）
    preview: str = ""              # 前 N 字预览
    error: Optional[str] = None

    def to_json_str(self) -> str:
        """单行 JSON，LLM 友好。"""
        import json as _json
        return _json.dumps({
            "ok": self.ok,
            "path": self.path,
            "action": self.action,
            "verified": self.verified,
            "backup": self.backup,
            "preview": self.preview[:PREVIEW_LENGTH],
            "error": self.error,
        }, ensure_ascii=False)


class AtomicWriter:
    """原子写入器。

    写入策略：
      - write():  覆盖写入 + 版本化备份（项目文件）
      - append(): 追加写入，无备份（系统事件 / 记忆）
    """

    def __init__(self, root: Optional[Path] = None):
        """
        Args:
            root: 项目根目录。None 时自动检测（取 cwd）。
        """
        self._root = (root or Path.cwd()).resolve()
        self._backup_dir = self._root / BACKUP_DIR_NAME

    # ====== 公开 API ======

    def write(self, path: Path, content: str, backup: bool = True) -> WriteResult:
        """原子覆盖写入。

        Args:
            path: 目标文件路径
            content: 要写入的完整内容
            backup: 是否创建版本化备份（默认 True，项目文件用）

        Returns:
            WriteResult — ok/verified/backup/preview/error
        """
        action = "updated" if path.exists() else "created"

        # 1. 备份（写入前，保留旧内容）
        backup_path = None
        if backup and path.exists():
            backup_path = self._create_backup(path)

        # 2. 原子写入
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)  # 原子 rename
        except Exception as e:
            tmp.unlink(missing_ok=True)
            logger.error("写入失败: %s → %s", path, e)
            return WriteResult(
                ok=False, path=str(path), action=action,
                verified=False, backup=str(backup_path) if backup_path else None,
                preview="", error=str(e),
            )

        # 3. 验证
        verified = self._verify(path, content)

        preview = content.strip()[:PREVIEW_LENGTH]
        if len(content.strip()) > PREVIEW_LENGTH:
            preview += "..."

        logger.info(
            "写入完成: %s | 校验=%s | 备份=%s",
            path.name, verified, backup_path,
        )

        return WriteResult(
            ok=True, path=str(path), action=action,
            verified=verified,
            backup=str(backup_path) if backup_path else None,
            preview=preview, error=None,
        )

    def append(self, path: Path, entry: str) -> WriteResult:
        """原子追加写入（无备份）。

        用于记忆文件：remember / write_topic。
        先读已有内容，追加新条目，再原子写入。

        Args:
            path: 目标文件路径
            entry: 要追加的内容（会自动加前导换行）

        Returns:
            WriteResult
        """
        action = "updated" if path.exists() else "created"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        new_content = (existing.rstrip() + "\n" + entry.strip() + "\n").lstrip()

        return self.write(path, new_content, backup=False)

    # ====== 备份管理 ======

    def _create_backup(self, file_path: Path) -> Path:
        """创建版本化备份。"""
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 备份子目录：按文件路径
        try:
            rel = str(file_path.relative_to(self._root))
        except ValueError:
            rel = file_path.name
        safe_name = rel.replace("/", "_").replace("\\", "_")
        backup_subdir = self._backup_dir / safe_name
        backup_subdir.mkdir(parents=True, exist_ok=True)

        # 版本文件名：时间戳
        ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = backup_subdir / f"{ts}.bak"

        shutil.copy2(file_path, backup_path)
        logger.debug("备份: %s → %s", file_path.name, backup_path)

        # 清理旧版本
        self._prune_backups(backup_subdir)

        return backup_path

    def _prune_backups(self, backup_subdir: Path) -> None:
        """清理超量/超期备份。"""
        files = sorted(backup_subdir.glob("*.bak"))
        if not files:
            return

        cutoff = datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(days=BACKUP_RETENTION_DAYS)

        for f in files:
            # 按时间戳解析文件名: YYYY-MM-DD_HH-MM-SS.bak
            try:
                ts = datetime.strptime(f.stem, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone(timedelta(hours=8)))
            except ValueError:
                continue
            if ts < cutoff:
                f.unlink()
                logger.debug("清理过期备份: %s", f.name)

        # 如果还超过 MAX_BACKUP_VERSIONS，删最旧的
        remaining = sorted(backup_subdir.glob("*.bak"))
        while len(remaining) > MAX_BACKUP_VERSIONS:
            remaining[0].unlink()
            logger.debug("清理超量备份: %s", remaining[0].name)
            remaining = remaining[1:]

    # ====== 验证 ======

    @staticmethod
    def _verify(path: Path, expected: str) -> bool:
        """回读校验：写入内容和预期一致。"""
        try:
            actual = path.read_text(encoding="utf-8")
            return actual == expected
        except Exception:
            return False
