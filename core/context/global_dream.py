"""
core/context/global_dream.py
GlobalDream — 用户级永久记忆整合（跨项目提炼）。

在每个项目 DreamPipeline 完成后触发检查：
  - 累计 50 次项目级 DreamPipeline 运行
  - 或距上次运行超过 7 天（168 小时）

流程：
  1. 收集所有 ~/.chacha/projects/*/CHACHA_MEMORY.md
  2. 读取旧 ~/.chacha/USER_MEMORY.md
  3. 1 次 LLM 调用 → 增量合并（KEEP/UPDATE/DELETE/NEW）
  4. 写入 ~/.chacha/USER_MEMORY.md
"""

import asyncio
import logging
import time
from datetime import timedelta,  datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE = Path.home() / ".chacha"
_GLOBAL_PERMANENT_FILE = "USER_MEMORY.md"
_GLOBAL_DREAM_COUNT = 1       # 累计 50 次项目 dream 触发
_GLOBAL_DREAM_HOURS = 168      # 每 7 天触发

GLOBAL_DREAM_SYSTEM_PROMPT = """You are a user-level memory consolidation assistant.
Your task: merge per-project permanent memories into a single USER-LEVEL permanent memory.

## Input
You will receive:
1. OLD user-level USER_MEMORY.md — existing cross-project permanent memory
2. NEW per-project permanent memories — one section per project, tagged with project name

## Goal
Extract only information that applies ACROSS MULTIPLE PROJECTS:
- User preferences that span projects (e.g. "prefers pytest", "likes dark themes")
- Universal lessons or reusable insights (e.g. "always validate API responses")
- Habits, workflow patterns, or tool preferences that appear in ≥2 projects
- Global error patterns that recur across projects

DO NOT include:
- Project-specific tech stack choices (e.g. "Project X uses SQLite")
- Project-specific milestones or progress
- One-off errors specific to a single project
- Anything that would be wrong/misleading if read in a different project

## Rules (same as project-level incremental merge):

### Step 1 — Review old entries
Each entry in OLD USER_MEMORY.md has a stable [id:xxx] tag.
For EACH old entry, decide:
  [KEEP]   — Still valid and accurate → include EXACTLY as-is
  [UPDATE] — Info is stale or needs revision → rewrite, KEEP its id
  [DELETE] — No longer applicable → remove entirely

### Step 2 — Extract new entries
Assign unique IDs: pref-XXX, learn-XXX, err-XXX (user-level only, no project-specific)
Continue numbering from highest existing + 1.

### Step 3 — Output the merged result
Output the COMPLETE merged USER_MEMORY.md.
Sort by importance within each category.
No entry limit.

## Entry format:
```
### User Preferences
- [id:pref-001] User prefers dark theme CLI with green accents
  Source: projects/chachaAgent, otherProject
  Updated: 2026-06-19T12:00:00Z

### Key Lessons
- [id:learn-001] Always verify file paths before dangerous operations
  Source: projects/chachaAgent, webScraper
  Updated: 2026-06-19T12:00:00Z

### Error Patterns
- [id:err-001] ImportError: platform-specific deps must be documented
  Source: projects/chachaAgent
  Updated: 2026-06-19T12:00:00Z
```

## Output Format:
```
{timestamp}

### User Preferences
- [id:pref-001] ...

### Key Lessons
- [id:learn-001] ...

### Error Patterns
- [id:err-001] ...
```

Output ONLY the merged USER_MEMORY.md content. No explanations, no separators, no commentary."""


def _get_global_store() -> Path:
    """返回 ~/.chacha/ 目录，确保存在。"""
    _DEFAULT_BASE.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_BASE


def read_global_permanent_memory() -> str:
    """读取 ~/.chacha/USER_MEMORY.md。"""
    path = _DEFAULT_BASE / _GLOBAL_PERMANENT_FILE
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


class GlobalDream:
    """用户级永久记忆整合管道（模块级单例，跨项目共享）。"""

    def __init__(self, llm_invoker=None):
        self._llm = llm_invoker
        self._last_run: Optional[float] = None
        self._dream_count = 0

    def set_llm(self, llm_invoker) -> None:
        self._llm = llm_invoker

    def record_project_dream(self) -> None:
        """每次项目级 DreamPipeline 完成后调用。"""
        self._dream_count += 1

    def should_run(self) -> bool:
        """检查是否应触发 GlobalDream。"""
        if self._llm is None:
            return False
        if self._last_run is None:
            return self._dream_count >= _GLOBAL_DREAM_COUNT

        count_triggered = self._dream_count >= _GLOBAL_DREAM_COUNT
        time_triggered = time.time() - self._last_run > _GLOBAL_DREAM_HOURS * 3600
        return count_triggered or time_triggered

    async def run(self) -> str:
        """执行 GlobalDream 整合。返回更新的 USER_MEMORY.md 内容。"""
        logger.info("GlobalDream 开始整合用户级永久记忆...")
        t0 = time.monotonic()

        # 1. Gather
        old_global = read_global_permanent_memory()
        project_memories = self._gather_project_memories()

        if not project_memories and not old_global.strip():
            logger.info("GlobalDream: 无内容需要整合")
            return ""

        # 2. Consolidate
        merged = await self._consolidate(old_global, project_memories)

        # 3. Write
        if merged.strip():
            path = _DEFAULT_BASE / _GLOBAL_PERMANENT_FILE
            path.write_text(merged.strip() + "\n", encoding="utf-8")
            logger.info("GlobalDream: 已写入 %s (%d chars)", path, len(merged))

        self._last_run = time.time()
        self._dream_count = 0

        elapsed = time.monotonic() - t0
        logger.info("GlobalDream 完成 (%.1fs, %d chars)", elapsed, len(merged))
        return merged

    def _gather_project_memories(self) -> list[tuple[str, str]]:
        """收集所有项目永久记忆。返回 [(project_name, content), ...]"""
        result = []
        projects_dir = _DEFAULT_BASE / "projects"
        if not projects_dir.exists():
            return result

        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            chacha_mem = proj_dir / _GLOBAL_PERMANENT_FILE
            if chacha_mem.exists():
                content = chacha_mem.read_text(encoding="utf-8").strip()
                if content:
                    result.append((proj_dir.name, content))
        return result

    async def _consolidate(
        self, old_global: str, project_memories: list[tuple[str, str]],
    ) -> str:
        """调用 LLM 增量合并用户级永久记忆。"""
        ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%SZ")

        system_prompt = GLOBAL_DREAM_SYSTEM_PROMPT.replace("{timestamp}", ts)

        user_parts = []
        if old_global.strip():
            user_parts.append(
                f"## OLD USER-LEVEL USER_MEMORY.md\n```\n{old_global}\n```"
            )
        for proj_name, content in project_memories:
            user_parts.append(
                f"## Project: {proj_name}\n```\n{content}\n```"
            )

        user_content = "\n\n".join(user_parts) if user_parts else "No memories."

        resp = await self._llm.invoke(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            session_id="global-dream-consolidation",
        )

        return resp.text.strip()


# ====== 模块级单例 ======
_global_dream: Optional[GlobalDream] = None


def get_global_dream() -> GlobalDream:
    global _global_dream
    if _global_dream is None:
        _global_dream = GlobalDream()
    return _global_dream
