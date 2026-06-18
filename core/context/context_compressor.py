"""
core/context/context_compressor.py
ContextCompressor — 渐进式压缩：FROZEN → TRIMMED → SUMMARIZED。

设计：
  Level 1 FROZEN:   工具结果 → 占位符 + 缓存文件（LLM 可通过 read_file 查看）
  Level 2 TRIMMED:  历史消息 → 首尾裁剪（动态比例 = 1 - pressure）
  Level 3 SUMMARIZED: 最旧消息 → LLM 摘要替换
  永不动：system_prompt / CHACHA.md / skills / 最近 5 轮

用法:
    compressor = ContextCompressor(llm_invoker, base_dir)
    ctx = compressor.compress(ctx, pressure=0.85)
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.models.context import (
    AssembledContext, BlockSource, CompressionLevel, ContextBlock,
)

logger = logging.getLogger(__name__)

_PROTECTED_ZONE = "protected"  # 永不压缩
_RECENT_KEEP = 5               # 最近 N 个 history 块保持原样
_CACHE_DIR = Path(".chacha_agent/tool_results")


class ContextCompressor:
    """渐进式上下文压缩器"""

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

        if level == CompressionLevel.NONE.value or level == "none":
            return ctx

        # Level 1: FROZEN — 冻结工具结果
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
                logger.warning("SUMMARIZED 需要 LLMInvoker，但未注入，退回 TRIMMED")
                blocks = self._trim_history(blocks, max(0.5, pressure))

        # 重建 AssembledContext
        return AssembledContext(
            meta=ctx.meta,
            blocks=blocks,
            needs_compression=ctx.needs_compression,
            recommended_level=ctx.recommended_level,
        )

    # ====== Level 1: FROZEN ======

    def _freeze_tool_results(
        self, blocks: list[ContextBlock], pressure: float, session_id: str,
    ) -> list[ContextBlock]:
        """工具结果 → 占位符 + 缓存文件"""
        result: list[ContextBlock] = []

        for b in blocks:
            if b.zone == _PROTECTED_ZONE:
                result.append(b)
                continue

            if b.source in (BlockSource.TOOL_RESULT, str(BlockSource.TOOL_RESULT)):
                # 缓存完整内容
                cache_path = self._cache_result(b.content, session_id)
                # 占位符
                placeholder = f"[工具结果已缓存: {cache_path}]\n[可通过 read_file 查看]"
                result.append(ContextBlock(
                    source=b.source, role=b.role,
                    content=placeholder,
                    zone=b.zone, priority=b.priority,
                    importance_score=b.importance_score,
                    token_count=len(placeholder) // 4,
                    original_token_count=b.token_count,
                    frozen_kept_lines=b.token_count,
                    frozen_total_lines=b.token_count,
                ))
            else:
                result.append(b)

        return result

    # ====== Level 2: TRIMMED ======

    def _trim_history(
        self, blocks: list[ContextBlock], pressure: float,
    ) -> list[ContextBlock]:
        """历史消息 → 首尾裁剪（动态比例）"""
        keep_ratio = max(0.1, 1.0 - pressure)
        history_blocks = [b for b in blocks if b.source in (BlockSource.HISTORY, str(BlockSource.HISTORY))]
        recent = history_blocks[-_RECENT_KEEP:] if len(history_blocks) > _RECENT_KEEP else history_blocks
        recent_ids = {id(b) for b in recent}

        result: list[ContextBlock] = []
        for b in blocks:
            if b.zone == _PROTECTED_ZONE or id(b) in recent_ids:
                result.append(b)
                continue

            if b.source in (BlockSource.HISTORY, str(BlockSource.HISTORY)):
                lines = b.content.split("\n")
                keep = max(1, int(len(lines) * keep_ratio))
                half = keep // 2
                trimmed = "\n".join(
                    lines[:half] +
                    [f"... [截断 {len(lines) - keep} 行] ..."] +
                    lines[-half:]
                )
                result.append(self._clone_block(b, trimmed))
            else:
                result.append(b)

        return result

    # ====== Level 3: SUMMARIZED ======

    async def _summarize_async(self, old_text: str) -> str:
        """调用 LLM 摘要旧对话"""
        if not self._llm:
            return old_text

        resp = await self._llm.invoke(
            messages=[
                {"role": "system", "content": "将以下对话历史总结为 2-3 句话的摘要，只提取关键决策和结果。"},
                {"role": "user", "content": old_text},
            ],
            session_id="compression-summary",
        )
        return resp.text.strip()

    def _summarize_history(
        self, blocks: list[ContextBlock], pressure: float, session_id: str,
    ) -> list[ContextBlock]:
        """历史消息 → LLM 摘要（同步包装异步调用）"""
        marked_old = self._mark_old_blocks(blocks)
        # 摘要逻辑委托给 orchestator 调用 _summarize_async
        return marked_old  # 返回标记后的 blocks，Orcherstrator 会调用 summarize_old_blocks

    def _mark_old_blocks(self, blocks: list[ContextBlock]) -> list[ContextBlock]:
        """标记需要摘要的旧 blocks"""
        history_blocks = [b for b in blocks if b.source in (BlockSource.HISTORY, str(BlockSource.HISTORY))]
        if len(history_blocks) <= _RECENT_KEEP:
            return blocks

        old = history_blocks[:-_RECENT_KEEP]
        recent_ids = {id(b) for b in history_blocks[-_RECENT_KEEP:]}

        result: list[ContextBlock] = []
        for b in blocks:
            if b.zone == _PROTECTED_ZONE or id(b) not in {id(o) for o in old}:
                result.append(b)
                continue
            # 替换为摘要占位（由 Orchestrator 实际执行 LLM 调用后填入）
            result.append(self._clone_block(b, f"[待LLM摘要: {len(b.content)} 字符]"))
        return result

    async def summarize_old_blocks(self, blocks: list[ContextBlock]) -> list[ContextBlock]:
        """异步：将标记为待摘要的块替换为 LLM 输出（由 Orchestrator 调用）"""
        if not self._llm:
            return blocks

        result: list[ContextBlock] = []
        for b in blocks:
            if b.content.startswith("[待LLM摘要:"):
                old_blocks = []  # 需要从原始 ctx 恢复，此处简化
                summary = await self._summarize_async(b.content)
                result.append(self._clone_block(b, f"[摘要] {summary}"))
            else:
                result.append(b)
        return result

    # ====== 工具 ======

    def _cache_result(self, content: str, session_id: str) -> Path:
        """缓存工具结果到文件"""
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self._cache_dir / f"{session_id}_{ts}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _clone_block(b: ContextBlock, new_content: str) -> ContextBlock:
        return ContextBlock(
            source=b.source, role=b.role, content=new_content,
            zone=b.zone, priority=b.priority,
            importance_score=b.importance_score,
            token_count=len(new_content) // 4,
            original_token_count=b.original_token_count,
            frozen_kept_lines=b.frozen_kept_lines,
            frozen_total_lines=b.frozen_total_lines,
        )
