"""
core/hook_orchestrator.py
HookOrchestrator — 钩子责任链引擎。

设计理念（融合外部进程 + Harness Plugin Hook + 安全优先容错）：
1. 双模式 handler：Python callable（内置钩子）+ ShellCommand（用户自定义外部进程）
2. 结果累积：MODIFY 链式覆盖参数，additional_context 跨钩子拼接
3. PRE 钩子正序执行，POST 钩子倒序执行（中间件"洋葱"语义）
4. 安全钩子（可返回 BLOCK/MODIFY）超时/崩溃 → 默认拒绝，日志钩子（仅 CONTINUE）→ 容错继续
5. 每钩子独立超时，超时视为执行失败
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

from core.models.hook import (
    HookContext,
    HookMatcher,
    HookPoint,
    HookResult,
)

logger = logging.getLogger(__name__)

# ========================= 外部进程命令 =========================


@dataclass
class ShellCommand:
    """外部钩子：通过 stdin/stdout JSON 协议通信。

    command 在子进程中执行，stdin 接收 HookContext JSON，stdout 期望输出 HookResult JSON。
    """

    command: str  # 命令字符串（如 "python audit.py"）
    timeout: float = 10.0  # 子进程超时
    env: Optional[Dict[str, str]] = field(default=None)  # 额外环境变量


# ========================= 钩子注册 =========================

HookHandler = Union[
    Callable[[HookContext], HookResult],
    Callable[[HookContext], Coroutine[Any, Any, HookResult]],
    ShellCommand,
]


@dataclass
class RegisteredHook:
    """已注册的钩子"""

    name: str
    hook_point: HookPoint
    handler: HookHandler
    matcher: HookMatcher = field(default_factory=lambda: HookMatcher(type="always"))
    priority: int = 0  # 越大越先执行（PRE 正序，POST 倒序）
    timeout: float = 10.0  # 执行超时（秒）

    # 显式覆盖容错策略（None=自动推断）
    on_timeout_continue: Optional[bool] = None
    on_error_continue: Optional[bool] = None


# ========================= 钩子协调器 =========================


class HookOrchestrator:
    """
    钩子责任链引擎。

    用法:
        orchestrator = HookOrchestrator()
        orchestrator.register("audit", HookPoint.PRE_TOOL_EXECUTION, audit_handler)
        result = await orchestrator.run("s1", HookPoint.PRE_TOOL_EXECUTION,
                                         tool_call=tool_ctx)
        if result.is_blocked():
            return  # 操作被拦截

    TODO(阶段2.10): 与 rule_engine.py 集成 — YAML 声明的钩子规则自动注册到本引擎
    TODO(阶段5):    内置钩子实现（security_check / cost_check / compression_hook / path_sanitizer）
    """

    def __init__(self, telemetry: Optional[Any] = None):
        self._hooks: List[RegisteredHook] = []
        self._telemetry = telemetry

    # ====== 注册 ======

    def register(
        self,
        name: str,
        hook_point: HookPoint,
        handler: HookHandler,
        matcher: Optional[HookMatcher] = None,
        priority: int = 0,
        timeout: float = 10.0,
        on_timeout_continue: Optional[bool] = None,
        on_error_continue: Optional[bool] = None,
    ) -> None:
        """注册钩子。

        handler 可以是:
          - async def (HookContext) → HookResult（内置异步钩子）
          - ShellCommand(cmd, timeout)（外部进程钩子，风格）
        """
        hook = RegisteredHook(
            name=name,
            hook_point=hook_point,
            handler=handler,
            matcher=matcher or HookMatcher(type="always"),
            priority=priority,
            timeout=timeout,
            on_timeout_continue=on_timeout_continue,
            on_error_continue=on_error_continue,
        )
        self._hooks.append(hook)
        # 按 priority 降序排序（大的先执行），稳定排序保持注册顺序
        self._hooks.sort(key=lambda h: h.priority, reverse=True)
        logger.debug("已注册钩子: %s (point=%s, pri=%d)", name, hook_point, priority)

    def unregister(self, name: str) -> None:
        """注销钩子"""
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.name != name]
        if len(self._hooks) < before:
            logger.debug("已注销钩子: %s", name)

    # ====== 执行 ======

    async def run(
        self,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        hook_point: Optional[HookPoint] = None,
        tool_call: Optional[Any] = None,
        llm_request: Optional[Any] = None,
        error: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HookResult:
        """在指定挂载点执行所有匹配钩子的责任链。

        返回最终的 HookResult，其中:
          - modified_tool_args 是链式覆盖的最终结果
          - additional_context 是所有钩子的拼接
        """
        # 1. 筛选 + 排序
        candidates = self._select(hook_point, tool_call)
        if not candidates:
            return HookResult.continue_()

        # 2. 排序：PRE 正序（高 priority 先），POST 倒序（低 priority 先）
        is_post = hook_point and hook_point.value.startswith("post_")
        if is_post:
            candidates.reverse()  # 倒序：洋葱剥皮

        # 3. 构建上下文
        ctx = HookContext(
            hook_point=hook_point or HookPoint.PRE_TOOL_EXECUTION,
            session_id=session_id,
            project_id=project_id,
            tool_call=tool_call,
            llm_request=llm_request,
            error=error,
            metadata=metadata or {},
        )

        # 4. 链式执行
        accumulated_context: List[str] = []
        current_tool_args: Optional[Dict[str, Any]] = (
            dict(tool_call.arguments) if tool_call and hasattr(tool_call, "arguments") else None
        )

        for hook in candidates:
            # 构建带匹配器的上下文
            hook_ctx = ctx.model_copy(update={"matched_by": hook.matcher})

            # 注入当前累积的 modified_tool_args
            if current_tool_args is not None and hook_ctx.tool_call is not None:
                hook_ctx = hook_ctx.model_copy(
                    update={"tool_call": hook_ctx.tool_call.model_copy(update={"arguments": current_tool_args})}
                )

            try:
                result = await self._execute_hook(hook, hook_ctx)
            except asyncio.TimeoutError:
                logger.warning("钩子 %s 执行超时 (%.1fs)", hook.name, hook.timeout)
                tolerance = self._get_error_tolerance(hook)
                if tolerance:
                    result = HookResult.continue_(message=f"hook {hook.name} timed out")
                else:
                    return HookResult.block(message=f"安全钩子 {hook.name} 超时，默认拒绝")
            except Exception as e:
                logger.exception("钩子 %s 执行异常", hook.name)
                tolerance = self._get_error_tolerance(hook)
                if tolerance:
                    result = HookResult.continue_(message=f"hook {hook.name} failed: {e}")
                else:
                    return HookResult.block(message=f"安全钩子 {hook.name} 异常，默认拒绝: {e}")

            # 遥测
            if self._telemetry:
                self._telemetry.agent.record_hook(
                    hook.name,
                    0,
                    str(result.action),
                )

            # 累积 additional_context
            if result.additional_context:
                accumulated_context.append(result.additional_context)

            # MODIFY：链式覆盖参数
            if result.is_modified() and result.modified_tool_args:
                if current_tool_args is None:
                    current_tool_args = {}
                current_tool_args.update(result.modified_tool_args)

            # BLOCK：立即短路
            if result.is_blocked():
                result = result.model_copy(
                    update={
                        "additional_context": "\n".join(accumulated_context) if accumulated_context else None,
                    }
                )
                return result

            # STOP：停止链，但不拒绝
            if result.is_stopped():
                break

        # 5. 构造最终结果
        final = HookResult.continue_(
            additional_context="\n".join(accumulated_context) if accumulated_context else None,
        )
        if current_tool_args is not None:
            final = HookResult.modify(
                modified_tool_args=current_tool_args,
                additional_context="\n".join(accumulated_context) if accumulated_context else None,
            )
        return final

    async def _execute_hook(self, hook: RegisteredHook, ctx: HookContext) -> HookResult:
        """执行单个钩子（内置 callable 或外部进程）"""
        if isinstance(hook.handler, ShellCommand):
            return await self._execute_external(hook.handler, ctx)
        else:
            return await asyncio.wait_for(
                self._execute_callable(hook.handler, ctx),
                timeout=hook.timeout,
            )

    async def _execute_callable(self, handler: Callable, ctx: HookContext) -> HookResult:
        """执行内置 Python 钩子"""
        result = handler(ctx)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _execute_external(self, cmd: ShellCommand, ctx: HookContext) -> HookResult:
        """执行外部进程钩子（风格：stdin JSON → stdout JSON）"""
        ctx_json = ctx.model_dump_json()

        env = dict(cmd.env or {})
        env.update(
            {
                "CHACHA_SESSION_ID": ctx.session_id or "",
                "CHACHA_PROJECT_ID": ctx.project_id or "",
                "CHACHA_HOOK_POINT": str(ctx.hook_point),
            }
        )

        proc = await asyncio.create_subprocess_shell(
            cmd.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=ctx_json.encode()),
                timeout=cmd.timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[:500]
            logger.error("外部钩子失败 (exit=%d): %s", proc.returncode, err_text)
            raise RuntimeError(f"外部钩子退出码 {proc.returncode}: {err_text}")

        output = stdout.decode("utf-8", errors="replace")
        try:
            data = json.loads(output)
            return HookResult.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("外部钩子输出解析失败: %s", output[:200])
            raise RuntimeError(f"外部钩子返回非法 JSON: {e}") from e

    # ====== 内部辅助 ======

    def _select(
        self,
        hook_point: Optional[HookPoint],
        tool_call: Optional[Any],
    ) -> List[RegisteredHook]:
        """筛选匹配当前挂载点 + 工具/命令的钩子"""
        if hook_point is None:
            return []

        tool_name = getattr(tool_call, "tool_name", None) if tool_call else None
        command = getattr(tool_call, "command_or_action", None) if tool_call else None

        return [
            h
            for h in self._hooks
            if h.hook_point == hook_point and h.matcher.matches(tool_name=tool_name, command=command)
        ]

    def _get_error_tolerance(self, hook: RegisteredHook) -> bool:
        """
        判断钩子出错/超时后是否可以安全跳过。

        规则：
          1. 显式设置 → 使用显式值
          2. handler 是 ShellCommand → 拒绝（不确定外部进程行为）
          3. handler 是 callable，且只返回 CONTINUE → 容错继续
          4. handler 是 callable，可能返回 BLOCK/MODIFY → 拒绝（安全优先）
        """
        # 显式设置优先
        if hook.on_timeout_continue is not None or hook.on_error_continue is not None:
            return hook.on_timeout_continue and hook.on_error_continue

        # ShellCommand：保守拒绝
        if isinstance(hook.handler, ShellCommand):
            return False

        # callable：默认根据预期行为推断
        return False  # 默认容错（仅当 handler 可能返回 BLOCK 时外部应显式设为 False）

    def list_hooks(self, hook_point: Optional[HookPoint] = None) -> List[Dict[str, Any]]:
        """列出已注册的钩子"""
        hooks = self._hooks
        if hook_point:
            hooks = [h for h in hooks if h.hook_point == hook_point]
        return [
            {
                "name": h.name,
                "hook_point": h.hook_point.value,
                "priority": h.priority,
                "type": "external" if isinstance(h.handler, ShellCommand) else "internal",
            }
            for h in hooks
        ]
