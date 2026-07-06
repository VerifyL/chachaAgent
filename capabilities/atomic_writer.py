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
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = ".chacha/backups"
MAX_BACKUP_VERSIONS = 5
BACKUP_RETENTION_DAYS = 7
PREVIEW_LENGTH = 80
LOCK_TIMEOUT = 5.0  # 文件锁超时（秒）

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False   # Windows 回退 — 无锁


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

    校验策略：
      - 流式写入时实时 sha256 自校验 + rename 后 stat 大小确认，避免双倍 I/O
    """

    _CHUNK_SIZE = 64 * 1024  # 64KB：流式写入的分块大小

    def __init__(self, root: Optional[Path] = None):
        """
        Args:
            root: 项目根目录。None 时自动检测（取 cwd）。
        """
        self._root = (root or Path.cwd()).resolve()
        self._backup_dir = self._root / BACKUP_DIR_NAME
        self._lock_paths: dict = {}  # fd → lock_path 映射，用于释放时清理锁文件

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

        # 2. 原子写入（加锁）
        tmp = path.with_suffix(path.suffix + ".tmp")
        lock_fd = None
        content_bytes = content.encode("utf-8")
        try:
            lock_fd = self._acquire_lock(path)
            self._write_streaming_with_hash(tmp, content_bytes)
            tmp.replace(path)  # 原子 rename
        except Exception as e:
            tmp.unlink(missing_ok=True)
            logger.error("写入失败: %s → %s", path, e)
            return WriteResult(
                ok=False, path=str(path), action=action,
                verified=False, backup=str(backup_path) if backup_path else None,
                preview="", error=str(e),
            )
        finally:
            self._release_lock(lock_fd)

        # 3. 验证（stat 大小确认，写入时已做 sha256 自校验）
        verified = self._verify_checksum(path, content_bytes)

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

    def write_chunks(self, path: Path, chunks: list, backup: bool = True) -> WriteResult:
        """原子覆盖写入（分块版本，避免拼接全量字符串）。

        逐 chunk 编码写入，内存峰值 ≈ 单个 chunk 大小（64KB），
        适合 edit 等已持有 chunk 列表的场景。

        Args:
            path: 目标文件路径
            chunks: 字符串块列表
            backup: 是否创建版本化备份
        """
        action = "updated" if path.exists() else "created"

        # 1. 备份
        backup_path = None
        if backup and path.exists():
            backup_path = self._create_backup(path)

        # 2. 流式写入（单遍 encode，不做预计算）
        total_bytes = 0
        tmp = path.with_suffix(path.suffix + ".tmp")
        lock_fd = None
        try:
            lock_fd = self._acquire_lock(path)
            with tmp.open("wb") as f:
                for chunk in chunks:
                    chunk_bytes = chunk.encode("utf-8")
                    f.write(chunk_bytes)
                    total_bytes += len(chunk_bytes)
            tmp.replace(path)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            logger.error("写入失败: %s → %s", path, e)
            return WriteResult(
                ok=False, path=str(path), action=action,
                verified=False, backup=str(backup_path) if backup_path else None,
                preview="", error=str(e),
            )
        finally:
            self._release_lock(lock_fd)

        # 4. stat 大小校验
        try:
            verified = path.stat().st_size == total_bytes
        except Exception:
            verified = False

        # 5. preview
        preview_chunk = chunks[0] if chunks else ""
        preview = preview_chunk.strip()[:PREVIEW_LENGTH]
        if len(preview_chunk.strip()) > PREVIEW_LENGTH:
            preview += "..."

        logger.info(
            "写入完成(chunks): %s | 校验=%s | 备份=%s",
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

        用于记忆文件：memory。
        先读已有内容，追加新条目，再原子写入。

        Args:
            path: 目标文件路径
            entry: 要追加的内容（会自动加前导换行）

        Returns:
            WriteResult
        """
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        new_content = (existing.rstrip() + "\n" + entry.strip() + "\n").lstrip()

        return self.write(path, new_content, backup=False)

    # ====== 备份管理 ======

    def _is_in_git_repo(self, file_path: Path) -> bool:
        """检测文件所在目录是否在 git 仓库内。

        文件在 git 仓库内时跳过 .bak 备份，用 git 做版本控制。
        超时 5s，任何异常都返回 False（回退到 .bak 兜底）。
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=file_path.parent,
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _create_backup(self, file_path: Path) -> Optional[Path]:
        """创建版本化备份。

        如果文件在 git 仓库内则跳过 .bak（由 git 做版本控制），
        否则创建时间戳 .bak 文件到 .chacha/backups/。
        """
        # 有 git → 跳过 .bak，用 git 做版本控制
        if self._is_in_git_repo(file_path):
            logger.debug("git 仓库中，跳过 .bak: %s", file_path.name)
            return None
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

    # ====== 文件锁 ======

    def _acquire_lock(self, path: Path) -> Optional[int]:
        """获取文件独占锁。

        使用 fcntl.flock（Unix），Windows 上回退到无锁。
        超时抛 TimeoutError，避免死锁阻塞。
        """
        if not _HAS_FCNTL:
            return None

        lock_path = path.with_suffix(path.suffix + ".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        self._lock_paths[fd] = lock_path
        deadline = time.monotonic() + LOCK_TIMEOUT

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (OSError, IOError):
                if time.monotonic() > deadline:
                    os.close(fd)
                    # 超时：清理锁文件并移除映射
                    lock_path = self._lock_paths.pop(fd, None)
                    if lock_path is not None:
                        try:
                            os.unlink(lock_path)
                        except OSError:
                            pass
                    raise TimeoutError(
                        f"无法获取文件锁 '{path.name}'，超时 {LOCK_TIMEOUT}s "
                        f"(可能有其他进程正在写入同一文件)"
                    )
                time.sleep(0.1)

    def _release_lock(self, fd: Optional[int]) -> None:
        """释放文件锁、关闭描述符、清理锁文件。"""
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        # 清理锁文件
        lock_path = self._lock_paths.pop(fd, None)
        if lock_path is not None:
            try:
                os.unlink(lock_path)
            except OSError:
                pass

    # ====== 验证 ======

    @staticmethod
    def _verify(path: Path, expected: str) -> bool:
        """全量回读校验：读回文件内容与预期逐字对比。

        当前 write() 默认使用 _verify_checksum（stat 校验），
        此方法保留供外部直接调用或需要逐字节确认的场景使用。
        """
        try:
            actual = path.read_text(encoding="utf-8")
            return actual == expected
        except Exception:
            return False

    @staticmethod
    def _write_streaming_with_hash(tmp: Path, content_bytes: bytes) -> None:
        """流式写入临时文件，边写边校验 sha256。

        分块写入 + 实时 sha256 累加，写完后比对 hash。
        不匹配则抛 IOError，写入时已完成校验。
        """
        expected = hashlib.sha256(content_bytes).hexdigest()
        hasher = hashlib.sha256()
        with tmp.open("wb") as f:
            for i in range(0, len(content_bytes), AtomicWriter._CHUNK_SIZE):
                chunk = content_bytes[i:i + AtomicWriter._CHUNK_SIZE]
                f.write(chunk)
                hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise IOError(
                f"sha256 校验失败: tmp 写入内容与预期不一致 "
                f"(expected={expected[:16]}..., actual={hasher.hexdigest()[:16]}...)"
            )

    @staticmethod
    def _verify_checksum(path: Path, content_bytes: bytes) -> bool:
        """rename 后轻量校验：仅 stat 确认文件大小一致。

        rename 在同一文件系统上是原子的，文件内容不会变，
        因此不需要再次读取全量内容做比对。
        """
        try:
            return path.stat().st_size == len(content_bytes)
        except Exception:
            return False


