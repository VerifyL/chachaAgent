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
            messages = self._get_messages(state)

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

    # ====== 会话记忆 & 清理 ======

    def _save_round_memory(self, user_input: str, assistant_text: str) -> None:
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

    def _get_messages(self, state: ConversationState) -> List[Dict[str, Any]]:
        if self._context:
            ctx = self._context.assemble(state)
            return ctx.get_messages()
        return state.get_messages_for_llm()
