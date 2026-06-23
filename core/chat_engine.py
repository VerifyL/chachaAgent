"""
core/chat_engine.py
ChatEngine — 对话编排核心，管理消息历史 + 上下文压缩。
CLI/Web 均可调用，不依赖任何 UI 框架。
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.llm_invoker import LLMInvoker
from core.dispatcher import Dispatcher
from core.context.context_compressor import ContextCompressor
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

    def rebuild(self, tools=None) -> None:
        from core.tool_executor import ToolExecutor
        from core.dispatcher import Dispatcher
        from core.policy_engine import PolicyEngine
        if not self._llm:
            return
        policy = PolicyEngine()
        executor = ToolExecutor(tools=tools or [], policy_engine=policy)
        self._dispatcher = Dispatcher(llm_invoker=self._llm, tool_executor=executor)

    # ====== 检查点 ======

    def set_checkpoint_dir(self, path: Path) -> None:
        self._checkpoint_dir = path
        from core.checkpoint_manager import CheckpointManager
        self._checkpoint_mgr = CheckpointManager(base_dir=path.parent)
        self._session_id = path.name
        self._try_restore()
        # 更新 ContextManager 的全量记忆（跨 session 切换后刷新）
        if self._cm and self._project_root:
            from core.context.memory_manager import MemoryManager
            from pathlib import Path
            try:
                mgr = MemoryManager(project_root=self._project_root)
                idx = mgr.read()
                if idx:
                    self._cm.set_memory_index(idx)
                recent = mgr.read_recent_days(3)
                if recent:
                    self._cm.set_session_memory(recent)
                perm = mgr.read_permanent_memory()
                if perm:
                    self._cm.set_permanent_memory(perm)
                user_path = Path.home() / ".chacha" / "USER_MEMORY.md"
                if user_path.exists():
                    self._cm.set_global_permanent_memory(
                        user_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def _try_restore(self) -> None:
        if not self._checkpoint_dir:
            return

        # 优先 CheckpointManager 格式
        if self._checkpoint_mgr and self._session_id:
            try:
                state = self._checkpoint_mgr.restore(self._session_id)
                if state:
                    msgs = state.get_messages_for_llm()
                    self._messages = [
                        m for m in msgs if m.get("role") != "tool"
                    ]
                    for m in self._messages:
                        m.pop("tool_calls", None)
                    return
            except Exception:
                pass

        # 回退旧格式 checkpoint.json
        cp = self._checkpoint_dir / "checkpoint.json"
        if cp.exists():
            try:
                msgs = json.loads(cp.read_text(encoding="utf-8"))
                if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
                    msgs[-1] = {"role": "assistant", "content": "[会话已恢复，工具结果已清理]"}
                self._messages = msgs
            except Exception:
                pass

    def save_checkpoint(self) -> None:
        if not self._checkpoint_dir:
            return
        # CheckpointManager 格式（新）
        if self._checkpoint_mgr and self._session_id:
            try:
                from core.context_manager import ContextManager
                from core.models.session import SessionMetadata
                state = ContextManager.messages_to_state(
                    self._messages,
                    session_id=self._session_id,
                )
                self._checkpoint_mgr.save(state)
                return
            except Exception:
                pass

        # 回退旧格式 checkpoint.json
        try:
            trimmed = []
            for m in self._messages:
                role = m.get("role")
                if role == "tool":
                    continue
                if role == "assistant" and m.get("tool_calls"):
                    continue
                entry = dict(m)
                entry.pop("reasoning_content", None)
                trimmed.append(entry)
            cp = self._checkpoint_dir / "checkpoint.json"
            cp.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ====== 发送消息 ======

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        """发送用户消息 → 上下文组装 → 调度 → 自动压缩 → 检查点。"""
        if not self._dispatcher:
            yield {"type": "error", "message": "未初始化"}
            return

        self._messages.append({"role": "user", "content": user_input})
        t0 = time.monotonic()
        sid = self._checkpoint_dir.stem if self._checkpoint_dir else f"chat-{int(t0)}"

        # 通过 ContextManager 组装上下文（MEMORY.md 常驻动态区）
        if self._cm:
            from core.context_manager import ContextManager
            ctx = ContextManager.assemble_from_messages(
                self._messages, self._cm,
            )
            msgs_for_llm = ContextManager.blocks_to_messages(ctx)
        else:
            msgs_for_llm = self._messages

        try:
            async for chunk in self._dispatcher.dispatch_stream(
                messages=msgs_for_llm,
                session_id=sid,
                max_rounds=200,
            ):
                yield chunk
        except Exception as e:
            yield {"type": "error", "message": str(e)}

        # 自动压缩 + 检查点（压缩后标记 history_trimmed，下次组装注入 MEMORY.md）
        est = ContextCompressor.estimate_tokens(self._messages)
        pct = est / self._context_window
        cache_dir = self._checkpoint_dir / "tool_cache" if self._checkpoint_dir else None
        msgs, reason = ContextCompressor.auto_compact(
            self._messages,
            self._context_window,
            llm=self._llm,
            cache_dir=cache_dir,
            **self._compress_cfg,
        )
        if reason:
            self._messages = msgs
            yield {"type": "compact", "reason": reason}

        # 上下文利用率遥测
        tel = getattr(self._dispatcher, "_telemetry", None) if self._dispatcher else None
        if tel and tel.agent:
            tel.agent.record_context(est, pct, compression_triggered=bool(reason))

        # 同步最终回答：收集本轮所有 assistant 文本（含 tool_calls 伴生）。
        # DeepSeek 等模型习惯在 tool_call 前输出回答文本，最后一轮可能只有空 stop。
        # 只取「无 tool_calls」的那条会丢失真正的回答内容。
        if self._cm:
            found_user = False
            assistant_parts: list[str] = []
            for m in msgs_for_llm:
                if m.get("role") == "user" and m.get("content") == user_input:
                    found_user = True
                    continue
                if found_user and m.get("role") == "assistant":
                    c = (m.get("content") or "").strip()
                    if c:
                        assistant_parts.append(c)
            self._messages.append({
                "role": "assistant",
                "content": "\n\n".join(assistant_parts),
            })
        else:
            self._messages = [m for m in self._messages if m.get("role") != "tool"]
            for m in self._messages:
                m.pop("tool_calls", None)
                m.pop("reasoning_content", None)

        self.save_checkpoint()

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
        return 1_048_576  # 未知模型保守 1M
