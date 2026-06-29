"""
core/checkpoint_manager.py
CheckpointManager — 会话检查点保存与恢复。

设计理念：
1. 轻量保存：消息 dict 列表直接 JSON 序列化，不再经过 ConversationState
2. 文件级快照：{dir}/{session_id}/checkpoint.json
3. 恢复：直接返回 List[dict]，可直接喂给 LLM
4. 清理：purge() 删除 N 小时前的旧检查点

用法:
    mgr = CheckpointManager()
    mgr.save(messages, session_id="session-abc", description="手动保存")
    messages = mgr.restore("session-abc")
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_DIR = Path(".chacha_agent/checkpoints")


class CheckpointManager:
    """会话检查点管理器"""

    def __init__(self, base_dir: Optional[Path] = None):
        self._dir = base_dir or DEFAULT_CHECKPOINT_DIR

    # ====== 保存 ======

    def save(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        description: Optional[str] = None,
    ) -> None:
        """保存消息列表为检查点。

        Args:
            messages: 消息 dict 列表 (role/content 格式)
            session_id: 会话 ID
            description: 保存原因/标签
        """
        cp_dir = self._dir / session_id
        cp_dir.mkdir(parents=True, exist_ok=True)

        # 过滤：去掉 tool 消息、tool_calls、reasoning_content
        trimmed = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                continue
            entry = {k: v for k, v in m.items()
                     if k not in ("tool_calls", "reasoning_content")}
            trimmed.append(entry)

        data = {
            "session_id": session_id,
            "saved_at": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
            "description": description,
            "message_count": len(trimmed),
            "messages": trimmed,
        }

        cp_path = cp_dir / "checkpoint.json"
        cp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info("检查点已保存: %s (messages=%d)", cp_path, len(trimmed))

    # ====== 恢复 ======

    def restore(
        self,
        session_id: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """恢复会话的消息列表。

        返回 None 表示无可用检查点。
        """
        cp_dir = self._dir / session_id
        cp_path = self._latest(cp_dir)

        if not cp_path or not cp_path.exists():
            logger.warning("检查点不存在: session=%s", session_id)
            return None

        try:
            data = json.loads(cp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error("检查点文件损坏: %s", cp_path)
            return None

        messages = data.get("messages", [])
        logger.info("会话已恢复: %s (messages=%d)", session_id, len(messages))
        return messages

    # ====== 列表 ======

    def list(self, session_id: str) -> List[Dict]:
        """列出会话的检查点摘要"""
        cp_dir = self._dir / session_id
        if not cp_dir.exists():
            return []

        result = []
        for f in sorted(cp_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                result.append({
                    "checkpoint_id": f.stem,
                    "session_id": data.get("session_id"),
                    "message_count": data.get("message_count", 0),
                    "description": data.get("description"),
                    "saved_at": data.get("saved_at", ""),
                })
            except Exception:
                continue
        return result

    # ====== 删除 ======

    def delete(self, session_id: str) -> bool:
        """删除指定会话的检查点"""
        cp_path = self._dir / session_id / "checkpoint.json"
        if cp_path.exists():
            cp_path.unlink()
            # 如果目录为空也删除
            cp_dir = cp_path.parent
            if cp_dir.exists() and not any(cp_dir.iterdir()):
                cp_dir.rmdir()
            logger.info("检查点已删除: %s", cp_path)
            return True
        return False

    def purge(self, session_id: str, max_age_hours: float = 72) -> int:
        """清理 N 小时前的旧检查点。返回删除数量。"""
        cp_dir = self._dir / session_id
        if not cp_dir.exists():
            return 0

        cutoff = datetime.now(tz=timezone(timedelta(hours=8))).timestamp() - max_age_hours * 3600
        files = sorted(cp_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)

        deleted = 0
        for f in files[1:]:  # 保留最新 1 个
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1

        logger.info("清理旧检查点: session=%s, deleted=%d", session_id, deleted)
        return deleted

    # ====== 内部 ======

    @staticmethod
    def _latest(cp_dir: Path) -> Optional[Path]:
        files = sorted(cp_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        return files[0] if files else None
