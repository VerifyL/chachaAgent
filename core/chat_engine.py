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
    ):
        self._system_prompt = system_prompt
        self._context_window = context_window
        self._compress_cfg = compress_cfg or {}
        self._checkpoint_dir = checkpoint_dir
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
        if not self._llm:
            return
        executor = ToolExecutor(tools=tools or [])
        self._dispatcher = Dispatcher(llm_invoker=self._llm, tool_executor=executor)

    # ====== 检查点 ======

    def set_checkpoint_dir(self, path: Path) -> None:
        self._checkpoint_dir = path
        self._try_restore()

    def _try_restore(self) -> None:
        if not self._checkpoint_dir:
            return
        cp = self._checkpoint_dir / "checkpoint.json"
        if not cp.exists():
            return
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
        try:
            # 只保留 system / user / 最终 assistant（跳中间工具调用+推理过程）
            trimmed = []
            for m in self._messages:
                role = m.get("role")
                if role == "tool":
                    continue
                if role == "assistant" and m.get("tool_calls"):
                    continue        # 跳过含工具调用的中间 assistant
                entry = dict(m)
                entry.pop("reasoning_content", None)  # 删推理过程
                trimmed.append(entry)
            cp = self._checkpoint_dir / "checkpoint.json"
            cp.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ====== 发送消息 ======

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        """发送用户消息 → 调度 → 自动压缩 → 检查点。"""
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
                max_rounds=10,
            ):
                yield chunk
        except Exception as e:
            yield {"type": "error", "message": str(e)}

        # 自动压缩 + 检查点
        est = ContextCompressor.estimate_tokens(self._messages)
        pct = est / self._context_window
        msgs, reason = ContextCompressor.auto_compact(
            self._messages,
            self._context_window,
            llm=self._llm,
            **self._compress_cfg,
        )
        if reason:
            self._messages = msgs
            yield {"type": "compact", "reason": reason}

        # 上下文利用率遥测
        tel = getattr(self._dispatcher, "_telemetry", None) if self._dispatcher else None
        if tel and tel.agent:
            tel.agent.record_context(est, pct, compression_triggered=bool(reason))

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
