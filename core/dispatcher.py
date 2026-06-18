"""
core/dispatcher.py
Dispatcher — 工具调度器：桥接 ToolExecutor + LLMInvoker。

职责:
  1. 取工具 schemas → 传给 LLM
  2. LLM 返回 tool_calls → 转发给 ToolExecutor 执行
  3. 工具结果注入 messages → 继续 LLM 流式调用
  4. 直到 LLM 不再请求工具 → 返回最终文本

用法:
    dispatcher = Dispatcher(llm_invoker, tool_executor)
    response = await dispatcher.dispatch(messages, session_id)
"""

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.llm_invoker import LLMResponse

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 50           # 防止无限工具循环（Claude Code 实践）
KEEP_TOOL_RESULTS = 5          # 最近 5 个工具结果保持完整，更早的占位
TOOL_CACHE_DIR = Path(".chacha_agent/tool_results")


class Dispatcher:
    """桥接 LLM ↔ 工具执行"""

    def __init__(self, llm_invoker, tool_executor):
        self._llm = llm_invoker
        self._tools = tool_executor
        TOOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def dispatch_stream(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式 LLM + 工具循环。1 次 API 调用/轮（不复用 invoke 导致翻倍）。"""
        schemas = self._tools.get_schemas()
        rounds = 0
        total_tokens = 0

        while rounds < max_rounds:
            rounds += 1

            # 估算本轮输入 token（文本长度 / 2）
            total_tokens += sum(len(str(m.get("content", ""))) for m in messages) // 2

            # 一轮：1 次流式 API 调用
            text_parts: list[str] = []
            tool_calls_building: Dict[int, Dict[str, Any]] = {}
            has_tool_calls = False

            try:
                async for chunk in self._llm.stream(
                    messages=messages,
                    tools=schemas if schemas else None,
                    session_id=session_id,
                ):
                    if chunk.type == "text":
                        text_parts.append(chunk.content)
                        yield {"type": "text", "content": chunk.content}

                    elif chunk.type == "tool_call_start":
                        has_tool_calls = True
                        tool_calls_building[chunk.tool_index] = {
                            "id": chunk.tool_id,
                            "name": chunk.tool_name,
                            "args": "",
                        }
                        yield {
                            "type": "tool_call_start",
                            "tool_name": chunk.tool_name,
                            "tool_id": chunk.tool_id,
                        }

                    elif chunk.type == "tool_call_delta":
                        idx = chunk.tool_index
                        if idx in tool_calls_building:
                            tool_calls_building[idx]["args"] += chunk.tool_args_delta

                    elif chunk.type == "done":
                        if chunk.usage:
                            total_tokens += sum(
                                v for v in chunk.usage.values() if isinstance(v, (int, float))
                            )
                        else:
                            # DeepSeek 不一定会返回 usage，用文本长度估算
                            total_tokens += len("".join(text_parts)) // 2
                        # finish_reason="tool_calls" 时 has_tool_calls 已被设为 True

                    elif chunk.type == "error":
                        yield {"type": "error", "message": chunk.error or "Unknown error"}
                        return
            except Exception as e:
                yield {"type": "error", "message": str(e)}
                return

            # 无工具调用 → 追加文本到消息，结束
            if not has_tool_calls:
                full_text = "".join(text_parts)
                if full_text.strip():
                    messages.append({"role": "assistant", "content": full_text})
                break

            # 有工具调用 → 构建 assistant 消息 + tool_calls
            full_text = "".join(text_parts)
            parsed_tool_calls = []
            for idx in sorted(tool_calls_building.keys()):
                tc = tool_calls_building[idx]
                try:
                    args = json.loads(tc["args"]) if tc["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                parsed_tool_calls.append({
                    "id": tc["id"],
                    "name": tc["name"],
                    "arguments": args,
                })

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": full_text or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in parsed_tool_calls
                ],
            }
            messages.append(assistant_msg)

            # 执行工具 + 注入结果
            for tc in parsed_tool_calls:
                result = await self._tools.execute(
                    tool_name=tc["name"],
                    arguments=tc["arguments"],
                    session_id=session_id,
                    tool_use_id=tc["id"],
                )
                yield {
                    "type": "tool_call_end",
                    "tool_name": tc["name"],
                    "preview": result.output[:200],
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result.output,
                })

            # 主动冻结旧工具结果：超过 KEEP_TOOL_RESULTS 个时，最早的一个 → 占位符
            self._freeze_old_tool_results(messages, session_id)

        yield {
            "type": "done",
            "text": "",
            "tokens": total_tokens,
        }

    async def dispatch(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> LLMResponse:
        """LLM + 工具循环 → 最终响应。

        1. 调用 LLM（带 tools schema）
        2. 如果没有 tool_calls → 返回
        3. 执行工具 → 结果注入 messages → 继续调用 LLM
        """
        schemas = self._tools.get_schemas()
        accumulated_text: List[str] = []
        total_tokens = 0
        final_finish = "stop"
        rounds = 0

        while rounds < max_rounds:
            rounds += 1

            resp = await self._llm.invoke(
                messages=messages,
                tools=schemas if schemas else None,
                session_id=session_id,
            )

            accumulated_text.append(resp.text)
            if resp.usage:
                total_tokens += sum(v for v in resp.usage.values() if isinstance(v, (int, float)))

            if resp.error:
                final_finish = "error"
                accumulated_text.append(f"[Error: {resp.error}]")
                break

            if not resp.tool_calls:
                # 标准化 finish_reason：DeepSeek 可能返回非标准值
                valid = {"stop", "length", "tool_calls", "content_filter"}
                fr = resp.finish_reason if resp.finish_reason in valid else "stop"
                final_finish = fr
                break

            # 先注入 assistant 的 tool_calls 消息（OpenAI 格式要求）
            assistant_tool_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": resp.text or "",
            }
            if resp.tool_calls:
                assistant_tool_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ]
            messages.append(assistant_tool_msg)

            # 执行工具
            for tc in resp.tool_calls:
                result = await self._tools.execute(
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    session_id=session_id,
                    tool_use_id=tc.id,
                )

                # 将工具结果注入 messages（下一步 LLM 会看到）
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.output,
                })

            final_finish = resp.finish_reason or "tool_use"

        # 组装最终响应
        text = "\n".join(accumulated_text)
        return LLMResponse(
            text=text,
            tool_calls=[],  # 最终响应不需要工具
            finish_reason=final_finish,
            usage={"total": total_tokens} if total_tokens else {},
            duration_ms=0,
        )

    @property
    def tool_count(self) -> int:
        return len(self._tools.get_schemas())

    @property
    def schemas(self) -> list:
        return self._tools.get_schemas()

    # ====== 工具结果冻结 ======

    def _freeze_old_tool_results(self, messages: List[Dict], session_id: str) -> None:
        """保持最近 KEEP_TOOL_RESULTS 个工具结果完整，更早的替换为占位符。"""
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool" and "已缓存" not in str(m.get("content", ""))
        ]
        freeze_count = len(tool_indices) - KEEP_TOOL_RESULTS
        if freeze_count <= 0:
            return

        for idx in tool_indices[:freeze_count]:
            msg = messages[idx]
            original = msg.get("content", "")
            if not original or len(original) < 100:
                continue  # 太短不占位

            # 缓存完整结果
            cache_path = TOOL_CACHE_DIR / f"{session_id}_{msg.get('tool_call_id', idx)}.txt"
            try:
                cache_path.write_text(original, encoding="utf-8")
            except Exception:
                pass

            # 占位符
            messages[idx] = {
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": f"[结果已缓存: {cache_path}] (可通过 read_file 查看完整内容)",
            }
