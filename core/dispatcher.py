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

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.llm_invoker import LLMResponse
from core.tool_executor import ToolResult

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 200          # 防止无限工具循环
KEEP_TOOL_RESULTS = 8

# API Key mask regex (module-level to avoid recompilation on every error)
_API_KEY_RE = re.compile(
    r'(api[ _]?key[:\s]*["\x27]?)([^"\x27}\]]+)',
    re.IGNORECASE,
)

def _tool_args_summary(name: str, args: dict) -> str:
    if name in ("read_file", "bash", "list_files", "file_outline",
                 "depe_analyze", "project_overview", "load_memory",
                 "write_topic", "read_topic", "subagent", "subagent"):
        items = [f"{k}={v}" for k, v in args.items()]
        return " ".join(items)
    if name == "read_files" and "paths" in args:
        paths = args["paths"]
        return f"paths={paths}"
    if name == "grep":
        pat = args.get("pattern", "")
        loc = args.get("path", "") or ""
        return f"pattern={pat}" + (f" path={loc}" if loc else "")
    if name == "edit_file":
        return f"path={args.get('path', '?')}"
    items = [f"{k}={v}" for k, v in args.items()]
    return " ".join(items)



class Dispatcher:
    """桥接 LLM ↔ 工具执行（v2.0）"""

    def __init__(self, llm_invoker, tool_executor, memory_manager=None,
                 telemetry=None, project_id="", context_window=1_048_576):
        self._llm = llm_invoker
        self._tools = tool_executor
        self._memory = memory_manager
        self._telemetry = telemetry
        self._project_id = project_id
        self._max_context_window = context_window
        self.tool_calls_made = 0
        self._last_failed_call = ""           # circuit breaker: (tool_name, args_hash)
        self._same_call_failures = 0         # 同一调用连续失败计数
        self._max_same_call_failures = 5     # 同一调用连续失败上限

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
            reasoning_parts: list[str] = []
            tool_calls_building: Dict[int, Dict[str, Any]] = {}
            has_tool_calls = False

            try:
                llm_t0 = time.monotonic()
                llm_tokens = 0
                llm_usage: dict = {}
                llm_ok = True
                async for chunk in self._llm.stream(
                    messages=messages,
                    tools=schemas if schemas else None,
                    session_id=session_id,
                ):
                    if chunk.type == "text":
                        text_parts.append(chunk.content)
                        yield {"type": "text", "content": chunk.content}
                    elif chunk.type == "reasoning":
                        reasoning_parts.append(chunk.content)
                        yield {"type": "reasoning", "content": chunk.content}
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
                        if chunk.usage:
                            llm_tokens = chunk.usage.get("total", 0)
                            llm_usage = chunk.usage
                        break
                    elif chunk.type == "error":
                        yield {"type": "error", "message": chunk.content}
            except GeneratorExit:
                return
            except Exception as e:
                llm_ok = False
                msg = str(e)
                if "401" in msg or "403" in msg:
                    msg = _API_KEY_RE.sub(lambda m: m.group(1) + "***", msg)
                yield {"type": "error", "message": msg}
                return
            finally:
                # LLM 调用遥测
                tel = self._telemetry
                if tel and tel.agent and tel.logger:
                    ms = int((time.monotonic() - llm_t0) * 1000)
                    tel.agent.record_llm_call(
                        model="", input_tokens=0, output_tokens=llm_tokens,
                        latency_ms=ms, success=llm_ok,
                    )
                    tel.logger.info("LLM 调用", model=getattr(self._llm, "_model", ""),
                                    tokens=llm_tokens, duration_ms=ms)

            if not has_tool_calls:
                final_text = "".join(text_parts)
                messages.append({"role": "assistant", "content": final_text})
                yield {"type": "done", "text": "".join(text_parts), "tokens": llm_tokens, "usage": llm_usage}
                return

            # 执行工具
            # 先构造 1 个 assistant 消息（含所有 tool_calls + reasoning）
            _tc_id_to_name: Dict[str, str] = {}
            safe_tool_calls = []
            for idx, tc_info in sorted(tool_calls_building.items()):
                try:
                    args = json.loads(tc_info["args"]) if tc_info["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                safe_args = {k: str(v)[:100] for k, v in args.items()}
                safe_tool_calls.append({
                    "id": tc_info["id"],
                    "type": "function",
                    "function": {
                        "name": tc_info["name"],
                        "arguments": json.dumps(safe_args, ensure_ascii=False),
                    },
                })
                _tc_id_to_name[tc_info["id"]] = tc_info["name"]

            assistant_msg = {"role": "assistant", "content": "".join(text_parts) or None}
            if reasoning_parts:
                assistant_msg["reasoning_content"] = "".join(reasoning_parts)
            assistant_msg["tool_calls"] = safe_tool_calls
            messages.append(assistant_msg)

            # 执行并追加工具结果（并发执行）
            # Phase 1: 解析参数 + 发出 tool_exec_start 事件 + 构建 task 列表
            _tc_infos: list = []
            tasks: list = []
            for idx, tc_info in sorted(tool_calls_building.items()):
                try:
                    args = json.loads(tc_info["args"]) if tc_info["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                self.tool_calls_made += 1
                arg_summary = _tool_args_summary(tc_info["name"], args)
                yield {"type": "tool_exec_start", "tool_name": tc_info["name"], "args": arg_summary}
                _tc_infos.append((tc_info, arg_summary))
                tasks.append(self._tools.execute(
                    tool_name=tc_info["name"],
                    arguments=args,
                    session_id=session_id,
                    tool_use_id=tc_info["id"],
                    project_id=self._project_id,
                ))

            # Phase 2: 并发执行所有工具（ToolExecutor 内部 Semaphore(5) 兜底）
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Phase 3: 按原始顺序处理结果（Circuit Breaker 顺序累加，行为不变）
            for i, (tc_info, arg_summary) in enumerate(_tc_infos):
                raw = results[i]
                if isinstance(raw, Exception):
                    result = ToolResult(
                        tool_use_id=tc_info["id"],
                        tool_name=tc_info["name"],
                        status="error",
                        output="",
                        error=str(raw),
                    )
                else:
                    result = raw

                # blocked / pending_approval → 转为错误消息
                if result.status in ("blocked", "pending_approval"):
                    result.output = f"[{result.status}] {result.error or '工具执行被阻止'}"

                # Dynamic circuit breaker: same (tool+args) consecutive failures
                call_key = f"{tc_info['name']}:{arg_summary}"
                if result.status in ("error", "blocked", "timeout"):
                    if call_key == self._last_failed_call:
                        self._same_call_failures += 1
                    else:
                        self._last_failed_call = call_key
                        self._same_call_failures = 1
                else:
                    self._last_failed_call = ""
                    self._same_call_failures = 0

                if self._same_call_failures >= self._max_same_call_failures:
                    yield {
                        "type": "error",
                        "message": (
                            f"Circuit breaker: same call failed {self._same_call_failures} times "
                            f"({tc_info['name']}), terminating loop"
                        ),
                    }
                    return
                yield {"type": "tool_exec_end", "tool_name": tc_info["name"],
                       "preview": (result.output or result.error or "")[:80].split("\n")[0]}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_info["id"],
                    "content": result.output,
                })

            # Stage 1: 缓存旧工具结果
            self._freeze_old_tool_results(messages, session_id, _tc_id_to_name)

        yield {"type": "done", "text": "".join(text_parts), "tokens": llm_tokens, "usage": llm_usage}

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

            pre_len = len(messages)
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

            _tc_id_to_name: Dict[str, str] = {}
            for tc in resp.tool_calls:
                self.tool_calls_made += 1
                _tc_id_to_name[tc.id] = tc.name
                result = await self._tools.execute(
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    session_id=session_id,
                    tool_use_id=tc.id,
                    project_id=self._project_id,
                )
                # blocked / pending_approval → 转为错误消息
                if result.status in ("blocked", "pending_approval"):
                    result.output = f"[{result.status}] {result.error or '工具执行被阻止'}"

                # Dynamic circuit breaker: same (tool+args) consecutive failures
                call_key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                if result.status in ("error", "blocked", "timeout"):
                    if call_key == self._last_failed_call:
                        self._same_call_failures += 1
                    else:
                        self._last_failed_call = call_key
                        self._same_call_failures = 1
                else:
                    self._last_failed_call = ""
                    self._same_call_failures = 0

                if self._same_call_failures >= self._max_same_call_failures:
                    break
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.output,
                })

            # Stage 1: 缓存旧工具结果
            # 增量计 token（只算本轮新增的非 tool 消息）
            new_msgs = [m for m in messages[pre_len:] if m.get("role") != "tool"]
            total_tokens += sum(len(str(m.get("content", ""))) for m in new_msgs) // 3

            self._freeze_old_tool_results(messages, session_id, _tc_id_to_name)

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

    def _freeze_old_tool_results(self, messages: List[Dict], session_id: str, tc_id_to_name: Optional[Dict[str, str]] = None) -> None:
        """保持最近 KEEP_TOOL_RESULTS 个工具结果完整，更早的替换为 JSON 占位符。

        占位格式: {"toolname":"read_file","result_summary":"读取 main.py 前200行...","cache_path":"tool_cache/t3.json"}
        """
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool" and not m.get("content", "").startswith("{")
        ]
        keep = max(KEEP_TOOL_RESULTS, self._max_context_window // 8000)
        freeze_count = len(tool_indices) - keep
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
