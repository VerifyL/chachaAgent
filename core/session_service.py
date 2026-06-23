"""
core/session_service.py
SessionService — 会话编排层。统一管理 session 生命周期 + 记忆 + dream。

CLI / Web / API 前端只需调用此 service，不直接操作 MemoryManager。
"""

import asyncio
import time
from datetime import timedelta,  datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.context.memory_manager import MemoryManager
from core.project_init import ProjectInit


class SessionService:
    """会话编排服务"""

    # ---- 默认阈值（chachaConfig.toml 可覆盖） ----
    DREAM_ROUNDS = 10
    DREAM_HOURS = 24
    GLOBAL_DREAM_ROUNDS = 50
    GLOBAL_DREAM_HOURS = 72

    def __init__(self, project_root: Path, llm_invoker=None,
                 dream_rounds: int = None, dream_hours: int = None,
                 global_dream_rounds: int = None, global_dream_hours: int = None,
                 telemetry=None):
        self._root = project_root
        self._llm = llm_invoker
        self._telemetry = telemetry

        # 阈值：chachaConfig.toml → 传入参数 → 默认值
        auto = {}
        try:
            from core.config_manager import get_config_manager
            cfg = get_config_manager().load()
            auto = getattr(cfg, "auto_memory", None) or {}
        except Exception:
            pass  # 无配置文件，用默认值

        self._dream_rounds = (dream_rounds or
                              auto.get("dream_rounds") or self.DREAM_ROUNDS)
        self._dream_hours = (dream_hours or
                             auto.get("dream_hours") or self.DREAM_HOURS)
        self._global_dream_rounds = (global_dream_rounds or
                                     auto.get("global_dream_rounds") or self.GLOBAL_DREAM_ROUNDS)
        self._global_dream_hours = (global_dream_hours or
                                    auto.get("global_dream_hours") or self.GLOBAL_DREAM_HOURS)

        # 当前 session
        self._session_id = self._gen_id()
        self._init = ProjectInit(self._root, self._session_id)
        self._memory = self._init.memory_manager

        # 审计
        self.total_tokens = 0
        self.rounds = 0
        self._history: List[Dict[str, Any]] = []
        self._dream_hints = 0
        self._last_dream_at: Optional[float] = None

    # ====== Getters ======

    @property
    def memory_manager(self) -> MemoryManager:
        return self._memory

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def current_id(self) -> str:
        return f"{self._session_id[:8]}-{self._session_id[9:]}"

    @property
    def project_init(self) -> ProjectInit:
        return self._init

    def set_llm(self, llm_invoker) -> None:
        self._llm = llm_invoker

    # ====== Session 生命周期 ======

    def new(self) -> str:
        """新建 session"""
        self._session_id = self._gen_id()
        self._init = ProjectInit(self._root, self._session_id)
        self._memory = self._init.memory_manager
        self.total_tokens = 0
        self.rounds = 0
        self._history.clear()
        self._dream_hints = 0
        return self._session_id

    async def switch_to(self, new_sid: str) -> str:
        """切换到指定 session"""
        all_sessions = self._memory.list_all_sessions()
        if new_sid not in all_sessions:
            return f"Session 不存在: {new_sid}"
        if new_sid == self._session_id:
            return "已经是当前 session"

        # 切换前 dream 旧 session
        await self._maybe_dream()

        # 切换
        self._session_id = new_sid
        self._init = ProjectInit(self._root, self._session_id)
        self._memory = self._init.memory_manager
        self.total_tokens = 0
        self.rounds = 0
        self._history.clear()
        self._dream_hints = 0
        return f"✅ 已切换到: {new_sid}"

    async def delete_session(self, sid: str) -> str:
        """删除 session"""
        if sid == self._session_id:
            return "❌ 不能删除当前 session"
        ok = MemoryManager(project_root=self._root).delete_session(sid)
        return f"✅ 已删除: {sid}" if ok else f"删除失败: {sid}"

    def list_sessions(self) -> list[dict]:
        """列出所有 session（供 UI）"""
        mgr = MemoryManager(project_root=self._root)
        sessions = mgr.list_all_sessions()
        result = []
        for sid in sessions:
            smgr = MemoryManager(project_root=self._root, session_id=sid)
            preview = ""
            days = smgr.list_days(limit=5)
            for day in days:
                for line in smgr.read_day(day).split("\n"):
                    if line.strip().startswith("Q:"):
                        preview = line.strip()[2:].strip()[:60]
                        break
                if preview:
                    break
            if not preview:
                preview = "(新 session)"
            result.append({
                "id": sid,
                "preview": preview,
                "time": f"{sid[:8]} {sid[9:13]}:{sid[13:15]}" if len(sid) > 14 else sid,
            })
        return result

    # ====== Dream ======

    async def run_dream(self, sid: str = "") -> str:
        """运行 Session Dream"""
        if not self._llm:
            return "Dream: 未配置 LLM"
        from core.context.dream import DreamPipeline
        mgr = MemoryManager(
            project_root=self._root,
            session_id=sid or self._session_id,
        )
        pipeline = DreamPipeline(self._llm)
        await pipeline.run(mgr)
        return "Dream 完成"

    async def run_global_dream(self) -> str:
        """运行 Global Dream（跨 session 学习）"""
        from core.context.dream import GlobalDream
        gd = GlobalDream.get_instance(
            dream_rounds=self._global_dream_rounds,
            dream_hours=self._global_dream_hours,
        )
        gd.configure(llm_invoker=self._llm)
        return await gd.run()

    # ====== 审计 ======

    def add_round(self, tokens: int = 0, duration_ms: int = 0,
                  errors=None, user_input: str = "", assistant_text: str = "") -> None:
        self.total_tokens += tokens
        self.rounds += 1
        self._history.append({
            "round": self.rounds, "tokens": tokens,
            "duration_ms": duration_ms, "errors": errors or [],
            "time": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
        })
        # 写入记忆
        self._save_memory(user_input, assistant_text)
        # 触发 Dream 检查
        self._dream_hints += 1
        # GlobalDream 计数（阈值可配）
        from core.context.dream import GlobalDream
        gd = GlobalDream.get_instance(
            dream_rounds=self._global_dream_rounds,
            dream_hours=self._global_dream_hours,
        )
        gd.configure(llm_invoker=self._llm)
        gd.record_round()

        # 遥测：会话统计
        tel = self._telemetry
        if tel and tel.agent and tel.logger:
            tel.agent.record_session(self.session_id, self.total_tokens, 0.0, int(duration_ms / max(self.rounds, 1)))
            err_msgs = errors or []
            tel.logger.info("本轮完成", round=self.rounds, tokens=tokens, duration_ms=duration_ms,
                           total_tokens=self.total_tokens, errors=err_msgs)

    def audit_report(self) -> str:
        lines = [
            f"会话: {self.current_id}",
            f"Token: {self.total_tokens} | 轮次: {self.rounds}",
            f"记录: {len(self._history)} | Dream提示: {self._dream_hints}",
        ]
        if self._history:
            avg = self.total_tokens // max(self.rounds, 1)
            dur = sum(h["duration_ms"] for h in self._history)
            errs = sum(1 for h in self._history if h["errors"])
            lines += [f"平均Token/轮: {avg}", f"总耗时: {dur//1000}s", f"错误轮次: {errs}"]
        return "\n".join(lines)

    def status_report(self) -> str:
        return (
            f"会话: {self.current_id}\nToken: {self.total_tokens} | 轮次: {self.rounds}\n"
            f"Dream提示: {self._dream_hints}"
        )

    # ====== 内部 ======

    def _save_memory(self, user_input: str, assistant_text: str) -> None:
        if not user_input and not assistant_text:
            return
        try:
            entry = f"Q: {user_input.strip()[:80]}\nA: {assistant_text.strip()[:120]}"
            self._memory.remember(entry)
        except Exception:
            pass

    async def _maybe_dream(self) -> None:
        rounds_ok = self._dream_hints >= self._dream_rounds
        time_ok = (self._last_dream_at and
                   time.time() - self._last_dream_at > self._dream_hours * 3600)
        if (rounds_ok or time_ok) and self._llm:
            await self.run_dream(self._session_id)
            self._dream_hints = 0
            self._last_dream_at = time.time()

    @staticmethod
    def _gen_id() -> str:
        return datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
