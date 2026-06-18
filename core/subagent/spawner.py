"""
core/subagent/spawner.py
SubAgentSpawner — 子Agent 孵化器（参考 Claude Code sub-agent 设计）。

用法:
    spawner = SubAgentSpawner(llm_invoker, parent_tool_executor)
    result = await spawner.spawn("explore", "梳理项目架构", session_id)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.context_manager import ContextManager
from core.dispatcher import Dispatcher
from core.subagent.definitions import SUBAGENT_DEFINITIONS, SubAgentDef
from core.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # 子Agent 5 分钟硬超时


@dataclass
class SubAgentResult:
    """子Agent 执行结果"""
    agent_type: str
    task: str
    text: str = ""                # 子Agent 最终文本
    tool_calls_made: int = 0      # 工具调用次数
    tokens_used: int = 0          # Token 消耗
    duration_ms: int = 0
    status: str = "success"       # success | timeout | error


class SubAgentSpawner:
    """子Agent 孵化器"""

    def __init__(
        self,
        llm_invoker,
        parent_tool_executor: Optional[ToolExecutor] = None,
        hook_orchestrator: Optional[Any] = None,
    ):
        self._llm = llm_invoker
        self._parent_tools = parent_tool_executor
        self._hooks = hook_orchestrator

    async def spawn(
        self,
        agent_type: str,
        task: str,
        session_id: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> SubAgentResult:
        """创建并执行子Agent。返回 SubAgentResult。"""
        t0 = time.monotonic()

        # 1. 查找定义
        definition = SUBAGENT_DEFINITIONS.get(agent_type)
        if not definition:
            return SubAgentResult(
                agent_type=agent_type, task=task,
                text=f"[错误] 未知子Agent类型: {agent_type}。可用: {list(SUBAGENT_DEFINITIONS.keys())}",
                status="error",
            )

        logger.info("子Agent 启动: %s / %s", agent_type, task[:80])

        # 前置钩子
        if self._hooks:
            try:
                await self._hooks.run(
                    session_id,
                    "pre_subagent_spawn",
                    agent_type=agent_type, task=task,
                )
            except Exception:
                pass

        try:
            # 2. 构建隔离上下文
            ctx_mgr = self._build_context(definition)

            # 3. 构建有限工具
            tools = self._build_tools(definition)

            # 4. 调度执行
            dispatcher = Dispatcher(self._llm, tools)

            result = await asyncio.wait_for(
                dispatcher.dispatch(
                    messages=[
                        {"role": "system", "content": definition.system_prompt},
                        {"role": "user", "content": task},
                    ],
                    session_id=f"{session_id}-sub-{agent_type}",
                    max_rounds=definition.max_iterations,
                ),
                timeout=timeout,
            )

            elapsed = int((time.monotonic() - t0) * 1000)
            sub_result = SubAgentResult(
                agent_type=agent_type,
                task=task,
                text=result.text,
                tokens_used=result.usage.get("total", 0),
                duration_ms=elapsed,
                status="success" if not result.error else "error",
            )

            # 后置钩子
            if self._hooks:
                try:
                    await self._hooks.run(
                        session_id,
                        "post_subagent_spawn",
                        agent_type=agent_type, result=sub_result,
                    )
                except Exception:
                    pass

            return sub_result

        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.warning("子Agent 超时: %s (%.1fs)", agent_type, elapsed / 1000)
            return SubAgentResult(
                agent_type=agent_type, task=task,
                text=f"[超时] 子Agent {agent_type} 执行超过 {timeout}s",
                status="timeout", duration_ms=elapsed,
            )
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.error("子Agent 错误: %s - %s", agent_type, e)
            return SubAgentResult(
                agent_type=agent_type, task=task,
                text=f"[错误] {type(e).__name__}: {e}",
                status="error", duration_ms=elapsed,
            )

    # ====== 内部 ======

    def _build_context(self, definition: SubAgentDef) -> ContextManager:
        """构建子Agent 专用 ContextManager"""
        mgr = ContextManager()
        if definition.skip_claude_md:
            # 不加载 CHACHA.md（同 Claude Code Explore）
            pass
        return mgr

    def _build_tools(self, definition: SubAgentDef) -> ToolExecutor:
        """根据 whitelist 过滤父工具列表"""
        if not self._parent_tools:
            return ToolExecutor(tools=[])

        allowed = set(definition.tools_whitelist)
        filtered = []
        for t in self._parent_tools._tool_objects:
            if hasattr(t, 'name') and t.name in allowed:
                filtered.append(t)

        return ToolExecutor(tools=filtered)
