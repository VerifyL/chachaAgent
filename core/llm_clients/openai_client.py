"""
core/model/openai_client.py
OpenAIClient — OpenAI 及兼容 API（DeepSeek / Ollama / Qwen）的流式适配器。

通过 base_url 参数兼容任何 OpenAI-compatible API。

用法:
    # OpenAI
    client = OpenAIClient(api_key="sk-...", model="gpt-4")
    # DeepSeek
    client = OpenAIClient(api_key="sk-...", model="deepseek-chat",
                          base_url="https://api.deepseek.com/v1")
    # Ollama (本地)
    client = OpenAIClient(model="llama3", base_url="http://localhost:11434/v1",
                          api_key="ollama")
    # 接入 LLMInvoker
    invoker = LLMInvoker(model_client=client)
"""

import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from core.llm_invoker import (
    TextChunk, ReasoningChunk, ToolCallStartChunk,
    ToolCallDeltaChunk, DoneChunk,
)

logger = logging.getLogger(__name__)


class OpenAIClient:
    """OpenAI 及兼容 API 流式客户端。

    通过 stream() 将 API 事件转换为 LLMInvoker 所需的 StreamChunk 序列。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4",
        base_url: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 16384,
        timeout: float = 120.0,
    ):
        self._model = model
        self._temperature = temperature
        # 优先级: 环境变量 → 构造参数 → 默认 4096
        env_max = os.environ.get("MAX_TOKENS")
        self._max_tokens = int(env_max) if env_max else max_tokens

        kwargs: Dict[str, Any] = {"api_key": api_key or "sk-placeholder"}
        if base_url:
            kwargs["base_url"] = base_url
        if timeout:
            kwargs["timeout"] = timeout

        self._client = AsyncOpenAI(**kwargs)

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[Any]:
        """流式调用 LLM，逐个返回 StreamChunk。"""
        # DeepSeek thinking mode: 需保留 reasoning_content（但不要传 null）
        _messages = []
        for m in messages:
            entry = dict(m)
            if entry.get("role") == "assistant" and "reasoning_content" in entry and entry["reasoning_content"] is None:
                del entry["reasoning_content"]
            _messages.append(entry)

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": _messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        response = await self._client.chat.completions.create(**kwargs)

        # 追踪每个 tool_call index 是否已发出 tool_call_start
        started_indices: set[int] = set()
        usage_info: Dict[str, int] = {}

        async for event in response:
            choices = getattr(event, "choices", [])
            if not choices:
                continue

            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue

            # 1. 文本内容 + DeepSeek reasoning
            content = getattr(delta, "content", None)
            if content:
                yield TextChunk(content=content)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ReasoningChunk(content=reasoning)

            # 2. 工具调用
            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    idx = getattr(tc, "index", 0)

                    # tool_call_start：首次出现 id
                    tc_id = getattr(tc, "id", None)
                    tc_name = None
                    fn = getattr(tc, "function", None)
                    if fn:
                        tc_name = getattr(fn, "name", None)

                    if tc_id and idx not in started_indices:
                        started_indices.add(idx)
                        yield ToolCallStartChunk(
                            tool_index=idx,
                            tool_id=tc_id,
                            tool_name=tc_name or "",
                        )

                    # tool_call_delta：参数增量
                    if fn:
                        args_delta = getattr(fn, "arguments", None)
                        if args_delta:
                            yield ToolCallDeltaChunk(
                                tool_index=idx,
                                tool_args_delta=args_delta,
                            )

            # 3. 结束
            finish_reason = getattr(choices[0], "finish_reason", None)
            if finish_reason:
                # 尝试获取 usage（不同提供商行为不同）
                event_usage = getattr(event, "usage", None)
                if event_usage:
                    usage_info = {
                        "input": getattr(event_usage, "prompt_tokens", 0),
                        "output": getattr(event_usage, "completion_tokens", 0),
                        "total": getattr(event_usage, "total_tokens", 0),
                        "cache_hit": getattr(event_usage, "prompt_cache_hit_tokens", 0),
                        "cache_miss": getattr(event_usage, "prompt_cache_miss_tokens", 0),
                        "reasoning": getattr(event_usage, "completion_tokens_details", None) and
                                     getattr(event_usage.completion_tokens_details, "reasoning_tokens", 0) or 0,
                        "model": self._model,
                    }

                yield DoneChunk(
                    finish_reason=finish_reason,
                    usage=usage_info if usage_info else None,
                )
                return

        # 流自然结束但无 finish_reason（兼容某些提供商）
        yield DoneChunk(
            finish_reason="stop",
            usage=usage_info if usage_info else None,
        )
