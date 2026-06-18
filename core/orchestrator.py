"""
core/orchestrator.py
Orchestrator — 主控制器：Think-Act-Observe 循环，协调所有子系统。

设计理念（异步生成器 +事件驱动）：
1. 薄胶水层：ContextManager/LLMInvoker/ToolExecutor 已独立实现，本模块只做编排
2. 异步生成器：run() 为主入口，内部驱动 agent loop
3. 事件发布：通过 Gateway 发布 SessionLifecycleEvent/TokenChunkEvent
4. 终止条件：finish_reason=stop 或无 tool_calls → 结束；max_iterations 耗尽 → 强制终止
5. 错误即观察：LLM 错误注入 state 作为下一轮输入

用法:
    orch = Orchestrator(context_mgr, llm_invoker, tool_executor, gateway, telemetry)
    resp = await orch.run("帮我读一下 main.py", session_id="s1")
    print(resp.text)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.models.session import (
    ConversationState, SessionMetadata, MessageEvent, ObservationEvent,
)

logger = logging.getLogger(__name__)


# ========================= 响应类型 =========================

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


# ========================= 主控制器 =========================

class Orchestrator:
    """
    Think-Act-Observe 主循环控制器。

    所有子系统均可为 None（渐进构建/测试友好）。
    """

    def __init__(
        self,
        context_manager: Optional[Any] = None,   # ContextManager
        llm_invoker: Optional[Any] = None,        # LLMInvoker
        tool_executor: Optional[Any] = None,      # ToolExecutor
        dispatcher: Optional[Any] = None,         # Dispatcher（取代 llm_invoker+tool_executor 的工具循环）
        gateway: Optional[Any] = None,            # ChaChaAsyncGateway
        telemetry: Optional[Any] = None,          # Telemetry
        hook_orchestrator: Optional[Any] = None,  # HookOrchestrator
        policy_engine: Optional[Any] = None,       # PolicyEngine
        memory_manager: Optional[Any] = None,      # MemoryManager
        dream_pipeline: Optional[Any] = None,      # DreamPipeline（会话结束后异步整合）
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
        project_id: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        state: Optional[ConversationState] = None,
    ) -> OrchResponse:
        """执行 Think-Act-Observe 循环。

        若无传入 state，自动创建新会话。
        """
        t0 = time.monotonic()

        # 0. 初始化会话状态
        if state is None:
            meta = SessionMetadata(
                project_id=project_id,
                session_id=session_id,
            )
            state = ConversationState(metadata=meta)

        # 用户消息
        state.add_event(MessageEvent(source="user", role="user", content=user_input))

        # 会话开始事件
        if self._gateway:
            from protocol.rpc_schema import SessionLifecycleEvent
            await self._gateway.publish(
                SessionLifecycleEvent(params={
                    "event": "started",
                    "session_id": state.metadata.session_id,
                    "project_id": project_id,
                }),
                session_id=state.metadata.session_id,
            )

        final_text = ""
        total_tokens = 0
        iterations = 0

        # ====== 主循环 ======
        while iterations < self._max_iterations:
            iterations += 1
            logger.debug("Orchestrator iteration %d/%d", iterations, self._max_iterations)

            # 1. 上下文组装
            messages = self._get_messages(state)

            # 2. LLM + 工具调度
            if self._dispatcher:
                # Dispatcher 内部处理工具循环
                resp = await self._dispatcher.dispatch(
                    messages, state.metadata.session_id,
                )
            else:
                if self._llm is None:
                    return OrchResponse(
                        error="No LLM invoker configured",
                        session_id=state.metadata.session_id,
                    )
                resp = await self._llm.invoke(messages, tools, state.metadata.session_id)

            if resp.error:
                logger.error("LLM error: %s", resp.error)
                # 错误注入状态
                state.add_event(MessageEvent(
                    source="system", role="system",
                    content=f"[Error] {resp.error}",
                ))
                # 如果是不可恢复的错误（认证/熔断），终止
                if "authentication" in resp.error.lower() or "circuit" in resp.error.lower():
                    return OrchResponse(
                        text=final_text, error=resp.error,
                        session_id=state.metadata.session_id,
                        iterations=iterations, duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                continue

            total_tokens += resp.usage.get("total", 0)
            final_text = resp.text

            # 助手回复
            state.add_event(MessageEvent(source="agent", role="assistant", content=resp.text))

            # 3. 工具调用
            if resp.tool_calls and self._tools:
                results = await self._tools.execute_batch(
                    [
                        {
                            "tool_name": tc.name,
                            "arguments": tc.arguments,
                            "tool_use_id": tc.id,
                        }
                        for tc in resp.tool_calls
                    ],
                    state.metadata.session_id,
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

            # 4. 终止条件
            if not resp.tool_calls or resp.finish_reason == "stop":
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
                    "session_id": state.metadata.session_id,
                    "total_tokens": total_tokens,
                    "total_cost_usd": 0.0,
                }),
                session_id=state.metadata.session_id,
            )

        # 遥测
        if self._telemetry:
            self._telemetry.agent.record_session(
                session_id=state.metadata.session_id,
                total_tokens=total_tokens,
                total_cost=0.0,
                duration_ms=duration,
            )

        # DreamPipeline：会话结束后异步整合记忆
        if self._dream and self._memory and self._dream.should_run():
            import asyncio
            asyncio.create_task(self._dream.run(self._memory))

        return OrchResponse(
            text=final_text,
            session_id=state.metadata.session_id,
            iterations=iterations,
            total_tokens=total_tokens,
            duration_ms=duration,
        )

    # ====== 内部 ======

    def _get_messages(self, state: ConversationState) -> List[Dict[str, Any]]:
        """从会话状态获取 LLM 消息列表。"""
        if self._context:
            ctx = self._context.assemble(state)
            return ctx.get_messages()
        return state.get_messages_for_llm()
