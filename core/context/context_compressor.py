"""
core/context/context_compressor.py
ContextCompressor — 渐进式压缩：TRIMMED → SUMMARIZED → CONSOLIDATED。

v3.2: 冻结逻辑已迁移至 dispatcher（每轮实时执行），compressor 不再包含 FROZEN 阶段。
compress() 为唯一入口，auto_compact() 委托到 compress()。

压缩级别依据 pressure（Token 窗口利用率）：
  pressure < 0.70  → 不压缩（dispatcher 已实时冻结旧工具结果）
  pressure < 0.85  → TRIMMED（裁剪中间，保留头尾）
  pressure < 0.95  → SUMMARIZED（LLM 摘要中间部分）
  pressure >= 0.95 → CONSOLIDATED（多轮摘要再压缩）

用法:
    compressor = ContextCompressor(llm_invoker=llm)
    ctx = await compressor.compress(ctx, pressure=0.85, session_id="s1")

    # 便捷入口（从消息列表）
    msgs, reason = await ContextCompressor.auto_compact(messages, context_window, ...)
"""

import asyncio
import json
import logging
from typing import Any, List, Optional

from core.models.context import (
    AssembledContext,
    BlockSource,
    CompressionLevel,
    ContextBlock,
)

logger = logging.getLogger(__name__)

_PROTECTED_ZONE = "protected"
_DEFAULT_PRESERVE_RECENT = 5
# 默认摘要模型（可通过 auto_compact() 的 summary_model 参数覆盖）
_SUMMARY_MODEL = "deepseek-v4-flash"


class ContextCompressor:
    """渐进式上下文压缩器（v3.1 统一引擎）。"""

    def __init__(
        self,
        llm_invoker: Optional[Any] = None,
        *,
        summary_model: str = _SUMMARY_MODEL,
        preserve_recent: int = _DEFAULT_PRESERVE_RECENT,
        context_window: int = 1_048_576,
        target_ratio: float = 0.85,
    ):
        self._llm = llm_invoker
        self._summary_model = summary_model
        self._preserve_recent = preserve_recent
        self._context_window = context_window
        self._target_ratio = target_ratio

    # ====== 主入口 ======

    async def compress(
        self,
        ctx: AssembledContext,
        pressure: float = 0.8,
        session_id: str = "",
        *,
        force: bool = False,
    ) -> AssembledContext:
        """渐进式压缩。pressure 越大压缩越激进。

        pressure < 0.70  → 不压缩（dispatcher 已实时冻结旧工具结果）
        pressure < 0.85  → TRIMMED（裁剪中间）
        pressure < 0.95  → SUMMARIZED（LLM 摘要）
        pressure >= 0.95 → CONSOLIDATED（摘要合并）

        force=True 时跳过 pressure < 0.70 的早退，至少执行 TRIMMED。
        """
        protected = [b for b in ctx.blocks if b.zone == _PROTECTED_ZONE]
        dynamic = [b for b in ctx.blocks if b.zone != _PROTECTED_ZONE]

        if not dynamic:
            return ctx

        # pressure < 0.70: 不压缩（dispatcher 已实时冻结旧工具结果）
        if pressure < 0.70 and not force:
            return ctx

        # Level 1: TRIMMED — 裁剪中间，保留头尾（head=2 + tail=preserve_recent）
        dynamic = self._trim_middle(dynamic)

        if pressure < 0.85:
            return self._rebuild(ctx, protected, dynamic)

        # Level 3: SUMMARIZED — LLM 摘要中间部分
        dynamic = await self._summarize_core(dynamic, session_id)

        if pressure < 0.95:
            return self._rebuild(ctx, protected, dynamic)

        # Level 4: CONSOLIDATED — 摘要块再次压缩
        dynamic = await self._consolidate(dynamic, session_id)
        return self._rebuild(ctx, protected, dynamic)

    # ====== Level 1: TRIMMED ======

    def _trim_middle(self, blocks: List[ContextBlock]) -> List[ContextBlock]:
        """裁剪中间消息，保留 HEAD(前2) + TAIL(最近 preserve_recent)。"""
        head_count = 2
        tail_count = self._preserve_recent

        if len(blocks) <= head_count + tail_count:
            return blocks

        head = blocks[:head_count]
        tail = blocks[-tail_count:]
        marker = ContextBlock(
            source=BlockSource.HISTORY,
            role="system",
            content=f"……(中间 {len(blocks) - head_count - tail_count} 条消息已裁剪)……",
            zone="dynamic",
            priority=50,
            compression_level=CompressionLevel.TRIMMED,
            token_count=10,
        )
        return head + [marker] + tail

    # ====== Level 3: SUMMARIZED ======

    async def _summarize_core(self, blocks: List[ContextBlock], session_id: str) -> List[ContextBlock]:
        """HEAD + LLM摘要(CORE) + TAIL。"""
        head_count = 2
        tail_count = self._preserve_recent

        if len(blocks) <= head_count + tail_count:
            return blocks

        head = blocks[:head_count]
        tail = blocks[-tail_count:]
        core = blocks[head_count:-tail_count]

        if not core:
            return blocks

        # 构建 CORE 文本
        core_text_parts = []
        for b in core:
            role = b.role or "unknown"
            content = (b.content or "")[:300]
            if content.strip():
                core_text_parts.append(f"[{role}] {content}")
        core_text = "\n".join(core_text_parts)

        # LLM 摘要（带容错降级）
        summary = await self._llm_summarize(core_text, session_id)

        summary_block = ContextBlock(
            source=BlockSource.HISTORY,
            role="system",
            content=f"[历史上下文摘要]\n{summary}",
            zone="dynamic",
            priority=50,
            compression_level=CompressionLevel.SUMMARIZED,
            token_count=len(summary) // 3 if summary else 10,
        )
        return head + [summary_block] + tail

    async def _llm_summarize(self, text: str, session_id: str) -> str:
        """调用 LLM 摘要，失败时退回纯规则。"""
        if not self._llm or not text.strip():
            return self._rule_summarize(text)

        try:
            resp = await asyncio.wait_for(
                self._llm.invoke(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "将以下对话历史压缩为 2-5 句摘要。"
                                "保留：用户意图、关键决策、代码变更、工具调用及结果核心内容。"
                                "使用原文语言。"
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    session_id=f"{session_id}-compression",
                ),
                timeout=10.0,
            )
            summary = resp.text.strip()[:500]
            return summary if summary else self._rule_summarize(text)
        except asyncio.TimeoutError:
            logger.warning("LLM 摘要超时，退回规则摘要")
            return self._rule_summarize(text)
        except Exception as e:
            logger.warning("LLM 摘要失败: %s，退回规则摘要", e)
            return self._rule_summarize(text)

    @staticmethod
    def _rule_summarize(text: str) -> str:
        """纯规则摘要：取每行前 60 字符，最多 10 行。"""
        lines = text.strip().split("\n")[:10]
        return "\n".join(line[:60] for line in lines if line.strip())

    # ====== Level 4: CONSOLIDATED ======

    async def _consolidate(self, blocks: List[ContextBlock], session_id: str) -> List[ContextBlock]:
        """多轮摘要再压缩：将所有 SUMMARIZED 块合并为一条 LLM 摘要。"""
        summary_blocks = [b for b in blocks if b.compression_level in (CompressionLevel.SUMMARIZED, "summarized")]
        other_blocks = [b for b in blocks if b.compression_level not in (CompressionLevel.SUMMARIZED, "summarized")]

        if len(summary_blocks) <= 1:
            # 单条摘要降级为更激进裁剪
            return self._trim_middle(blocks)

        # 合并所有摘要块
        combined = "\n---\n".join(b.content or "" for b in summary_blocks)
        new_summary = await self._llm_summarize(combined, session_id)

        merged = ContextBlock(
            source=BlockSource.HISTORY,
            role="system",
            content=f"[合并历史摘要]\n{new_summary}",
            zone="dynamic",
            priority=50,
            compression_level=CompressionLevel.CONSOLIDATED,
            token_count=len(new_summary) // 3,
        )
        return other_blocks + [merged]

    # ====== 重建 & 工具 ======

    def _rebuild(
        self,
        ctx: AssembledContext,
        protected: List[ContextBlock],
        dynamic: List[ContextBlock],
    ) -> AssembledContext:
        """重组 AssembledContext，重新分配 priority。


        使用 model_copy 而非直接赋值，因为 ContextBlock 是 frozen Pydantic model。
        """
        new_blocks: List[ContextBlock] = []
        for i, b in enumerate(protected + dynamic):
            if b.zone != _PROTECTED_ZONE and b.priority != 100 + i:
                new_blocks.append(b.model_copy(update={"priority": 100 + i}))
            else:
                new_blocks.append(b)

        total_tokens = sum(b.token_count or 0 for b in new_blocks)
        return AssembledContext(
            meta=ctx.meta,
            blocks=new_blocks,
            needs_compression=total_tokens > int(self._target_ratio * self._context_window),
            recommended_level=ctx.recommended_level,
        )

    @staticmethod
    def _clone_block(b: ContextBlock, new_content: str) -> ContextBlock:
        return ContextBlock(
            source=b.source,
            role=b.role,
            content=new_content,
            zone=b.zone,
            priority=b.priority,
            importance_score=b.importance_score,
            token_count=len(new_content) // 4,
            original_token_count=b.original_token_count or b.token_count,
            frozen_kept_lines=b.frozen_kept_lines,
            frozen_total_lines=b.frozen_total_lines,
        )

    # ====== 消息估算 & 便捷入口 ======

    @staticmethod
    def estimate_tokens(messages: list) -> int:
        """估算消息列表的 token 数（中英混合 ≈ 2.5 char/token）。"""
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        return int(total / 2.5)

    @staticmethod
    async def auto_compact(
        messages: list,
        context_window: int,
        *,
        llm=None,
        trigger_ratio: float = 0.7,
        warn_ratio: float = 0.9,
        trim_head: int = 5,
        trim_tail: int = 12,
        summary_head: int = 3,
        summary_tail: int = 8,
        force: bool = False,
        summary_model: Optional[str] = None,
        **kwargs,
    ) -> tuple:
        """全自动压缩：判断 → dict→AssembledContext→compress()→dict。

        返回 (压缩后消息列表, 原因字符串)。不达阈值返回 (原消息, "")。
        已废弃的内部参数（trim_head/summary_head/summary_tail）保留兼容。
        summary_model: 摘要用模型，None 则使用默认值 (deepseek-v4-flash)。
        """
        est = ContextCompressor.estimate_tokens(messages)
        pct = est / context_window if context_window else 0

        if pct >= warn_ratio:
            reason = f"⚠ {est // 1000}K token ({int(pct * 100)}% 窗口)"
        elif pct >= trigger_ratio:
            reason = f"压缩 {int(pct * 100)}% 窗口"
        elif not force:
            return messages, ""

        # messages → AssembledContext（简化版，无 ContextManager）
        ctx = ContextCompressor._messages_to_ctx(messages)

        # compress
        compressor_kwargs = dict(
            llm_invoker=llm,
            context_window=context_window,
            preserve_recent=trim_tail,
        )
        if summary_model is not None:
            compressor_kwargs["summary_model"] = summary_model
        compressor = ContextCompressor(**compressor_kwargs)
        ctx = await compressor.compress(ctx, pressure=pct, session_id="auto-compact", force=force)

        # AssembledContext → messages
        new_messages = ContextCompressor._ctx_to_messages(ctx)
        return new_messages, reason

    @staticmethod
    def _messages_to_ctx(messages: list) -> AssembledContext:
        """从消息 dict 列表构建简化 AssembledContext（供 auto_compact 使用）。"""
        blocks: List[ContextBlock] = []

        for i, m in enumerate(messages):
            role = m.get("role", "")
            content = str(m.get("content", "") or "")
            tool_calls = m.get("tool_calls")

            if role == "system":
                blocks.append(
                    ContextBlock(
                        source=BlockSource.SYSTEM_PROMPT,
                        role="system",
                        content=content,
                        zone="protected",
                        priority=i,
                        token_count=len(content) // 3,
                    )
                )
            elif role == "tool":
                blocks.append(
                    ContextBlock(
                        source=BlockSource.TOOL_RESULT,
                        role="tool",
                        content=content,
                        zone="dynamic",
                        priority=100 + i,
                        token_count=len(content) // 3,
                    )
                )
            elif role == "user":
                blocks.append(
                    ContextBlock(
                        source=BlockSource.HISTORY,
                        role="user",
                        content=content,
                        zone="dynamic",
                        priority=100 + i,
                        token_count=len(content) // 3,
                    )
                )
            elif role == "assistant":
                blocks.append(
                    ContextBlock(
                        source=BlockSource.HISTORY,
                        role="assistant",
                        content=content,
                        zone="dynamic",
                        priority=100 + i,
                        token_count=len(content) // 3,
                    )
                )
                if tool_calls:
                    if not hasattr(blocks[-1], "_extra") or blocks[-1]._extra is None:
                        blocks[-1]._extra = {}
                    blocks[-1]._extra["tool_calls"] = tool_calls

        total_tokens = sum(b.token_count or 0 for b in blocks)
        from core.models.context import ContextAssemblyMeta

        meta = ContextAssemblyMeta(total_tokens=total_tokens, trigger="auto_compact")
        return AssembledContext(meta=meta, blocks=blocks, needs_compression=True)

    @staticmethod
    def _ctx_to_messages(ctx: AssembledContext) -> list:
        """从 AssembledContext 构建消息 dict 列表。"""
        from core.context_manager import ContextManager

        return ContextManager.blocks_to_messages(ctx)
