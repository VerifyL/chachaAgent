"""
core/subagent/spawner.py
SubAgentSpawner — 子Agent 孵化器（参考 sub-agent 设计）。

用法:
    spawner = SubAgentSpawner(llm_invoker, parent_tool_executor)
    result = await spawner.spawn("explore", "梳理项目架构", session_id)
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel

from core.dispatcher import Dispatcher
from core.models.hook import HookPoint
from core.subagent.definitions import SUBAGENT_DEFINITIONS, SubAgentDef
from core.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # 子Agent 5 分钟硬超时


class SubAgentResult(BaseModel):
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
        project_root: Optional[str] = None,
        telemetry: Optional[Any] = None,
    ):
        self._llm = llm_invoker
        self._parent_tools = parent_tool_executor
        self._hooks = hook_orchestrator
        self._project_root = project_root
        self._telemetry = telemetry
        self._parent_budget = None

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
                    session_id=session_id,
                    hook_point=HookPoint.PRE_SUBAGENT_SPAWN,
                    metadata={"agent_type": agent_type, "task": task},
                )
            except Exception:
                pass

        try:
            # 2. 构建隔离上下文
            ctx_data = self._build_context(definition, session_id)

            # 3. 组装子Agent system prompt（角色 + 项目记忆 + 宪法）
            system_content = definition.system_prompt
            if ctx_data.get("permanent_memory"):
                system_content += f"\n\n--- 项目永久记忆 ---\n{ctx_data['permanent_memory']}"
            if ctx_data.get("memory_index"):
                system_content += f"\n\n--- 记忆索引 ---\n{ctx_data['memory_index']}"
            if not definition.skip_claude_md:
                chacha_path = Path(self._project_root) / "CHACHA.md" if self._project_root else None
                if chacha_path and chacha_path.exists():
                    system_content += f"\n\n--- 项目宪法 (CHACHA.md) ---\n{chacha_path.read_text(encoding='utf-8')}"

            # 4. 构建有限工具
            tools = self._build_tools(definition)

            # 5. 调度执行
            dispatcher = Dispatcher(
                self._llm, tools,
                memory_manager=None,  # 子Agent 短对话，缓存收益不大
            )

            result = await asyncio.wait_for(
                dispatcher.dispatch(
                    messages=[
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": task},
                    ],
                    session_id=f"{session_id}-sub-{agent_type}",
                    max_rounds=definition.max_rounds,
                ),
                timeout=timeout,
            )

            # 将子 executor 的输出缓存合并到父 executor，确保父 agent 的 cache_read 能命中
            if self._parent_tools and hasattr(tools, '_output_cache'):
                self._parent_tools._output_cache.update(tools._output_cache)

            elapsed = int((time.monotonic() - t0) * 1000)
            sub_result = SubAgentResult(
                agent_type=agent_type,
                task=task,
                text=result.text,
                tokens_used=result.usage.get("total", 0),
                duration_ms=elapsed,
                status="success" if not result.error else "error",
                tool_calls_made=dispatcher.tool_calls_made,
            )

            # 后置钩子
            if self._hooks:
                try:
                    await self._hooks.run(
                        session_id=session_id,
                        hook_point=HookPoint.POST_SUBAGENT_SPAWN,
                        metadata={"agent_type": agent_type, "result": str(sub_result)},
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

    def _build_context(self, definition: SubAgentDef, session_id: str = "") -> Dict[str, str]:
        """构建子Agent 上下文数据。返回 {"memory_index": ..., "permanent_memory": ...}。"""
        result: Dict[str, str] = {"memory_index": "", "permanent_memory": ""}
        if self._parent_tools:
            try:
                from core.context.memory_manager import MemoryManager
                if self._project_root:
                    mm = MemoryManager(project_root=Path(self._project_root), session_id=session_id or None)
                    idx = mm.read_index()
                    if idx:
                        result["memory_index"] = idx
                    perm = mm.read_permanent_memory()
                    if perm:
                        result["permanent_memory"] = perm
            except Exception:
                pass
        return result

    def _build_tools(self, definition: SubAgentDef) -> ToolExecutor:
        """根据 whitelist 过滤父工具列表。

        注意：cache_read 工具持有 _executor 引用，不能直接共享父实例，
        否则子Agent内部截断缓存在子 executor 但 cache_read 仍指向父 executor。
        为 cache_read 创建独立实例并绑定子Agent自己的 executor。
        """
        if not self._parent_tools:
            return ToolExecutor(tools=[])

        allowed = set(definition.tools_whitelist)
        filtered = []
        for t in self._parent_tools.get_tools():
            if hasattr(t, 'name') and t.name in allowed:
                if t.name == 'cache_read':
                    # 创建独立实例，避免共享 _executor 引用
                    from capabilities.builtins.cache_read_tool import CacheReadTool
                    t = CacheReadTool()
                filtered.append(t)

        executor = ToolExecutor(tools=filtered, telemetry=self._telemetry)

        # 重新配置 cache_read 指向子Agent自己的 executor
        for t in filtered:
            if hasattr(t, 'name') and t.name == 'cache_read' and hasattr(t, 'configure'):
                t.configure(parent_tool_executor=executor)
                break

        return executor
