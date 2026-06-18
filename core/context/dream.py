"""
core/context/dream.py
DreamPipeline — 记忆整合管道。

会话结束后异步运行，不阻塞对话：
  1. 读取所有 *.md 每日文件
  2. 1 次 LLM 调用 → 去重 + 分类 + 摘要
  3. 写入 MEMORY.md
  4. 可选 Prune → 删除 N 天前的旧每日文件

用法:
    pipeline = DreamPipeline(llm_invoker)
    await pipeline.run(memory_manager)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DREAM_SYSTEM_PROMPT = """You are a memory consolidation assistant. Your task is to read raw conversation memories from multiple daily files and produce a clean, deduplicated MEMORY.md index.

Rules:
1. Extract only the most important and persistent information (maximum 200 entries)
2. Deduplicate: if two entries say the same thing, keep only the clearest version
3. Categorize: group entries by topic (user preferences, project decisions, lessons learned, errors fixed, project progress)
4. Keep each entry concise: one line summary + optional key detail
5. Sort by importance: most critical/reusable information first
6. Output in Markdown with ## category headings

Format example:
## User Preferences
- Prefers Python 3.11+ with ruff for formatting
- Works in VS Code with vim keybindings

## Project Decisions
- 2026-06-15: Migrated from black to ruff (100ms faster)
- 2026-06-18: Added Hook system to core/ for pre/post execution hooks

Output ONLY the MEMORY.md content, no explanations."""


class DreamPipeline:
    """记忆整合管道（Claude Code Dreaming 模式）"""

    def __init__(self, llm_invoker, max_entries: int = 200, prune_days: int = 30):
        self._llm = llm_invoker
        self._max_entries = max_entries
        self._prune_days = prune_days
        self._last_run: Optional[float] = None

    async def run(self, memory_manager) -> str:
        """执行完整整合管道。返回生成的 MEMORY.md 内容。"""
        logger.info("DreamPipeline 开始整合记忆...")
        t0 = time.monotonic()

        # 1. Gather：收集所有每日文件
        raw_text = self._gather(memory_manager)
        if not raw_text.strip():
            logger.info("无新记忆需要整合")
            return ""

        # 2. Consolidate：LLM 总结
        memory_md = await self._consolidate(raw_text)

        # 3. Write：写入 MEMORY.md
        memory_manager.update_index(memory_md)

        # 4. Prune：清理旧文件
        self._prune(memory_manager)

        self._last_run = time.time()
        elapsed = time.monotonic() - t0
        logger.info("DreamPipeline 完成 (%.1fs, %d chars)", elapsed, len(memory_md))
        return memory_md

    # ====== 内部阶段 ======

    def _gather(self, memory_manager) -> str:
        """收集最近 N 天的每日文件内容"""
        parts: list[str] = []
        days = memory_manager.list_days(limit=60)  # 最近 60 天

        for day in days:
            text = memory_manager.read_day(day)
            if text.strip():
                parts.append(f"## {day}.md\n{text}")

        return "\n\n".join(parts)

    async def _consolidate(self, raw_text: str) -> str:
        """调用 LLM 整合记忆"""
        resp = await self._llm.invoke(
            messages=[
                {"role": "system", "content": DREAM_SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            session_id="dream-consolidation",
        )
        return resp.text.strip()

    def _prune(self, memory_manager) -> int:
        """删除 N 天前的每日文件。返回删除数。"""
        cutoff = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        deleted = 0

        for day in memory_manager.list_days(limit=999):
            if day >= cutoff:
                continue  # 保留最近 prune_days 天
            # 从日期字符串推算天数差
            try:
                dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age = (datetime.now(tz=timezone.utc) - dt).days
                if age > self._prune_days:
                    path = memory_manager._day_path(day)
                    if path.exists():
                        path.unlink()
                        deleted += 1
            except ValueError:
                continue

        if deleted:
            logger.info("Prune: 删除 %d 个旧记忆文件 (>%d 天)", deleted, self._prune_days)
        return deleted

    @property
    def last_run_timestamp(self) -> Optional[float]:
        return self._last_run

    def should_run(self) -> bool:
        """检查是否应触发（距上次整合 > 24h）"""
        if self._last_run is None:
            return True
        return time.time() - self._last_run > 86400
