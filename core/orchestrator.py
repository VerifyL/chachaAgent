"""
core/orchestrator.py
Orchestrator — 主控制器：Think-Act-Observe 循环，协调所有子系统。

v2.0 新增:
  - 每轮 assistant 最终回答后异步保存 session/{date}.md 记忆
  - 会话结束时清理 tool_cache 目录
  - DreamPipeline 会话计数 + 条件触发

用法:
    orch = Orchestrator(context_mgr, llm_invoker, tool_executor, memory_manager=mgr, dream_pipeline=dream)
    resp = await orch.run("帮我读一下 main.py", session_id="s1")
    print(resp.text)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent, ObservationEvent,
    ToolCallEvent,
)

logger = logging.getLogger(__name__)


@dataclass
class OrchResponse:
    """Orchestrator 运行结果"""
    text: str = ""
    session_id: str = ""
    iterations: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    duration_ms: int = 0
    error: Optional[str] = None


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
        max_iterations: int = 50,
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
        self._max_iterations = max_iterations
        self._engine: Optional[Any] = None  # ChatEngine（由 set_engine 设置）

    def set_engine(self, engine) -> None:
        """注入 ChatEngine 实例（用于 run_stream）。"""
        self._engine = engine

    # ====== 主入口 ======

    async def run(
        self,
        user_input: str,
        session_id: str = "",
        project_id: str = "default",
        tools: Optional[List[Dict]] = None,
    ) -> OrchResponse:
        """运行 Think-Act-Observe 主循环。"""
        t0 = time.monotonic()
        sid = session_id or f"session-{int(t0)}"

        if not self._llm and not self._dispatcher:
            return OrchResponse(
                error="No LLM invoker or dispatcher configured",
                session_id=sid,
            )

        # 初始化会话状态
        meta = SessionMetadata(session_id=sid, project_id=project_id)
        state = ConversationState(metadata=meta)
        state.add_event(MessageEvent(source="user", role="user", content=user_input))

        # 会话开始事件
        if self._gateway:
            from protocol.rpc_schema import SessionLifecycleEvent
            await self._gateway.publish(
                SessionLifecycleEvent(params={
                    "event": "started",
                    "session_id": sid,
                }),
                session_id=sid,
            )

        # 主循环
        iterations = 0
        total_tokens = 0
        final_text = ""

        while iterations < self._max_iterations:
            iterations += 1

            # 1. 上下文组装
            messages = await self._get_messages(state)

            # 2. LLM 调用
            if self._dispatcher:
                resp = await self._dispatcher.dispatch(messages, sid)
            else:
                resp = await self._llm.invoke(messages, tools=tools, session_id=sid)

            if resp.usage:
                total_tokens += resp.usage.get("total", 0)

            # 3. 错误处理
            if resp.error:
                if any(kw in resp.error.lower() for kw in ("authentication", "circuit")):
                    return OrchResponse(error=resp.error, session_id=sid)
                state.add_event(MessageEvent(source="system", role="system", content=f"[Error] {resp.error}"))
                continue

            if resp.text:
                final_text = resp.text
                state.add_event(MessageEvent(source="agent", role="assistant", content=resp.text))

            # 4. 工具调用处理
            if resp.tool_calls:
                for tc in resp.tool_calls:
                    state.add_event(ToolCallEvent(
                        source="tool",
                        tool_name=tc.name,
                        tool_use_id=tc.id,
                        arguments=tc.arguments,
                    ))

                results = await self._tools.execute_batch(
                    [
                        {"tool_name": tc.name, "arguments": tc.arguments, "tool_use_id": tc.id}
                        for tc in resp.tool_calls
                    ],
                    sid,
                )

                for r in results:
                    state.add_event(ObservationEvent(
                        source="tool",
                        tool_use_id=r.tool_use_id,
                        content=r.output,
                        status=r.status,
                        error=r.error,
                        truncated=r.truncated,
                        duration_ms=r.duration_ms,
                    ))
            else:
                # 5. 无工具调用 → 最终回答 → 异步保存记忆
                if final_text and self._memory:
                    self._save_round_memory(user_input, final_text, project_id)

                break

        # 强制终止警告
        if iterations >= self._max_iterations:
            logger.warning("达到最大迭代次数 (%d)，强制终止", self._max_iterations)

        duration = int((time.monotonic() - t0) * 1000)

        # 会话结束事件
        if self._gateway:
            from protocol.rpc_schema import SessionLifecycleEvent
            await self._gateway.publish(
                SessionLifecycleEvent(params={
                    "event": "ended",
                    "session_id": sid,
                    "total_tokens": total_tokens,
                    "total_cost_usd": 0.0,
                }),
                session_id=sid,
            )

        # 遥测
        if self._telemetry:
            self._telemetry.agent.record_session(
                session_id=sid,
                total_tokens=total_tokens,
                total_cost=0.0,
                duration_ms=duration,
            )

        # 会话结束清理
        await self._end_session_cleanup(sid)

        return OrchResponse(
            text=final_text,
            session_id=sid,
            iterations=iterations,
            total_tokens=total_tokens,
            duration_ms=duration,
        )

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

        # ── 1. ConversationState ──
        state = ConversationState(
            metadata=SessionMetadata(
                session_id=session_id,
                project_id=project_id,
            ),
        )
        state.add_event(MessageEvent(source="user", role="user", content=user_input))
        self._engine._messages.append({"role": "user", "content": user_input})

        # ── 2. Hook: PRE_CONTEXT_ASSEMBLY ──
        additional_blocks: list = []
        if self._hooks:
            try:
                from core.models.hook import HookPoint
                from core.models.context import ContextBlock, BlockSource
                hook_result = await self._hooks.run(
                    session_id=session_id,
                    hook_point=HookPoint.PRE_CONTEXT_ASSEMBLY,
                )
                if hook_result and hook_result.additional_context:
                    additional_blocks.append(ContextBlock(
                        source=BlockSource.ADDITIONAL_CONTEXT,
                        role="system",
                        content=hook_result.additional_context,
                        zone="dynamic",
                        priority=8,
                        importance=0.55,
                    ))
            except Exception:
                pass

        # ── 3. Policy 检查 ──
        if self._policy:
            try:
                allowed = await self._policy.check(user_input)
                if not allowed:
                    yield {"type": "error", "message": "请求被策略拦截"}
                    return
            except Exception:
                pass

        # ── 4. Gateway: session_started ──
        if self._gateway:
            try:
                from protocol.rpc_schema import SessionLifecycleEvent
                await self._gateway.publish(
                    SessionLifecycleEvent(params={
                        "event": "started",
                        "session_id": session_id,
                    }),
                    session_id=session_id,
                )
            except Exception:
                pass

        # ── 5. 上下文组装 ──
        from core.context_manager import ContextManager
        if self._context:
            ctx = ContextManager.assemble_from_messages(
                self._engine._messages, self._context,
                additional_contexts=additional_blocks or None,
            )
            msgs_for_llm = ContextManager.blocks_to_messages(ctx)
        else:
            msgs_for_llm = list(self._engine._messages)

        # ── 6. Dispatcher 调度（直接调用，不经过 ChatEngine） ──
        dispatcher = self._dispatcher or getattr(self._engine, '_dispatcher', None)
        if not dispatcher:
            yield {"type": "error", "message": "No dispatcher configured"}
            return

        response_parts: list[str] = []
        try:
            async for chunk in dispatcher.dispatch_stream(
                messages=msgs_for_llm,
                session_id=session_id,
                max_rounds=200,
            ):
                # ConversationState 事件跟踪
                if chunk.get("type") == "tool_call_start":
                    state.add_event(ToolCallEvent(
                        source="tool",
                        tool_name=chunk.get("tool_name", "?"),
                        tool_use_id=chunk.get("tool_use_id", ""),
                        arguments=chunk.get("arguments", {}),
                    ))
                elif chunk.get("type") == "tool_exec_end":
                    state.add_event(ObservationEvent(
                        source="tool",
                        tool_use_id=chunk.get("tool_use_id", ""),
                        content=chunk.get("content", ""),
                        status=chunk.get("status", "success"),
                        error=chunk.get("error"),
                        truncated=chunk.get("truncated", False),
                        duration_ms=chunk.get("duration_ms", 0),
                    ))
                elif chunk.get("type") == "text":
                    response_parts.append(chunk.get("content", ""))

                yield chunk
        except GeneratorExit:
            return
        except Exception as e:
            yield {"type": "error", "message": str(e)}

        final_text = "".join(response_parts)

        # ── 7. 自动压缩 ──
        from core.context.context_compressor import ContextCompressor
        est = ContextCompressor.estimate_tokens(self._engine._messages)
        pct = est / self._engine._context_window if self._engine._context_window else 0
        cache_dir = (self._engine._checkpoint_dir / "tool_cache"
                      if self._engine._checkpoint_dir else None)
        msgs, reason = ContextCompressor.auto_compact(
            self._engine._messages,
            self._engine._context_window,
            llm=getattr(self._engine, '_llm', None),
            cache_dir=cache_dir,
            **getattr(self._engine, '_compress_cfg', {}),
        )
        if reason:
            self._engine._messages = msgs
            yield {"type": "compact", "reason": reason}

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
            self._engine._messages.append({
                "role": "assistant",
                "content": "\n\n".join(assistant_parts),
            })
        else:
            self._engine._messages = [
                m for m in self._engine._messages if m.get("role") != "tool"
            ]
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
                    SessionLifecycleEvent(params={
                        "event": "ended",
                        "session_id": session_id,
                    }),
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
        """会话结束时清理 tool_cache 目录 + 记录 DreamPipeline。"""
        # 清理 tool_cache
        if self._memory:
            try:
                self._memory.cleanup_tool_cache()
            except Exception as e:
                logger.warning("清理 tool_cache 失败: %s", e)

        # DreamPipeline 计数 + 异步触发
        if self._dream:
            self._dream.record_session()
            if self._dream.should_run() and self._memory:
                logger.info("触发 DreamPipeline 整合...")
                try:
                    asyncio.create_task(self._dream.run(self._memory))
                except Exception as e:
                    logger.warning("DreamPipeline 启动失败: %s", e)

    # ====== 内部 ======

    async def _get_messages(self, state: ConversationState) -> List[Dict[str, Any]]:
        """组装上下文消息（含 PRE_CONTEXT_ASSEMBLY 钩子注入）。"""
        # 钩子注入的额外上下文（Git 感知等可插拔模块）
        additional_blocks: list = []
        if self._hooks:
            try:
                from core.models.hook import HookPoint
                from core.models.context import ContextBlock, BlockSource
                hook_result = await self._hooks.run(
                    session_id=state.metadata.session_id,
                    hook_point=HookPoint.PRE_CONTEXT_ASSEMBLY,
                )
                if hook_result and hook_result.additional_context:
                    additional_blocks.append(ContextBlock(
                        source=BlockSource.ADDITIONAL_CONTEXT,
                        role="system",
                        content=hook_result.additional_context,
                        zone="dynamic",
                        priority=8,
                        importance=0.55,
                    ))
            except Exception:
                pass

        if self._context:
            ctx = self._context.assemble(
                state,
                additional_contexts=additional_blocks or None,
            )
            return ctx.get_messages()
        return state.get_messages_for_llm()
