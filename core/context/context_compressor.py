"""
core/context/context_compressor.py
ContextCompressor — 渐进式压缩：FROZEN → TRIMMED → SUMMARIZED。

v2.0 两阶段工具结果缓存:
  Stage 1 (Dispatcher, 宽松): >10 个工具结果 → JSON 占位符 + 缓存文件
  Stage 2 (Compressor FROZEN, 激进): JSON key 最小化 {"t":"x","s":"x","p":"x"}，
      完整结果截断到 150 字符摘要

设计:
  Level 1 FROZEN:   工具结果 → 激进占位 + 缓存文件
  Level 2 TRIMMED:  历史消息 → 首尾裁剪
  Level 3 SUMMARIZED: 最旧消息 → LLM 摘要
  永不动：protected zone 所有块 + 最近 5 轮历史

用法:
    compressor = ContextCompressor(llm_invoker, base_dir)
    ctx = compressor.compress(ctx, pressure=0.85)
"""

import json
import logging
from datetime import timedelta,  datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.models.context import (
    AssembledContext, BlockSource, CompressionLevel, ContextBlock,
)

logger = logging.getLogger(__name__)

_PROTECTED_ZONE = "protected"
_RECENT_KEEP = 5
_CACHE_DIR = Path(".chacha_agent/tool_results")


class ContextCompressor:
    """渐进式上下文压缩器（v2.0 激进 FROZEN）"""

    def __init__(self, llm_invoker: Optional[Any] = None, base_dir: Optional[Path] = None):
        self._llm = llm_invoker
        self._cache_dir = base_dir or _CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def compress(
        self,
        ctx: AssembledContext,
        pressure: float = 0.8,
        session_id: str = "",
    ) -> AssembledContext:
        """根据 pressure 渐进压缩。返回压缩后的 AssembledContext。"""
        level = ctx.recommended_level
        blocks = list(ctx.blocks)

        if level in (CompressionLevel.NONE.value, "none"):
            return ctx

        # Level 1: FROZEN — 激进工具结果冻结
        if level in ("frozen", "trimmed", "summarized", "consolidated"):
            blocks = self._freeze_tool_results(blocks, pressure, session_id)

        # Level 2: TRIMMED — 裁剪历史消息
        if level in ("trimmed", "summarized", "consolidated"):
            blocks = self._trim_history(blocks, pressure)

        # Level 3: SUMMARIZED — LLM 摘要
        if level in ("summarized", "consolidated"):
            if self._llm:
                blocks = self._summarize_history(blocks, pressure, session_id)
            else:
                logger.warning("SUMMARIZED 需要 LLMInvoker，退回 TRIMMED")
                blocks = self._trim_history(blocks, max(0.5, pressure))

        return AssembledContext(
            meta=ctx.meta,
            blocks=blocks,
            needs_compression=ctx.needs_compression,
            recommended_level=ctx.recommended_level,
        )

    # ====== Level 1: FROZEN（激进版） ======

    def _freeze_tool_results(
        self, blocks: list[ContextBlock], pressure: float, session_id: str,
    ) -> list[ContextBlock]:
        """v2.0 激进 FROZEN:
        - 对已是 JSON 占位符的 → 二次压缩为最小化格式 {"t":"x","s":"x","p":"x"}
        - 对完整工具结果 → 截断到 150 字符摘要 + 缓存
        - protected zone 跳过
        """
        result: list[ContextBlock] = []
        for b in blocks:
            if b.zone == _PROTECTED_ZONE:
                result.append(b)
                continue

            if b.source in (BlockSource.TOOL_RESULT, str(BlockSource.TOOL_RESULT)):
                content = b.content
                original = content

                # 已经是 Stage 1 占位符 → Stage 2 二次压缩
                if content.startswith("{") and '"toolname"' in content:
                    frozen = self._compress_json_placeholder(content, session_id)
                else:
                    # 完整结果 → 激进截断
                    frozen = self._freeze_full_result(content, session_id)

                result.append(self._clone_block(b, frozen))
            else:
                result.append(b)

        return result

    def _compress_json_placeholder(self, content: str, session_id: str) -> str:
        """Stage 2: 将 Stage 1 的 JSON 占位符压缩为最小化格式。

        Input:  {"toolname": "read_file", "result_summary": "读取 main.py...", "cache_path": "tool_cache/t3.json"}
        Output: {"t":"read_file","s":"读取 main.py...","p":"tool_cache/t3.json"}
        """
        try:
            data = json.loads(content)
            mini = {
                "t": data.get("toolname", "?"),
                "s": data.get("result_summary", "")[:80],  # 摘要截断到 80 字符
                "p": data.get("cache_path", ""),
            }
            return json.dumps(mini, ensure_ascii=False)
        except (json.JSONDecodeError, KeyError):
            return content[:150] + "..." if len(content) > 150 else content

    def _freeze_full_result(self, content: str, session_id: str) -> str:
        """完整工具结果 → 激进截断 + 缓存。"""
        summary = content[:150].replace("\n", " ").strip()
        if len(content) > 150:
            summary += "..."

        cache_path = self._cache_result(content, session_id)
        return (
            f"[工具结果已缓存: {cache_path.name}]\n"
            f"摘要: {summary}"
        )

    # ====== Level 2: TRIMMED ======

    def _trim_history(
        self, blocks: list[ContextBlock], pressure: float,
    ) -> list[ContextBlock]:
        """裁剪旧历史消息。最近 _RECENT_KEEP 个保持完整。"""
        history_blocks = [(i, b) for i, b in enumerate(blocks)
                          if b.source in (BlockSource.HISTORY, str(BlockSource.HISTORY))
                          and b.zone != _PROTECTED_ZONE]

        if len(history_blocks) <= _RECENT_KEEP:
            return blocks

        # 需要裁剪的旧块数量
        to_trim = history_blocks[:-_RECENT_KEEP]
        result = list(blocks)

        for idx, b in to_trim:
            content = b.content
            keep_chars = max(100, int(len(content) * max(0.1, 1 - pressure)))
            head = content[:keep_chars // 2]
            tail = content[-keep_chars // 2:] if len(content) > keep_chars else ""
            new_content = f"{head}\n...[截断]...\n{tail}" if tail else f"{head}\n...[截断]..."

            result[idx] = self._clone_block(b, new_content)

        return result

    # ====== Level 3: SUMMARIZED ======

    async def _summarize_async(self, old_content: str) -> str:
        if not self._llm:
            return old_content[:200] + "..."
        resp = await self._llm.invoke(
            messages=[
                {"role": "system", "content": "Summarize this conversation in 2-3 sentences in the original language."},
                {"role": "user", "content": old_content},
            ],
            session_id="compression-summary",
        )
        return resp.text.strip()

    def _summarize_history(
        self, blocks: list[ContextBlock], pressure: float, session_id: str,
    ) -> list[ContextBlock]:
        mark_old = self._mark_old_blocks(blocks)
        return mark_old

    def _mark_old_blocks(self, blocks: list[ContextBlock]) -> list[ContextBlock]:
        history_blocks = [(i, b) for i, b in enumerate(blocks)
                          if b.source in (BlockSource.HISTORY, str(BlockSource.HISTORY))]
        if len(history_blocks) <= _RECENT_KEEP:
            return blocks

        old_indices = {i for i, _ in history_blocks[:-_RECENT_KEEP]}
        result: list[ContextBlock] = []
        for i, b in enumerate(blocks):
            if b.zone == _PROTECTED_ZONE or i not in old_indices:
                result.append(b)
            else:
                result.append(self._clone_block(b, f"[待LLM摘要: {len(b.content)} 字符]"))
        return result

    async def summarize_old_blocks(self, blocks: list[ContextBlock]) -> list[ContextBlock]:
        if not self._llm:
            return blocks
        result: list[ContextBlock] = []
        for b in blocks:
            if b.content.startswith("[待LLM摘要:"):
                summary = await self._summarize_async(b.content)
                result.append(self._clone_block(b, f"[摘要] {summary}"))
            else:
                result.append(b)
        return result

    # ====== 工具 ======

    def _cache_result(self, content: str, session_id: str) -> Path:
        ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
        path = self._cache_dir / f"{session_id}_{ts}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    # ====== 消息估算 & 三层压缩（供 CLI 调用） ======

    @staticmethod
    def estimate_tokens(messages: list) -> int:
        """估算消息列表的 token 数（中英混合 ≈ 2.5 char/token）。"""
        import json
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        return int(total / 2.5)

    @staticmethod
    def auto_compact(
        messages: list,
        context_window: int,
        *,
        llm=None,
        trigger_ratio: float = 0.7,
        warn_ratio: float = 0.9,
        frozen_keep: int = 5,
        trim_head: int = 5,
        trim_tail: int = 12,
        summary_head: int = 3,
        summary_tail: int = 8,
    ) -> tuple[list, str]:
        """全自动压缩：判断 → 执行 → (压缩后消息, 理由)。不达阈值直接返回 (原消息, "")。"""
        est = ContextCompressor.estimate_tokens(messages)
        pct = est / context_window

        if pct >= warn_ratio:
            reason = f"⚠ {est//1000}K token ({int(pct*100)}% 窗口)"
        elif pct >= trigger_ratio:
            reason = f"压缩 {int(pct*100)}% 窗口"
        else:
            return messages, ""

        compressed = ContextCompressor.smart_compact_messages(
            messages, context_window, llm=llm,
            frozen_keep=frozen_keep, trim_head=trim_head, trim_tail=trim_tail,
            summary_head=summary_head, summary_tail=summary_tail,
        )
        return compressed, reason

    @staticmethod
    def smart_compact_messages(
        messages: list,
        context_window: int,
        llm=None,
        *,
        trigger_ratio: float = 0.7,
        frozen_keep: int = 5,
        trim_head: int = 5,
        trim_tail: int = 12,
        summary_head: int = 3,
        summary_tail: int = 8,
    ) -> list:
        """三层渐进压缩到 < trigger_ratio 窗口。
        
        参数可通过 ContextConfig 传入（config.toml context.xxx 配置）。
        """
        msgs = list(messages)
        target = int(context_window * trigger_ratio)

        while ContextCompressor.estimate_tokens(msgs) > target and len(msgs) > 5:
            # ── Level 1: FROZEN ──
            tool_indices = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
            if tool_indices and len(tool_indices) > frozen_keep:
                freeze_idx = tool_indices[0]
                original = msgs[freeze_idx]
                preview = str(original.get("content", ""))[:80].split("\n")[0]
                msgs[freeze_idx] = dict(
                    original,
                    content=f"[{preview}]...\n[工具结果已缓存，占位]",
                )
                continue

            # ── Level 2: TRIMMED ──
            n = len(msgs)
            trim_cutoff = 1 + trim_head + trim_tail  # system + head + tail
            if n > trim_cutoff:
                head = msgs[:1 + trim_head]   # system + head 条
                tail = msgs[-trim_tail:]       # tail 条
                head[-1] = dict(head[-1], content="……(中间对话已裁剪)……")
                msgs = head + tail
                continue

            # ── Level 3: SUMMARIZED ──
            summary_cutoff = 1 + summary_head + summary_tail
            if n > summary_cutoff:
                head = msgs[:1 + summary_head]
                middle = msgs[1 + summary_head:-summary_tail]
                tail = msgs[-summary_tail:]

                old_content = ""
                for m in middle:
                    role = m.get("role", "")
                    content = str(m.get("content", ""))[:150]
                    if content:
                        old_content += f"[{role}] {content}\n"

                if llm and old_content.strip():
                    import asyncio
                    summary = asyncio.run(
                        ContextCompressor._summarize_block(llm, old_content)
                    )
                    msgs = head + [
                        {"role": "user", "content": f"[历史摘要] {summary}"},
                    ] + tail
                else:
                    head[-1] = dict(head[-1], content="……(中间对话已裁剪)……")
                    msgs = head + tail
                continue

            break

        return msgs

    @staticmethod
    async def _summarize_block(llm, text: str) -> str:
        """LLM 摘要一段对话。"""
        try:
            resp = await llm.invoke(
                messages=[
                    {"role": "system",
                     "content": "将对话历史压缩为 1-3 句摘要，保留关键决策、代码变更和用户偏好。"},
                    {"role": "user", "content": text},
                ],
                session_id="compact-summary",
            )
            summary = resp.text.strip()[:200]
            return summary if summary else text[:80]
        except Exception:
            return text[:80]

    @staticmethod
    def _clone_block(b: ContextBlock, new_content: str) -> ContextBlock:
        return ContextBlock(
            source=b.source, role=b.role, content=new_content,
            zone=b.zone, priority=b.priority,
            importance_score=b.importance_score,
            token_count=len(new_content) // 4,
            original_token_count=b.original_token_count or b.token_count,
            frozen_kept_lines=b.frozen_kept_lines,
            frozen_total_lines=b.frozen_total_lines,
        )
