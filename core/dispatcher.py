"""
core/dispatcher.py
Dispatcher — 工具调度器：桥接 ToolExecutor + LLMInvoker。

v2.0 Stage 1 工具结果缓存（宽松）:
  - 保留最近 KEEP_TOOL_RESULTS(=10) 个完整工具结果
  - 更早的结果 → JSON 占位符 {"toolname":"x","result_summary":"x","cache_path":"x"}
  - 缓存到 session/{session_id}/tool_cache/ 目录
  - 会话结束时清理整个 tool_cache 目录

职责:
  1. 取工具 schemas → 传给 LLM
  2. LLM 返回 tool_calls → 转发给 ToolExecutor 执行
  3. 工具结果注入 messages → 继续 LLM 流式调用
  4. 直到 LLM 不再请求工具 → 返回最终文本

用法:
    dispatcher = Dispatcher(llm_invoker, tool_executor, memory_manager)
    response = await dispatcher.dispatch(messages, session_id)
"""

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.llm_invoker import LLMResponse

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 50          # 防止无限工具循环
KEEP_TOOL_RESULTS = 10        # Stage 1 宽松：保留最近 10 个完整工具结果


class Dispatcher:
    """桥接 LLM ↔ 工具执行（v2.0）"""

    def __init__(self, llm_invoker, tool_executor, memory_manager=None):
        self._llm = llm_invoker
        self._tools = tool_executor
        self._memory = memory_manager

    async def dispatch_stream(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式 LLM + 工具循环。"""
        schemas = self._tools.get_schemas()
        rounds = 0

        while rounds < max_rounds:
            rounds += 1

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
                            "tool_index": chunk.tool_index,
                        }
                    elif chunk.type == "tool_call_delta":
                        if chunk.tool_index in tool_calls_building:
                            tool_calls_building[chunk.tool_index]["args"] += chunk.tool_args_delta
                    elif chunk.type == "tool_call_end":
                        yield {
                            "type": "tool_call_end",
                            "tool_index": chunk.tool_index,
                        }
                    elif chunk.type == "done":
                        break
                    elif chunk.type == "error":
                        yield {"type": "error", "message": chunk.content}
            except Exception as e:
                yield {"type": "error", "message": str(e)}
                return

            if not has_tool_calls:
                final_text = "".join(text_parts)
                messages.append({"role": "assistant", "content": final_text})
                yield {"type": "done", "text": "".join(text_parts)}
                return

            # 执行工具
            for idx, tc_info in sorted(tool_calls_building.items()):
                try:
                    args = json.loads(tc_info["args"]) if tc_info["args"] else {}
                except json.JSONDecodeError:
                    args = {}

                result = await self._tools.execute(
                    tool_name=tc_info["name"],
                    arguments=args,
                    session_id=session_id,
                    tool_use_id=tc_info["id"],
                )

                safe_args = {k: str(v)[:100] for k, v in args.items()}
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc_info["id"],
                        "type": "function",
                        "function": {
                            "name": tc_info["name"],
                            "arguments": json.dumps(safe_args, ensure_ascii=False),
                        },
                    }],
                }
                messages.append(assistant_msg)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_info["id"],
                    "content": result.output,
                })

            # Stage 1: 缓存旧工具结果
            self._freeze_old_tool_results(messages, session_id)

        yield {"type": "done", "text": "".join(text_parts)}

    async def dispatch(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> LLMResponse:
        """同步式调度（兼容旧 API）。"""
        accumulated_text: list[str] = []
        total_tokens = 0

        schemas = self._tools.get_schemas()
        rounds = 0
        final_finish = "stop"

        while rounds < max_rounds:
            rounds += 1

            total_tokens += sum(len(str(m.get("content", ""))) for m in messages) // 2

            resp = await self._llm.invoke(
                messages=messages,
                tools=schemas if schemas else None,
                session_id=session_id,
            )

            if resp.error:
                return resp

            if resp.text:
                accumulated_text.append(resp.text)

            if not resp.tool_calls:
                final_finish = resp.finish_reason or "stop"
                break

            assistant_tool_msg = {
                "role": "assistant",
                "content": resp.text or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            }
            messages.append(assistant_tool_msg)

            for tc in resp.tool_calls:
                result = await self._tools.execute(
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    session_id=session_id,
                    tool_use_id=tc.id,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.output,
                })

            # Stage 1: 缓存旧工具结果
            self._freeze_old_tool_results(messages, session_id)

            final_finish = resp.finish_reason or "tool_use"

        text = "\n".join(accumulated_text)
        return LLMResponse(
            text=text,
            tool_calls=[],
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

    # ====== Stage 1 工具结果缓存（宽松） ======

    def _freeze_old_tool_results(self, messages: List[Dict], session_id: str) -> None:
        """保持最近 KEEP_TOOL_RESULTS 个工具结果完整，更早的替换为 JSON 占位符。

        占位格式: {"toolname":"read_file","result_summary":"读取 main.py 前200行...","cache_path":"tool_cache/t3.json"}
        """
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool" and not m.get("content", "").startswith("{")
        ]
        freeze_count = len(tool_indices) - KEEP_TOOL_RESULTS
        if freeze_count <= 0:
            return

        for idx in tool_indices[:freeze_count]:
            msg = messages[idx]
            original = msg.get("content", "")
            if not original or len(original) < 100:
                continue

            tool_use_id = msg.get("tool_call_id", f"t{idx}")
            tool_name = self._guess_tool_name(messages, idx)

            # 摘要（前 120 字符）
            summary = original[:120].replace("\n", " ").strip()
            if len(original) > 120:
                summary += "..."

            # 缓存到 session/tool_cache/
            if self._memory:
                cache_path = self._memory.cache_tool_result(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    result=original,
                )
                cache_ref = f"tool_cache/{cache_path.name}"
            else:
                cache_ref = f"tool_cache/{session_id}_{tool_use_id}.json"

            placeholder = json.dumps({
                "toolname": tool_name,
                "result_summary": summary,
                "cache_path": cache_ref,
            }, ensure_ascii=False)

            messages[idx] = {
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": placeholder,
            }

    @staticmethod
    def _guess_tool_name(messages: List[Dict], tool_idx: int) -> str:
        """从 messages 中猜测 tool_call_id 对应的工具名。"""
        target_id = messages[tool_idx].get("tool_call_id", "")
        for i in range(tool_idx - 1, -1, -1):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc.get("id") == target_id:
                        return tc.get("function", {}).get("name", "unknown")
        return "unknown"
