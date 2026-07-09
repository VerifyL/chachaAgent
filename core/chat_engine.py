"""
core/chat_engine.py
ChatEngine — 对话编排核心，管理消息历史 + 上下文压缩。
CLI/Web 均可调用，不依赖任何 UI 框架。
"""

import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.dispatcher import Dispatcher
from core.llm_invoker import LLMInvoker
from core.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)


class ChatEngine:
    """对话引擎：消息历史 + 上下文管理 + 自动压缩 + 检查点。"""

    def __init__(
        self,
        system_prompt: str = "",
        tools: Optional[List] = None,
        context_window: int = 1_048_576,
        compress_cfg: Optional[Dict[str, Any]] = None,
        checkpoint_dir: Optional[Path] = None,
        context_manager: Optional[Any] = None,
    ):
        self._system_prompt = system_prompt
        self._context_window = context_window
        self._compress_cfg = compress_cfg or {}
        self._checkpoint_dir = checkpoint_dir
        self._cm = context_manager
        self._project_root: Optional[Path] = None
        self._llm: Optional[LLMInvoker] = None
        self._dispatcher: Optional[Dispatcher] = None
        self._tool_executor: Optional[ToolExecutor] = None

        self._reset_messages()
        self._try_restore()

    # ====== 消息 ======

    def _reset_messages(self) -> None:
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
        ]

    def set_llm(self, llm: LLMInvoker) -> None:
        self._llm = llm

    def set_dispatcher(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher
        self._tool_executor = getattr(dispatcher, "_tools", None)

    @property
    def dispatcher(self) -> Optional[Dispatcher]:
        """公开 Dispatcher（供 Orchestrator 直接调用）。"""
        return self._dispatcher

    def rebuild(self, tools=None) -> None:
        """重新装配工具/Dispatcher（当前由 AgentBridge.rebuild() 统一管理）。


        ChatEngine 自身不负责 PolicyEngine/ToolExecutor/Dispatcher 的创建，
        避免与 AgentBridge.rebuild() 产生双实例冲突。
        如未来 ChatEngine 需要独立运行，通过 agent_bridge 注入完成。
        """
        pass

    # ====== 检查点 ======

    def set_checkpoint_dir(self, path: Path) -> None:
        self._checkpoint_dir = path
        from core.checkpoint_manager import CheckpointManager

        self._checkpoint_mgr = CheckpointManager(base_dir=path.parent)
        self._session_id = path.name
        self._try_restore()
        # 更新 ContextManager 的全量记忆（跨 session 切换后刷新）
        if self._cm and self._project_root:
            from pathlib import Path

            from core.context.memory_manager import MemoryManager

            try:
                mgr = MemoryManager(project_root=self._project_root)
                idx = mgr.read_index()
                if idx:
                    self._cm.set_memory_index(idx)
                perm = mgr.read_permanent_memory()
                if perm:
                    self._cm.set_permanent_memory(perm)
                user_path = Path.home() / ".chacha" / "USER_MEMORY.md"
                if user_path.exists():
                    self._cm.set_global_permanent_memory(user_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def _try_restore(self) -> None:
        if not self._checkpoint_dir:
            return

        if self._checkpoint_mgr and self._session_id:
            try:
                msgs = self._checkpoint_mgr.restore(self._session_id)
                if msgs:
                    self._messages = msgs
                    return
            except Exception:
                pass

    def save_checkpoint(self) -> None:
        if not self._checkpoint_dir:
            return
        if self._checkpoint_mgr and self._session_id:
            try:
                self._checkpoint_mgr.save(
                    self._messages,
                    session_id=self._session_id,
                )
            except Exception:
                pass

    def restore_checkpoint(self) -> None:
        """回滚到上次保存的 checkpoint（用于取消对话后恢复干净上下文）。"""
        self._reset_messages()
        self._try_restore()

    # ====== 发送消息 ======

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        """简化版：仅追加消息 + 委托 Dispatcher（完整编排已迁入 Orchestrator.run_stream）。"""
        if not self._dispatcher:
            yield {"type": "error", "message": "未初始化"}
            return

        self._messages.append({"role": "user", "content": user_input})
        t0 = time.monotonic()
        sid = self._checkpoint_dir.stem if self._checkpoint_dir else f"chat-{int(t0)}"

        try:
            async for chunk in self._dispatcher.dispatch_stream(
                messages=self._messages,
                session_id=sid,
                max_rounds=200,
            ):
                yield chunk
        except GeneratorExit:
            return
        except Exception as e:
            yield {"type": "error", "message": str(e)}

    def reset(self) -> None:
        self._reset_messages()

    # ====== 上下文窗口推断 ======

    @staticmethod
    def infer_context_window(model: str) -> int:
        """根据模型名推断上下文窗口（与 agent_bridge 共享）。"""
        m = model.lower()
        if any(k in m for k in ("deepseek-v4", "deepseek-v3", "deepseek-r1", "gemini-2", "gemini-1.5", "gemini")):
            return 1_048_576
        if "claude" in m:
            return 200_000
        if "gpt-4" in m or "gpt-4o" in m:
            return 128_000
        if "llama" in m or "qwen" in m or "mistral" in m or "mixtral" in m:
            return 128_000
        return 128_000  # 未知模型保守 128K
