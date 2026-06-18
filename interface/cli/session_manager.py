"""
interface/cli/session_manager.py
SessionManager — CLI 会话生命周期 + 审计追踪。

职责:
  - 会话 id / token / 轮次 / 耗时 追踪
  - 记忆整合触发（DreamPipeline）
  - 上下文压缩（ContextCompressor）
  - 审计报告（/audit /trace）
"""

import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SessionManager:
    """CLI 会话管理器"""

    def __init__(self, project_root: Path, bridge: Optional[Any] = None):
        self._project = project_root
        self._bridge = bridge
        self._session_id = self._gen_id()
        self.total_tokens = 0
        self.rounds = 0
        self._history: List[Dict[str, Any]] = []  # 审计追踪
        self._last_dream_at: Optional[float] = None
        self._dream_hints = 0

    # ====== 会话 ======

    def new(self) -> str:
        self._session_id = self._gen_id()
        self.total_tokens = 0
        self.rounds = 0
        self._history.clear()
        self._dream_hints = 0
        return self._session_id

    def save(self) -> str:
        """保存会话（TODO: 持久化 ConversationState + 触发 DreamPipeline）"""
        return self._session_id

    def add_round(
        self, tokens: int = 0, duration_ms: int = 0,
        errors: Optional[List[str]] = None,
        user_input: str = "",
        assistant_text: str = "",
    ) -> None:
        """记录一轮对话（审计用）。记忆在最终回答后由 remember_final_answer 单独写入。"""
        self.total_tokens += tokens
        self.rounds += 1
        self._history.append({
            "round": self.rounds,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "errors": errors or [],
            "time": datetime.now(tz=timezone.utc).isoformat(),
        })

    @property
    def current_id(self) -> str:
        return f"{self._session_id[:8]}-{self._session_id[9:]}"

    # ====== 压缩 ======

    async def compact(self) -> str:
        """手动触发上下文压缩"""
        if self._bridge and hasattr(self._bridge, '_messages'):
            n_before = len(self._bridge._messages)
            msg_count = n_before

            # 简化压缩：保留系统提示 + 最后 10 条
            if msg_count > 11:
                self._bridge._messages = (
                    self._bridge._messages[:1] +
                    self._bridge._messages[-10:]
                )

            return f"压缩: {msg_count} → {len(self._bridge._messages)} 条 ({msg_count - len(self._bridge._messages)} 已移除)"
        return "压缩失败: 桥接未就绪"

    # ====== 审计 ======

    def audit_report(self) -> str:
        """完整审计报告"""
        lines = [
            f"会话: {self.current_id}",
            f"总 Token: {self.total_tokens}",
            f"总轮次: {self.rounds}",
            f"记录条目: {len(self._history)}",
            f"记忆提示: {self._dream_hints}",
        ]
        if self._history:
            avg_tokens = self.total_tokens // max(self.rounds, 1)
            total_duration = sum(h["duration_ms"] for h in self._history)
            error_rounds = sum(1 for h in self._history if h["errors"])
            lines += [
                f"平均 Token/轮: {avg_tokens}",
                f"总耗时: {total_duration // 1000}s",
                f"错误轮次: {error_rounds}/{self.rounds}",
            ]
        return "\n".join(lines)

    def trace_last(self) -> str:
        """最近一轮的追踪信息"""
        if not self._history:
            return "暂无追踪记录"
        last = self._history[-1]
        return (
            f"轮次 #{last['round']}\n"
            f"Token: {last['tokens']}\n"
            f"耗时: {last['duration_ms']}ms\n"
            f"时间: {last['time'][:19]}\n"
            f"错误: {len(last['errors'])} 个"
        )

    def status_report(self) -> str:
        """状态摘要"""
        return (
            f"会话: {self.current_id}\n"
            f"Token: {self.total_tokens}  |  轮次: {self.rounds}\n"
            f"记忆: {len(self.list_memory_days())} 日\n"
            f"压缩: {'建议' if self.total_tokens > 80000 else '正常'}\n"
            f"Dream记录: {self._dream_hints} 次提示"
        )

    # ====== 记忆 ======

    def should_dream(self) -> bool:
        """是否触发 DreamPipeline"""
        return self._dream_hints >= 3 or (
            self._last_dream_at and time.time() - self._last_dream_at > 86400
        )

    def record_dream_hint(self) -> None:
        self._dream_hints += 1

    def remember_final_answer(self, user_input: str, assistant_text: str) -> None:
        """最终回答后写入记忆（只在一轮完整对话结束时调用）"""
        if not user_input and not assistant_text:
            return
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(
                project_id=self._project.name,
                session_id=self._session_id,
            )
            user_short = user_input[:80].replace("\n", " ")
            asst_short = assistant_text[:120].replace("\n", " ") if assistant_text else "..."
            entry = f"Q: {user_short}\nA: {asst_short}"
            mgr.remember(entry)
        except Exception:
            pass

    # ====== 每轮记忆 ======

    def _auto_remember(self, user_input: str, assistant_text: str) -> None:
        """每轮对话自动追加到当前 session 的当日记忆文件"""
        if not user_input and not assistant_text:
            return
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(
                project_id=self._project.name,
                session_id=self._session_id,
            )

            user_short = user_input[:80].replace("\n", " ")
            asst_short = assistant_text[:120].replace("\n", " ") if assistant_text else "..."

            entry = f"Q: {user_short}\nA: {asst_short}"
            mgr.remember(entry)
        except Exception:
            pass

    def list_memory_days(self, limit: int = 10) -> list:
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(
                project_id=self._project.name,
                session_id=self._session_id,
            )
            return mgr.list_days(limit=limit)
        except Exception:
            return []

    # ====== 内部 ======

    @staticmethod
    def _gen_id() -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
