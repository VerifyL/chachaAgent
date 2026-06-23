"""
core/checkpoint_manager.py
CheckpointManager — 会话检查点保存与恢复。

设计理念：
1. 全量保存：ConversationState JSON 完整序列化（events + metadata + loop_state）
2. 文件级快照：{dir}/{session_id}/{checkpoint_id}.json
3. 恢复：加载指定或最新检查点 → model_validate_json() → ConversationState
4. 清理：purge() 删除 N 小时前的旧检查点

用法:
    mgr = CheckpointManager()
    await mgr.save(state, "用户手动保存")
    state = await mgr.restore("session-abc")
"""

import json
import logging
import os
from datetime import timedelta,  datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.models.session import (
    ConversationState, SessionCheckpoint, SessionMetadata,
)

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_DIR = Path(".chacha_agent/checkpoints")


class CheckpointManager:
    """会话检查点管理器"""

    def __init__(self, base_dir: Optional[Path] = None):
        self._dir = base_dir or DEFAULT_CHECKPOINT_DIR

    # ====== 保存 ======

    def save(
        self,
        state: ConversationState,
        description: Optional[str] = None,
    ) -> SessionCheckpoint:
        """保存当前会话状态为检查点。

        返回创建的 SessionCheckpoint 对象。
        """
        sid = state.metadata.session_id
        event_index = len(state.events)

        cp = SessionCheckpoint(
            description=description,
            event_index=event_index,
            metadata_snapshot=state.metadata,
            loop_state_snapshot=state.loop_state,
        )

        # 写入文件
        cp_dir = self._dir / sid
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = cp_dir / f"{cp.checkpoint_id}.json"

        data = state.model_dump(mode="json")
        cp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 追加到 state 的检查点列表
        state.checkpoints.append(cp)

        logger.info("检查点已保存: %s (events=%d)", cp_path, event_index)
        return cp

    # ====== 恢复 ======

    def restore(
        self,
        session_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> Optional[ConversationState]:
        """恢复会话。

        checkpoint_id=None 时恢复最新检查点。
        返回 None 表示无可用检查点。
        """
        cp_dir = self._dir / session_id

        if checkpoint_id:
            cp_path = cp_dir / f"{checkpoint_id}.json"
        else:
            cp_path = self._latest(cp_dir)

        if not cp_path or not cp_path.exists():
            logger.warning("检查点不存在: %s", cp_path)
            return None

        try:
            data = json.loads(cp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error("检查点文件损坏: %s", cp_path)
            return None

        state = ConversationState.model_validate(data)
        logger.info("会话已恢复: %s (events=%d)", session_id, len(state.events))
        return state

    # ====== 列表 ======

    def list(self, session_id: str) -> List[Dict]:
        """列出会话的所有检查点"""
        cp_dir = self._dir / session_id
        if not cp_dir.exists():
            return []

        result = []
        for f in sorted(cp_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                meta = data.get("metadata", {})
                checkpoints = data.get("checkpoints", [])
                last_cp = checkpoints[-1] if checkpoints else {}
                result.append({
                    "checkpoint_id": f.stem,
                    "session_id": meta.get("session_id"),
                    "events_count": len(data.get("events", [])),
                    "total_tokens": meta.get("total_tokens", 0),
                    "description": last_cp.get("description"),
                    "created_at": meta.get("updated_at", ""),
                })
            except Exception:
                continue
        return result

    # ====== 删除 ======

    def delete(self, session_id: str, checkpoint_id: str) -> bool:
        """删除指定检查点"""
        cp_path = self._dir / session_id / f"{checkpoint_id}.json"
        if cp_path.exists():
            cp_path.unlink()
            logger.info("检查点已删除: %s", cp_path)
            return True
        return False

    def purge(self, session_id: str, max_age_hours: float = 72) -> int:
        """清理 N 小时前的旧检查点，保留最新的至少 1 个。返回删除数量。"""
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
