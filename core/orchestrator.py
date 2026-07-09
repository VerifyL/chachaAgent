"""
core/orchestrator.py
Orchestrator — 主控制器：Think-Act-Observe 循环，协调所有子系统。

v2.0 新增:
  - 每轮 assistant 最终回答后异步保存 session/{date}.md 记忆
  - 会话结束时记录 DreamPipeline 触发
  - DreamPipeline 会话计数 + 条件触发

用法:
    orch = Orchestrator(context_mgr, llm_invoker, tool_executor, memory_manager=mgr, dream_pipeline=dream)
    orch.set_engine(engine)
    async for chunk in orch.run_stream("帮我读一下 main.py", session_id="s1"):
        ...
"""

import asyncio
import logging
from typing import Any, Optional

from core.models.stream_event import (
    CompactEvent,
    ErrorEvent,
    TextEvent,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    """Think-Act-Observe 主循环控制器（v2.0）。"""

    def __init__(
        self,
        context_manager: Optional[Any] = None,
        llm_invoker: Optional[Any] = None,
        tool_executor: Optional[Any] = None,
        dispatcher: Optional[Any] = None,
        gateway: Optional[Any] = None,
        telemetry: Optional[Any] = None,
        hook_orchestrator: Optional[Any] = None,
        policy_engine: Optional[Any] = None,
        memory_manager: Optional[Any] = None,
        dream_pipeline: Optional[Any] = None,
    ):
        self._context = context_manager
        self._llm = llm_invoker
        self._tools = tool_executor
        self._dispatcher = dispatcher
        self._gateway = gateway
        self._memory = memory_manager
        self._dream = dream_pipeline
        self._telemetry = telemetry
        self._hooks = hook_orchestrator
        self._policy = policy_engine
        self._engine: Optional[Any] = None  # ChatEngine（由 set_engine 设置）

    def set_engine(self, engine) -> None:
        """注入 ChatEngine 实例（用于 run_stream）。"""
        self._engine = engine

    # ====== 流式入口（委托 ChatEngine，为 Hook/Policy 预留） ======

    async def run_stream(
        self,
        user_input: str,
        session_id: str = "",
        project_id: str = "default",
    ):
        """流式执行：Hook → Policy → Gateway → Context → Dispatcher → 压缩 → 记忆。

        v2.1: 不再委托 ChatEngine，直接调度 Dispatcher，注入 Hook/Policy/Gateway/Dream。
        """
        if not self._engine:
            raise RuntimeError("run_stream 需要 ChatEngine，请调用 set_engine()")

        # ── 1. 用户输入注入 ──
        self._engine._messages.append({"role": "user", "content": user_input})

        # ── 2. Hook: PRE_CONTEXT_ASSEMBLY ──
        additional_blocks: list = []
        if self._hooks:
            try:
                from core.models.context import BlockSource, ContextBlock
                from core.models.hook import HookPoint

                hook_result = await self._hooks.run(
                    session_id=session_id,
                    hook_point=HookPoint.PRE_CONTEXT_ASSEMBLY,
                )
                if hook_result and hook_result.additional_context:
                    additional_blocks.append(
                        ContextBlock(
                            source=BlockSource.ADDITIONAL_CONTEXT,
                            role="system",
                            content=hook_result.additional_context,
                            zone="dynamic",
                            priority=8,
                            importance=0.55,
                        )
                    )
            except Exception:
                pass

        # ── 3. Policy 检查 ──
        # PolicyEngine 关注工具级风险评估（evaluate_tool），输入级检查当前为空白占位。
        # 若后续需要输入策略（黑名单词、注入检测），在此扩展。

        # ── 4. Gateway: session_started ──
        if self._gateway:
            try:
                from protocol.rpc_schema import SessionLifecycleEvent

                await self._gateway.publish(
                    SessionLifecycleEvent(
                        params={
                            "event": "started",
                            "session_id": session_id,
                        }
                    ),
                    session_id=session_id,
                )
            except Exception:
                pass

        # ── 5. 上下文组装 ──
        from core.context_manager import ContextManager

        if self._context:
            ctx = ContextManager.assemble_from_messages_direct(
                self._engine._messages,
                self._context,
                additional_contexts=additional_blocks or None,
            )
            msgs_for_llm = ContextManager.blocks_to_messages(ctx)
        else:
            msgs_for_llm = list(self._engine._messages)

        # ── 6. Dispatcher 调度（直接调用，不经过 ChatEngine） ──
        dispatcher = self._dispatcher or getattr(self._engine, "_dispatcher", None)
        if not dispatcher:
            yield ErrorEvent(message="No dispatcher configured")
            return

        response_parts: list[str] = []
        try:
            async for chunk in dispatcher.dispatch_stream(
                messages=msgs_for_llm,
                session_id=session_id,
                max_rounds=200,
            ):
                if isinstance(chunk, TextEvent):
                    response_parts.append(chunk.content)

                yield chunk
        except GeneratorExit:
            return
        except asyncio.CancelledError:
            # 用户取消：不保存 checkpoint、不压缩、不记录记忆
            raise
        except Exception as e:
            yield ErrorEvent(message=str(e))

        final_text = "".join(response_parts)

        # ── 7. 自动压缩 ──
        from core.context.context_compressor import ContextCompressor

        est = ContextCompressor.estimate_tokens(self._engine._messages)
        pct = est / self._engine._context_window if self._engine._context_window else 0
        msgs, reason = await ContextCompressor.auto_compact(
            self._engine._messages,
            self._engine._context_window,
            llm=getattr(self._engine, "_llm", None),
        )
        if reason:
            self._engine._messages = msgs
            yield CompactEvent(reason=reason)

        # ── 8. 上下文利用率遥测 ──
        try:
            tel = getattr(dispatcher, "_telemetry", None) if dispatcher else None
            if tel and tel.agent:
                tel.agent.record_context(est, pct, compression_triggered=bool(reason))
        except Exception:
            pass

        # ── 9. 最终回答提取（DeepSeek think 兼容） ──
        if self._context:
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
            self._engine._messages.append(
                {
                    "role": "assistant",
                    "content": "\n\n".join(assistant_parts),
                }
            )
        else:
            self._engine._messages = [m for m in self._engine._messages if m.get("role") != "tool"]
            for m in self._engine._messages:
                m.pop("tool_calls", None)
                m.pop("reasoning_content", None)

        # ── 10. 检查点保存 ──
        self._engine.save_checkpoint()

        # ── 11. 会话记忆 ──
        self._save_round_memory(user_input, final_text, project_id)

        # ── 12. Gateway: session_ended ──
        if self._gateway:
            try:
                from protocol.rpc_schema import SessionLifecycleEvent

                await self._gateway.publish(
                    SessionLifecycleEvent(
                        params={
                            "event": "ended",
                            "session_id": session_id,
                        }
                    ),
                    session_id=session_id,
                )
            except Exception:
                pass

        # ── 13. 清理 + Dream 触发 ──
        await self._end_session_cleanup(session_id)

    # ====== 会话记忆 & 清理 ======

    def _save_round_memory(self, user_input: str, assistant_text: str, project_id: str = "") -> None:
        """异步保存本轮对话到 memory/session/{date}.md（只含 user + assistant，无工具调用）。"""
        try:
            if self._memory:
                entry = f"Q: {user_input.strip()}\nA: {assistant_text.strip()}"
                self._memory.remember(entry)
                logger.debug("会话记忆已保存")
        except Exception as e:
            logger.warning("保存会话记忆失败: %s", e)

    async def _end_session_cleanup(self, session_id: str) -> None:
        """会话结束时记录 DreamPipeline。"""
        # DreamPipeline 计数 + 异步触发
        if self._dream:
            self._dream.record_session()
            if self._dream.should_run() and self._memory:
                logger.info("触发 DreamPipeline 整合...")
                try:
                    asyncio.create_task(self._dream.run(self._memory))
                except Exception as e:
                    logger.warning("DreamPipeline 启动失败: %s", e)
