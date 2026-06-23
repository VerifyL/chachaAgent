"""
core/llm_invoker.py
LLMInvoker — 流式 LLM 调用器：流式分块、工具调用增量解析、异常映射、遥测。

设计理念（ 异步生成器 +结构化输出）：
1. 模型客户端解耦：仅依赖 AsyncIterator[StreamChunk] 接口，不绑定具体适配器
2. 流式输出：text chunk → Gateway.publish(TokenChunkEvent) → 前端实时展示
3. 工具调用增量解析：tool_call_delta 累积 → 最终 parse JSON → OutputGovernor 修复
4. 异常映射：429→重试 / 401/403→认证 / 超时→timeout / 其他→通用异常
5. 自动遥测：record_llm_call() 在 invoke 完成时调用

用法:
    invoker = LLMInvoker(model_client, gateway, telemetry, output_governor, policy_engine)
    resp = await invoker.invoke(messages, tools, session_id)
    # resp.text, resp.tool_calls, resp.usage
"""

import json
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from core.models.context import CompressionLevel

logger = logging.getLogger(__name__)


# ========================= 接口定义 =========================

@dataclass
class StreamChunk:
    """模型适配器返回的流式块（适配器无关接口）"""
    type: str = ""               # text | tool_call_start | tool_call_delta | tool_call_end | done
    content: str = ""            # text 类型的内容
    tool_index: int = -1         # tool_call 类型的索引
    tool_id: str = ""            # tool_call_start
    tool_name: str = ""          # tool_call_start
    tool_args_delta: str = ""    # tool_call_delta
    finish_reason: str = ""      # done 类型：stop | tool_calls | length | error
    usage: Optional[Dict[str, int]] = None  # done 类型：{input, output, total}
    error: Optional[str] = None  # error 类型


@dataclass
class ToolCall:
    """解析后的工具调用"""
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    _args_raw: str = ""  # 流式累积的原始 JSON 字符串（内部使用）


@dataclass
class LLMResponse:
    """LLM 调用完整结果"""
    text: str = ""                         # 文本输出（所有 text chunk 拼接）
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"            # stop | tool_calls | length | content_filter
    error: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)  # {input, output, total}
    duration_ms: int = 0


# ========================= 调用器 =========================

class LLMInvoker:
    """
    流式 LLM 调用器。

    模型客户端只需实现:
        async def stream(messages, tools) -> AsyncIterator[StreamChunk]: ...

    参数均可为 None（渐进构建）。

    TODO(阶段3): 接入真实的 OpenAI/Anthropic/Ollama 适配器
    TODO(阶段3): 接入 UsageTracker 精确成本计算，替换粗略估算
    """

    def __init__(
        self,
        model_client: Optional[Any] = None,         # stream() → AsyncIterator[StreamChunk]
        gateway: Optional[Any] = None,              # ChaChaAsyncGateway
        telemetry: Optional[Any] = None,            # Telemetry
        output_governor: Optional[Any] = None,      # OutputGovernor
        policy_engine: Optional[Any] = None,        # PolicyEngine（成本熔断）
        retry_handler: Optional[Any] = None,        # RetryHandler（指数退避）
    ):
        self._client = model_client
        self._gateway = gateway
        self._telemetry = telemetry
        self._governor = output_governor
        self._policy = policy_engine
        self._retry = retry_handler

    async def invoke(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> LLMResponse:
        """发起 LLM 请求 → 返回完整 LLMResponse。"""
        return await self._invoke_impl(messages, tools, session_id)

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> Any:
        """流式调用 LLM，返回 AsyncIterator[StreamChunk]。

        注意：必须是 async generator（yield 表达式），不能用 return。
        """
        try:
            if self._retry:
                async for chunk in self._retry.execute(
                    self._client.stream, messages, tools or [],
                ):
                    yield chunk
            else:
                async for chunk in self._client.stream(messages, tools):
                    yield chunk
        except (KeyboardInterrupt, asyncio.CancelledError):
            yield StreamChunk(type="error", content="用户中断")

    async def _invoke_impl(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> LLMResponse:
        """发起 LLM 请求，流式输出到 Gateway，返回完整结果。"""
        t0 = time.monotonic()

        if self._client is None:
            return LLMResponse(error="No model client configured", duration_ms=0)

        # 1. 成本熔断
        if self._policy:
            allowed, reason, _ = self._policy.evaluate_cost(0.0)
            if not allowed:
                return LLMResponse(error=reason or "Cost circuit breaker open", duration_ms=0)

        # 2. 流式调用（带重试）
        text_parts: List[str] = []
        tool_calls: Dict[int, ToolCall] = {}
        finish_reason = "stop"
        usage: Dict[str, int] = {}

        try:
            if self._retry:
                it = self._retry.execute(
                    self._client.stream, messages, tools or [],
                )
            else:
                it = self._client.stream(messages, tools or [])
            async for chunk in it:
                if chunk.type == "text":
                    text_parts.append(chunk.content)
                    if self._gateway:
                        from protocol.rpc_schema import TokenChunkEvent
                        await self._gateway.publish(
                            TokenChunkEvent().set_delta(chunk.content),
                            session_id=session_id,
                        )

                elif chunk.type == "tool_call_start":
                    tool_calls[chunk.tool_index] = ToolCall(
                        id=chunk.tool_id, name=chunk.tool_name,
                    )

                elif chunk.type == "tool_call_delta":
                    tc = tool_calls.get(chunk.tool_index)
                    if tc:
                        tc._args_raw += chunk.tool_args_delta

                elif chunk.type == "tool_call_end":
                    tc = tool_calls.get(chunk.tool_index)
                    if tc:
                        tc._args_raw += chunk.tool_args_delta

                elif chunk.type == "done":
                    finish_reason = chunk.finish_reason or finish_reason
                    usage = chunk.usage or {}

                elif chunk.type == "error":
                    mapped = self._map_error(Exception(chunk.error or "Unknown error"))
                    return LLMResponse(
                        text="".join(text_parts),
                        error=mapped,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )

        except Exception as e:
            return LLMResponse(
                text="".join(text_parts),
                error=self._map_error(e),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        # 3. 流结束 → Gateway 通知
        if self._gateway:
            from protocol.rpc_schema import TokenChunkEvent
            await self._gateway.publish(
                TokenChunkEvent().set_finish(finish_reason),
                session_id=session_id,
            )

        # 4. 解析工具参数 + OutputGovernor 修复残缺 JSON
        final_tool_calls = list(tool_calls.values())
        for tc in final_tool_calls:
            args_str = tc._args_raw or "{}"
            if self._governor:
                valid, repaired = self._governor.validate_tool_call(args_str)
                args_str = repaired
            try:
                tc.arguments = json.loads(args_str)
            except json.JSONDecodeError:
                tc.arguments = {"_raw": args_str}

        duration = int((time.monotonic() - t0) * 1000)
        input_tokens = usage.get("input", 0)
        output_tokens = usage.get("output", 0)

        # 5. 成本记录
        if self._policy:
            est_cost = (input_tokens * 0.003 + output_tokens * 0.015) / 1000  # 粗略估算
            self._policy.evaluate_cost(est_cost)

        # 6. 遥测
        if self._telemetry:
            self._telemetry.agent.record_llm_call(
                model=usage.get("model", "unknown"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=duration,
                success=not bool(self._get_last_error()),
            )

        return LLMResponse(
            text="".join(text_parts),
            tool_calls=final_tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            duration_ms=duration,
        )

    # ====== 异常映射 ======

    def _map_error(self, exc: Exception) -> str:
        """映射常见异常到可读信息（参考 Harness 错误分类）"""
        name = type(exc).__name__
        msg = str(exc)

        if "429" in msg or "rate" in msg.lower():
            return f"Rate limited: {msg}"
        if "401" in msg or "403" in msg:
            # 遮罩 API key
            import re
            msg = re.sub(r'(api[ _]?key[:\s]*["\']?)([^"\'}\]]+)',
                         lambda m: m.group(1) + "***", msg, flags=re.IGNORECASE)
            return f"Authentication error: {msg}"
        if "timeout" in msg.lower():
            return f"Timeout: {msg}"
        if "connection" in msg.lower():
            return f"Connection error: {msg}"

        return f"{name}: {msg}"

    def _get_last_error(self) -> Optional[str]:
        return None  # 调用方在 LLMResponse 中检查
