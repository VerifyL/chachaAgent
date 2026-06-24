"""
core/tool_executor.py
ToolExecutor — 工具执行调度器：查找、审批、钩子、执行、遥测。

设计理念（融合 StreamingToolExecutor + 错误即观察）：
1. 薄胶水层：PolicyEngine/HookOrchestrator/Telemetry 已独立实现，本模块只做编排
2. 并发执行：asyncio.Semaphore 控制上限
3. 错误即观察：执行异常不抛，包装为 ToolResult(error=True) 反馈给 LLM
4. 超时+退避重试：仅网络/超时可重试，权限/黑名单拦截不重试（参考 Harness）
5. 遥测透明：执行完成后自动调用 telemetry.agent.record_tool_call()（可选注入）

用法:
    registry = {"read_file": read_file_fn, "shell": shell_fn}
    executor = ToolExecutor(registry, policy_engine, hook_orch, telemetry=telemetry)
    result = await executor.execute("read_file", {"path": "/tmp/a.py"}, "session-1")
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from core.models.audit import audit_factory, AuditEventCategory
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# 工具函数签名：async (arguments: dict) -> str
ToolFunc = Callable[[Dict[str, Any]], Coroutine[Any, Any, str]]


# ========================= 可重试与不可重试异常 =========================

RETRYABLE_EXCEPTIONS = (
    asyncio.TimeoutError,
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    OSError,  # 临时性 I/O 错误（如 "Too many open files"）
)
"""可重试异常：超时、网络断开、临时 I/O 错误。"""

NON_RETRYABLE_EXCEPTIONS = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    PermissionError,
    FileNotFoundError,
    IsADirectoryError,
    NotADirectoryError,
)
"""不可重试异常：参数错误、权限错误、文件不存在等永久性错误。"""

# ========================= 错误分类 =========================

class ToolNotFoundError(Exception):
    """工具未注册"""
    pass


class ToolTimeoutError(Exception):
    """工具执行超时"""
    pass


class ToolPermissionError(Exception):
    """权限不足（PolicyEngine 拦截）"""
    pass


# ========================= 审批请求 =========================

@dataclass
class ApprovalRequest:
    """审批请求上下文"""
    tool_name: str
    arguments: Dict[str, Any]
    risk_level: str                # RiskLevel.value
    risk_score: float
    session_id: str
    tool_use_id: str
    diff: Optional[str] = None     # edit_file 的变更 diff
    reason: str = ""               # 需要审批的原因


# ========================= 执行结果 =========================

@dataclass
class ToolResult:
    """工具执行结果"""
    tool_use_id: str
    tool_name: str
    status: str = "success"       # success | error | blocked | timeout
    output: str = ""               # 工具输出文本
    error: Optional[str] = None    # 错误详情
    duration_ms: int = 0           # 耗时（毫秒）
    truncated: bool = False        # 输出是否被截断


# ========================= 工具执行器 =========================

class ToolExecutor:
    """
    工具执行调度器。

    执行流程:
      find → policy → pre-hooks → execute → post-hooks → telemetry → return

    参数 injected 均可为 None（测试/渐进构建友好）。
    """

    def __init__(
        self,
        tools: Optional[list] = None,                # List[BaseTool] 或 Dict[str, Callable]（向后兼容）
        policy_engine: Optional[Any] = None,        # PolicyEngine
        hook_orchestrator: Optional[Any] = None,     # HookOrchestrator
        telemetry: Optional[Any] = None,             # Telemetry
        approval_handler: Optional[Callable[[ApprovalRequest], Coroutine[Any, Any, bool]]] = None,
        max_concurrent: int = 5,
        default_timeout: float = 60.0,
        max_retries: int = 2,
        max_output_chars: int = 200_000,
    ):
        self._tools: dict[str, Any] = {}
        self._tool_objects: list = list(tools or [])

        if self._tool_objects and hasattr(self._tool_objects[0], 'name'):
            # List[BaseTool] → 建立 name→object 映射
            for t in self._tool_objects:
                self._tools[t.name] = t
        elif isinstance(self._tool_objects, dict):
            self._tools = self._tool_objects
        elif tools and isinstance(tools, dict):
            self._tools = tools

        self._policy = policy_engine
        self._hooks = hook_orchestrator
        self._telemetry = telemetry
        self._approval_handler = approval_handler
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout = default_timeout
        self._max_retries = max_retries
        self._max_output_chars = max_output_chars
        self._output_cache: Dict[str, tuple[str, float]] = {}  # cache_key → (full_output, expiry_ts)

    # ====== 公开接口 ======

    def get_schemas(self) -> list[dict]:
        """获取所有工具的 function calling schema"""
        if self._tool_objects and hasattr(self._tool_objects[0], 'to_function_schema'):
            return [t.to_function_schema() for t in self._tool_objects]
        return []

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        session_id: str,
        tool_use_id: str = "",
        project_id: str = "",
    ) -> ToolResult:
        """执行单个工具，含审批、钩子、超时、重试。"""
        t0 = time.monotonic()

        # 0. 查找工具
        tool_fn = self._tools.get(tool_name)
        if tool_fn is None:
            return ToolResult(
                tool_use_id=tool_use_id, tool_name=tool_name,
                status="error", error=f"Tool '{tool_name}' not found",
                duration_ms=0,
            )

        # 1. 策略评估
        if self._policy:
            decision = self._policy.evaluate_tool(
                tool_name, command_or_action=str(arguments.get("cmd", "")),
                session_id=session_id,
                parameters=arguments,
            )
            if not decision.allowed:
                return ToolResult(
                    tool_use_id=tool_use_id, tool_name=tool_name,
                    status="blocked",
                    error=decision.blocked_reason or "Policy blocked",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            if decision.needs_approval:
                # 1. 构造审批请求
                diff = None
                if tool_name == "edit_file":
                    diff = self._compute_edit_diff(arguments)

                req = ApprovalRequest(
                    tool_name=tool_name,
                    arguments=arguments,
                    risk_level=decision.risk_level.value,
                    risk_score=decision.risk_score,
                    session_id=session_id,
                    tool_use_id=tool_use_id,
                    diff=diff,
                    reason=f"工具 '{tool_name}' 需要审批 (风险等级: {decision.risk_level.value}, 分数: {decision.risk_score:.0f})",
                )

                # 2. 调用审批处理器
                if self._approval_handler:
                    approved = await self._approval_handler(req)
                else:
                    # 无审批处理器 → 系统类默认拒绝，其余默认放行
                    from core.policy_engine import PolicyEngine
                    approved = tool_name not in PolicyEngine.SYSTEM_TOOLS

                # 3. 审批结果
                if not approved:
                    return ToolResult(
                        tool_use_id=tool_use_id, tool_name=tool_name,
                        status="blocked",
                        error=f"用户拒绝了 '{tool_name}' 的执行",
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )

                # 4. 审批通过 → 记录到 PolicyEngine 缓存
                if self._policy and decision.cache_key:
                    self._policy.record_approval(decision.cache_key, True)

        # 2. 前置钩子（关键工具豁免：记忆读写不被拦截）
        _HOOK_BYPASS_TOOLS = {"write_topic", "read_topic", "load_memory"}
        if self._hooks and tool_name not in _HOOK_BYPASS_TOOLS:
            from core.models.hook import ToolCallContext, HookPoint
            tc = ToolCallContext(
                tool_name=tool_name, tool_use_id=tool_use_id,
                arguments=arguments,
            )
            result = await self._hooks.run(
                session_id=session_id,
                hook_point=HookPoint.PRE_TOOL_EXECUTION,
                tool_call=tc,
            )
            if result.is_blocked():
                return ToolResult(
                    tool_use_id=tool_use_id, tool_name=tool_name,
                    status="blocked",
                    error=result.message or "Hooks blocked",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            if result.is_modified() and result.modified_tool_args:
                arguments.update(result.modified_tool_args)

        # 3. 执行（带超时 + 重试）
        output, error, status = await self._execute_with_retry(tool_name, arguments)

        # 4. 智能截断（在 \n 边界，不切断行）
        truncated = False
        cache_key = ""
        if len(output) > self._max_output_chars:
            # 在 \n 边界截断，避免切断行
            cut = output[:self._max_output_chars]
            last_nl = cut.rfind("\n")
            if last_nl > self._max_output_chars // 2:
                cut = cut[:last_nl]
            remaining = len(output) - len(cut)
            # 缓存完整输出，生成续读 key
            import hashlib, time
            cache_key = hashlib.md5(f"{tool_name}:{tool_use_id}:{time.time()}".encode()).hexdigest()[:12]
            self._output_cache[cache_key] = (output, time.time())
            self._cleanup_cache()
            # 分页提示（根据工具类型给出具体建议）
            hint = self._truncation_hint(tool_name, arguments, remaining)
            output = f"{cut}\n... [截断，剩余 {remaining} 字符。续读: cache_key={cache_key}]\n{hint}"
            truncated = True

        duration = int((time.monotonic() - t0) * 1000)

        # 5. 后置钩子
        if self._hooks:
            from core.models.hook import HookPoint
            await self._hooks.run(
                session_id=session_id,
                hook_point=HookPoint.POST_TOOL_EXECUTION,
                tool_call=None,
            )

        # 6. 遥测
        if self._telemetry and self._telemetry.agent:
            self._telemetry.agent.record_tool_call(
                tool_name, duration, status == "success",
                output_lines=output.count("\n") + 1,
                _logger=self._telemetry.logger,
            )
            if self._telemetry.logger:
                record = audit_factory(
                    AuditEventCategory.TOOL_CALL,
                    session_id=session_id,
                    project_id=project_id,
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    duration_ms=duration,
                    arguments_summary={k: str(v)[:100] for k, v in arguments.items()},
                    status="success",
                )
                self._telemetry.logger.audit(record)

        return ToolResult(
            tool_use_id=tool_use_id, tool_name=tool_name,
            status=status, output=output, error=error,
            duration_ms=duration, truncated=truncated,
        )

    async def execute_batch(
        self,
        calls: List[Dict[str, Any]],
        session_id: str,
    ) -> List[ToolResult]:
        """并发执行多个工具调用（参考 Claude Code StreamingToolExecutor）。"""
        tasks = []
        for call in calls:
            tasks.append(
                self.execute(
                    tool_name=call["tool_name"],
                    arguments=call.get("arguments", {}),
                    session_id=session_id,
                    tool_use_id=call.get("tool_use_id", ""),
                )
            )
        return await asyncio.gather(*tasks)

    # ====== 内部 ======

    async def _execute_with_retry(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, Optional[str], str]:
        """超时退避重试（参考 Harness：权限/黑名单不重试，超时/网络错误重试）。"""
        last_error = None

        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    tool = self._tools[tool_name]
                    # BaseTool → call .execute(**args); Callable → call directly
                    if hasattr(tool, 'execute'):
                        output = await asyncio.wait_for(
                            tool.execute(**arguments),
                            timeout=self._timeout,
                        )
                    else:
                        output = await asyncio.wait_for(
                            tool(arguments),
                            timeout=self._timeout,
                        )
                return str(output), None, "success"

            except asyncio.TimeoutError:
                last_error = f"Tool '{tool_name}' timed out after {self._timeout}s (attempt {attempt + 1}/{self._max_retries + 1})"
                if attempt < self._max_retries:
                    backoff = 2 ** attempt
                    logger.warning("%s, retrying in %ds", last_error, backoff)
                    await asyncio.sleep(backoff)
                else:
                    return "", last_error, "timeout"

            except RETRYABLE_EXCEPTIONS as e:
                last_error = (
                    f"Tool '{tool_name}' failed with {type(e).__name__}: {e} "
                    f"(attempt {attempt + 1}/{self._max_retries + 1})"
                )
                if attempt < self._max_retries:
                    backoff = 2 ** attempt
                    logger.warning("%s, retrying in %ds", last_error, backoff)
                    await asyncio.sleep(backoff)
                else:
                    return "", last_error, "error"

            except NON_RETRYABLE_EXCEPTIONS as e:
                # 永久性错误不重试，立即失败
                return "", f"{type(e).__name__}: {e}", "error"

        return "", last_error or "unknown", "timeout"


    # ====== 截断辅助 ======

    @staticmethod
    def _truncation_hint(tool_name: str, arguments: Dict[str, Any], remaining: int) -> str:
        """根据工具类型生成分页/续读提示。"""
        if tool_name in ("git_diff",):
            return '[hint] 输出过大，可用 git_diff(path="...") 按文件过滤查看'
        if tool_name == "git_log":
            return '[hint] 可用 git_log(n=5, path="...") 缩小范围'
        if tool_name == "bash":
            return '[hint] 可用 bash("... | head -n N") 或 tail 缩小输出'
        if tool_name == "grep":
            offset = arguments.get("offset", 0)
            limit = arguments.get("limit", 200)
            return f"[hint] 可用 offset={offset + limit} 查看下一页"
        if tool_name == "read_file":
            offset = arguments.get("offset", 1)
            limit = arguments.get("limit", 100)
            return f"[hint] 可用 offset={offset + limit} 续读下一页"
        return f"[hint] 可用 cache_key 续读完整输出"

    def _get_cached_output(self, cache_key: str, offset: int = 0, limit: int = 500) -> str:
        """获取缓存的完整输出（分页）。"""
        entry = self._output_cache.get(cache_key)
        if entry is None:
            return "[错误] 缓存已过期或不存在，请重新执行原始工具调用"
        full_output, _ = entry
        chunk = full_output[offset:offset + limit]
        has_more = offset + limit < len(full_output)
        result = f"[cache_key={cache_key}] offset={offset} limit={limit}\n{chunk}"
        if has_more:
            result += f"\n... [还有 {len(full_output) - offset - limit} 字符，续读: offset={offset + limit}]"
        return result

    def _cleanup_cache(self) -> None:
        """清理超过 5 分钟的过期缓存。"""
        now = time.time()
        expired = [k for k, (_, ts) in self._output_cache.items() if now - ts > 300]
        for k in expired:
            self._output_cache.pop(k, None)

    # ====== 审批辅助 ======

    @staticmethod
    def _compute_edit_diff(arguments: Dict[str, Any]) -> Optional[str]:
        """为 edit_file 计算变更 diff。"""
        path = arguments.get("path", "")
        old_str = arguments.get("old_string", "")
        new_str = arguments.get("new_string", "")

        if not path or not old_str:
            return None

        import difflib
        old_lines = old_str.splitlines(keepends=True)
        new_lines = new_str.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))
        return "".join(diff_lines) if diff_lines else "(无变更)"

    # ====== 查询 ======

    def get_tools(self) -> list:
        """返回所有已注册工具对象的列表。"""
        return list(self._tool_objects) if isinstance(self._tool_objects, list) else []

    def list_tools(self) -> List[str]:
        return sorted(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._tools
