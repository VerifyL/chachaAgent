"""
interface/cli/session_manager.py
SessionManager — CLI 会话生命周期 + 审计追踪。

v2.0 新增:
  - tool_cache 清理
  - 每轮记忆保存（_save_round_memory）
  - DreamPipeline 10 次会话或 24h 触发
  - should_dream() 使用 session_count
"""

import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SessionManager:
    """CLI 会话管理器（v2.0）"""

    def __init__(self, project_root: Path, bridge: Optional[Any] = None):
        self._project = project_root
        self._bridge = bridge
        self._session_id = self._gen_id()
        self.total_tokens = 0
        self.rounds = 0
        self._history: List[Dict[str, Any]] = []
        self._last_dream_at: Optional[float] = None
        self._dream_hints = 0
        self._last_user_input = ""
        self._last_assistant_text = ""

    # ====== 会话 ======

    def new(self) -> str:
        """开始新会话：生成 ID，清理 tool_cache。"""
        # 先清理旧会话的 tool_cache
        self.cleanup_tool_cache()

        self._session_id = self._gen_id()
        self.total_tokens = 0
        self.rounds = 0
        self._history.clear()
        self._dream_hints = 0
        self._last_user_input = ""
        self._last_assistant_text = ""
        return self._session_id

    def save(self) -> str:
        """保存会话：触发 DreamPipeline 检查。"""
        self.record_dream_hint()
        if self._bridge and hasattr(self._bridge, 'run_dream'):
            import asyncio
            try:
                # 检查是否应触发 dream
                if self.should_dream():
                    asyncio.create_task(self._bridge.run_dream())
                    self.mark_dream_run()
            except Exception:
                pass
        return self._session_id

    def add_round(
        self, tokens: int = 0, duration_ms: int = 0,
        errors: Optional[List[str]] = None,
        user_input: str = "",
        assistant_text: str = "",
    ) -> None:
        """记录一轮对话（审计用）。"""
        self.total_tokens += tokens
        self.rounds += 1
        self._last_user_input = user_input
        self._last_assistant_text = assistant_text
        self._history.append({
            "round": self.rounds,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "errors": errors or [],
            "time": datetime.now(tz=timezone.utc).isoformat(),
        })
        # 每轮结束后自动保存记忆
        #self._save_round_memory(user_input, assistant_text)

    @property
    def current_id(self) -> str:
        return f"{self._session_id[:8]}-{self._session_id[9:]}"

    # ====== 压缩 ======

    async def compact(self) -> str:
        if self._bridge and hasattr(self._bridge, '_messages'):
            n_before = len(self._bridge._messages)
            if n_before > 11:
                self._bridge._messages = (
                    self._bridge._messages[:1] +
                    self._bridge._messages[-10:]
                )
            return f"压缩: {n_before} → {len(self._bridge._messages)} 条"
        return "压缩失败: 桥接未就绪"

    # ====== 审计 ======

    def audit_report(self) -> str:
        lines = [
            f"会话: {self.current_id}",
            f"总 Token: {self.total_tokens}",
            f"总轮次: {self.rounds}",
            f"记录条目: {len(self._history)}",
            f"Dream计数: {self._dream_hints}",
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
        return (
            f"会话: {self.current_id}\n"
            f"Token: {self.total_tokens}  |  轮次: {self.rounds}\n"
            f"记忆: {len(self.list_memory_days())} 日\n"
            f"压缩: {'建议' if self.total_tokens > 80000 else '正常'}\n"
            f"Dream计数: {self._dream_hints}/10"
        )

    # ====== 记忆 ======

    def should_dream(self) -> bool:
        """是否触发 DreamPipeline（10 次提示或 24h）。"""
        from core.context.dream import _DREAM_SESSION_COUNT, _DREAM_HOURS
        count_triggered = self._dream_hints >= _DREAM_SESSION_COUNT
        time_triggered = (
            self._last_dream_at is not None
            and time.time() - self._last_dream_at > _DREAM_HOURS * 3600
        )
        return count_triggered or time_triggered

    def record_dream_hint(self) -> None:
        self._dream_hints += 1

    def mark_dream_run(self) -> None:
        """标记 DreamPipeline 已运行。"""
        self._dream_hints = 0
        self._last_dream_at = time.time()

    def remember_final_answer(self, user_input: str, assistant_text: str) -> None:
        """保存最终回答到记忆（app.py 调用）。"""
        self._save_round_memory(user_input, assistant_text)

    def _save_round_memory(self, user_input: str, assistant_text: str) -> None:
        """每轮对话自动追加到 memory/session/ 当日记忆文件。"""
        if not user_input and not assistant_text:
            return
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(project_root=self._project)
            entry = f"Q: {user_input.strip()}\nA: {assistant_text.strip()}"
            mgr.remember(entry)
        except Exception:
            pass


    def cleanup_tool_cache(self) -> None:
        """清理 memory/tool_cache/ 目录。"""
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(project_root=self._project)
            mgr.cleanup_tool_cache()
        except Exception:
            pass


    def list_memory_days(self, limit: int = 10) -> list:
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(project_root=self._project)
            return mgr.list_days(limit=limit)
        except Exception:
            return []


    # ====== 内部 ======

    @staticmethod
    def _gen_id() -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
