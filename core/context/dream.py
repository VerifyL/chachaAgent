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
import asyncio
logger = logging.getLogger(__name__)

# 触发配置
_DREAM_SESSION_COUNT = 1    # 每 N 次会话触发
_DREAM_HOURS = 24            # 距上次运行超 N 小时触发

DREAM_SYSTEM_PROMPT = """You are a memory consolidation assistant. Your task is to read raw conversation memories and produce TWO outputs:
1. An updated MEMORY.md (lightweight index — fully rebuilt each time)
2. An updated CHACHA_MEMORY.md (incrementally merged permanent memory)


## Rules for MEMORY.md:
1. Read ALL provided daily memories, topics, and the OLD MEMORY.md
2. Extract important information, deduplicate, and categorize
3. Each entry must include: a one-line summary + source file path + timestamp
4. Categories:
   - User Preferences (tools, style, workflow habits)
   - Project Decisions (architecture, tech stack, conventions)
   - Lessons Learned (mistakes, pitfalls, best practices discovered)
   - Errors Fixed (bugs resolved, root causes, solutions)
   - Project Progress (milestones, completed features, current status)
5. Keep entries concise — one to two lines per entry maximum
6. Sort by importance within each category
7. Format as Markdown with ## category headings


## Rules for CHACHA_MEMORY.md (Incremental Merge):

### Step 1 — Review old entries
Each entry in OLD CHACHA_MEMORY.md has a stable [id:xxx] tag.
For EACH old entry, you MUST explicitly decide one of:

  [KEEP]   — Still valid and accurate → include EXACTLY as-is, keep its id
  [UPDATE] — Info is stale or needs revision → rewrite the entry content, KEEP its id
  [DELETE] — No longer applicable, was a mistake, or superseded → remove entirely

Important: do NOT silently drop old entries. If you are unsure, default to [KEEP].

### Step 2 — Extract new entries
From NEW DAILY MEMORIES and topics, identify information that deserves permanent status.
Only upgrade information that is:
  - A long-term user preference or habit
  - A critical or irreversible project decision
  - A hard-won lesson or reusable insight
  - An error pattern likely to recur
  - A meaningful project milestone

Assign a unique id for each NEW entry following the pattern:
  pref-XXX   for user preferences
  dec-XXX    for project decisions
  learn-XXX  for lessons learned
  err-XXX    for errors fixed
  prog-XXX   for project progress

Start numbering from 001 within each prefix. If old entries use these same prefixes,
continue from the highest existing number + 1.

### Step 3 — Output the merged result
Output the COMPLETE merged CHACHA_MEMORY.md containing ALL [KEEP] + [UPDATE] + [NEW] entries.
Remove only [DELETE] entries.
NO entry limit — keep all truly persistent information.
Sort entries by importance within each category.

### Entry format for CHACHA_MEMORY.md:
```
### Critical Preferences
- [id:pref-001] User prefers dark theme CLI with green accents
  Source: sessions/2026-06-10.md
  Updated: 2026-06-10T09:15:00Z

- [id:pref-002] Uses pytest with pytest-asyncio for all tests
  Source: sessions/2026-06-15.md
  Updated: 2026-06-15T14:20:00Z

### Key Decisions
- [id:dec-001] Database: SQLite chosen for lightweight local deployment
  Source: sessions/2026-06-05.md
  Updated: 2026-06-05T10:30:00Z

### Lessons Learned
- [id:learn-001] Always add index before querying large tables — missed it caused 30s query
  Source: sessions/2026-06-08.md
  Updated: 2026-06-08T16:45:00Z

### Errors Fixed
- [id:err-001] ImportError on missing libffi-dev — install system package first
  Source: sessions/2026-06-03.md
  Updated: 2026-06-03T11:20:00Z

### Project Progress
- [id:prog-001] Auth module: login/logout/session completed (2026-06-12)
  Source: sessions/2026-06-12.md
  Updated: 2026-06-12T17:00:00Z
```


## Output Format:
Output exactly in this format with these exact separators.
The {timestamp} placeholder will be replaced with the current UTC time.

===MEMORY_MD===
## Memory Index (autoDream generated at {timestamp})

### User Preferences
- Summary line here
  Source: sessions/{date}.md
  Updated: {timestamp}

### Project Decisions
- Summary line here
  Source: sessions/{date}.md
  Updated: {timestamp}

### Lessons Learned
...

### Errors Fixed
...

### Project Progress
...

===CHACHA_MEMORY_MD===
## Permanent Project Memory (autoDream updated at {timestamp})

### Critical Preferences
- [id:pref-001] Entry content here
  Source: sessions/{date}.md
  Updated: {timestamp}

### Key Decisions
...

### Lessons Learned
...

### Errors Fixed
...

### Project Progress
...

Output ONLY the content between the markers. No additional explanations, no commentary."""




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

        
        # 5. 通知 GlobalDream
        try:
            from core.context.global_dream import get_global_dream
            gd = get_global_dream()
            gd.record_project_dream()
            if gd.should_run():
                logger.info("触发 GlobalDream 用户级记忆整合...")
                asyncio.create_task(gd.run())
        except Exception as e:
            logger.warning("GlobalDream 钩子异常: %s", e)

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

        # memory/session/ 下的每日文件
        days = memory_manager.list_all_session_days(limit_days=self._prune_days)
        for f in days:
            text = self._read(f)
            if text.strip():
                parts.append(f"## session/{f.name}\n{text}")

        # memory/topics/ 下的主题内容
        topics_text = memory_manager.all_topics_content()
        if topics_text:
            parts.append(f"## topics\n{topics_text}")

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
