"""
core/context/dream.py
DreamPipeline — 记忆整合管道（v2.0）。

会话结束后异步运行，不阻塞对话：
  1. 收集最近 7 天所有 session 每日文件 + 项目级每日文件
  2. 读取当前 MEMORY.md（旧索引）+ CHACHA_MEMORY.md（旧永久记忆）
  3. 1 次 LLM 调用 → 同时输出更新后的 MEMORY.md 和 CHACHA_MEMORY.md
  4. 写入 MEMORY.md + CHACHA_MEMORY.md
  5. Prune → 删除超过 7 天的旧每日文件

触发条件（二选一，先到先触发）:
  - 累计完成 10 次会话
  - 距上次运行超过 24 小时

用法:
    pipeline = DreamPipeline(llm_invoker)
    pipeline.record_session()     # 每次会话结束调用
    if pipeline.should_run():
        await pipeline.run(memory_manager)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 触发配置
_DREAM_SESSION_COUNT = 10    # 每 N 次会话触发
_DREAM_HOURS = 24            # 距上次运行超 N 小时触发

DREAM_SYSTEM_PROMPT = """You are a memory consolidation assistant. Your task is to read raw conversation memories from multiple daily files and produce TWO outputs:
1. An updated MEMORY.md (lightweight index with summaries, source paths, and timestamps)
2. An updated CHACHA_MEMORY.md (permanent project memory, maximum 100 entries, only the most critical/persistent info)

## Rules for MEMORY.md:
1. Read ALL provided daily memory entries and the OLD MEMORY.md
2. Extract important information, deduplicate, and categorize
3. Each entry should include: summary + source file path + summary timestamp
4. Categories: User Preferences, Project Decisions, Lessons Learned, Errors Fixed, Project Progress
5. Keep entries concise, sort by importance
6. Format as Markdown with ## category headings

## Rules for CHACHA_MEMORY.md:
1. This is PERMANENT project memory — only upgrade truly persistent information here
2. Judge which memories deserve permanent status (user preferences, critical project decisions, key learnings)
3. Maximum 100 entries total
4. Update/overwrite old entries when they become stale
5. Format as Markdown with ## category headings

## Output Format:
Output exactly in this format with these exact separators:

===MEMORY_MD===
## Memory Index (autoDream generated at {timestamp})

### User Preferences
- Summary line here
  Source: sessions/{session_id}/{date}.md
  Updated: {timestamp}

### Project Decisions
...

===CHACHA_MEMORY_MD===
## Permanent Project Memory (autoDream updated at {timestamp})

### Critical Preferences
- Entry summary here
...

### Key Decisions
...

Output ONLY the content between the markers, no additional explanations."""


class DreamPipeline:
    """记忆整合管道（Claude Code Dreaming 模式 v2.0）"""

    def __init__(
        self, llm_invoker,
        max_entries: int = 200,
        prune_days: int = 7,
        session_trigger: int = _DREAM_SESSION_COUNT,
        hours_trigger: int = _DREAM_HOURS,
    ):
        self._llm = llm_invoker
        self._max_entries = max_entries
        self._prune_days = prune_days
        self._session_trigger = session_trigger
        self._hours_trigger = hours_trigger
        self._last_run: Optional[float] = None
        self._session_count = 0

    async def run(self, memory_manager) -> tuple[str, str]:
        """执行完整整合管道。返回 (MEMORY.md 内容, CHACHA_MEMORY.md 内容)。"""
        logger.info("DreamPipeline 开始整合记忆...")
        t0 = time.monotonic()

        # 1. Gather：收集所有数据
        old_memory = memory_manager.read()
        old_permanent = memory_manager.read_permanent_memory()
        raw_text = self._gather(memory_manager)
        if not raw_text.strip() and not old_memory.strip():
            logger.info("无新记忆需要整合")
            return "", ""

        # 2. Consolidate：LLM 同时输出 MEMORY.md + CHACHA_MEMORY.md
        memory_md, permanent_md = await self._consolidate(
            raw_text, old_memory, old_permanent,
        )

        # 3. Write
        if memory_md.strip():
            memory_manager.update_index(memory_md)
        if permanent_md.strip():
            memory_manager.write_permanent_memory(permanent_md)

        # 4. Prune：清理超过 7 天的旧文件
        memory_manager.prune_old_days()

        self._last_run = time.time()
        self._session_count = 0  # 重置计数

        elapsed = time.monotonic() - t0
        logger.info(
            "DreamPipeline 完成 (%.1fs, MEMORY.md=%d chars, CHACHA_MEMORY.md=%d chars)",
            elapsed, len(memory_md), len(permanent_md),
        )
        return memory_md, permanent_md

    # ====== 触发判断 ======

    def record_session(self) -> None:
        """每次会话结束时调用，增加计数。"""
        self._session_count += 1

    def should_run(self) -> bool:
        """检查是否应触发整合:
        - 累计 10 次会话
        - 或距上次整合 > 24 小时
        """
        if self._last_run is None:
            # 首次：等待积累 10 次会话
            return self._session_count >= self._session_trigger

        session_triggered = self._session_count >= self._session_trigger
        time_triggered = time.time() - self._last_run > self._hours_trigger * 3600
        return session_triggered or time_triggered

    @property
    def last_run_timestamp(self) -> Optional[float]:
        return self._last_run

    @property
    def session_count(self) -> int:
        return self._session_count

    # ====== 内部阶段 ======

    def _gather(self, memory_manager) -> str:
        """收集所有可用的记忆内容。"""
        parts: list[str] = []

        # 项目级每日文件
        project_days = memory_manager.list_days(limit=self._prune_days)
        for day in project_days:
            text = memory_manager.read_day(day)
            if text.strip():
                parts.append(f"## project/{day}.md\n{text}")

        # 所有 session 的每日文件
        session_days = memory_manager.list_all_session_days(limit_days=self._prune_days)
        for sid, files in session_days.items():
            for f in files:
                text = self._read(f)
                if text.strip():
                    parts.append(f"## sessions/{sid}/{f.name}\n{text}")

        return "\n\n".join(parts)

    async def _consolidate(
        self, raw_text: str, old_memory: str, old_permanent: str,
    ) -> tuple[str, str]:
        """调用 LLM 整合记忆，解析出 MEMORY.md 和 CHACHA_MEMORY.md。"""
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        system_prompt = DREAM_SYSTEM_PROMPT.replace("{timestamp}", ts)

        user_parts = []
        if old_memory.strip():
            user_parts.append(f"## OLD MEMORY.md\n```\n{old_memory}\n```")
        if old_permanent.strip():
            user_parts.append(f"## OLD CHACHA_MEMORY.md\n```\n{old_permanent}\n```")
        if raw_text.strip():
            user_parts.append(f"## NEW DAILY MEMORIES\n```\n{raw_text}\n```")

        user_content = "\n\n".join(user_parts) if user_parts else "No memories to consolidate."

        resp = await self._llm.invoke(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            session_id="dream-consolidation",
        )

        return self._parse_llm_output(resp.text.strip())

    def _parse_llm_output(self, text: str) -> tuple[str, str]:
        """解析 LLM 输出，分离 MEMORY_MD 和 CHACHA_MEMORY_MD。"""
        memory_md = ""
        permanent_md = ""

        if "===MEMORY_MD===" in text and "===CHACHA_MEMORY_MD===" in text:
            # 标准格式：两个标记都存在
            parts = text.split("===CHACHA_MEMORY_MD===", 1)
            memory_md = parts[0].replace("===MEMORY_MD===", "").strip()
            permanent_md = parts[1].strip() if len(parts) > 1 else ""
        elif "===CHACHA_MEMORY_MD===" in text:
            # 只有永久记忆
            permanent_md = text.replace("===CHACHA_MEMORY_MD===", "").strip()
        elif "===MEMORY_MD===" in text:
            # 只有索引
            memory_md = text.replace("===MEMORY_MD===", "").strip()
        else:
            # 按 --- 分割的旧格式兼容
            parts = text.split("\n---\n", 1)
            if len(parts) == 2:
                memory_md = parts[0].strip()
                permanent_md = parts[1].strip()
            else:
                memory_md = text

        return memory_md, permanent_md

    def _prune(self, memory_manager) -> int:
        """删除旧文件（委托给 MemoryManager）。"""
        return memory_manager.prune_old_days()

    @staticmethod
    def _read(path) -> str:
        from pathlib import Path
        return path.read_text(encoding="utf-8").strip() if isinstance(path, Path) and path.exists() else ""
