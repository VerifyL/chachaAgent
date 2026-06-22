"""
core/context_manager.py
ContextManager — 上下文管理器：组装消息、触发压缩、注入记忆。

v2.0 设计（DYNAMIC_BOUNDARY + 永久记忆）：
1. protected 区按固定顺序装载（永不截断）：
   SYSTEM_PROMPT → CHACHA.md(宪法) → CHACHA_MEMORY.md(永久记忆) → SKILL
2. dynamic 区按 importance 排序：
   MEMORY.md(索引) → 今日会话记忆 → 对话历史 → 工具结果 → RAG → hooks
3. Token 预算检查：utilization > trigger_ratio → needs_compression=True
4. 钩子集成：PRE/POST_CONTEXT_ASSEMBLY 可注入追加 ContextBlock

用法:
    mgr = ContextManager(config.context, hooks, telemetry)
    mgr.set_permanent_memory("永久记忆内容")
    mgr.set_memory_index("MEMORY.md 内容")
    ctx = mgr.assemble(conversation_state, session_id)
    messages = ctx.get_messages()  # → LLMInvoker
"""

import logging
import time
from typing import Any, Dict, List, Optional

from core.models.context import (
    AssembledContext, BlockSource, CompressionLevel, ContextAssemblyMeta,
    ContextBlock, TriggerReason,
)
from core.models.config import ContextConfig
from core.models.session import (
    ConversationState, MessageEvent, ToolCallEvent, ObservationEvent,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are ChaChaAgent, a helpful AI assistant with access to tools. "
    "Use tools when needed to read files, execute commands, or search code. "
    "Always respond in the user's language."
)


class ContextManager:
    """上下文管理器（v2.0 — 永久记忆 + 两阶段工具缓存）。"""

    def __init__(
        self,
        config: Optional[ContextConfig] = None,
        hook_orchestrator: Optional[Any] = None,
        telemetry: Optional[Any] = None,
    ):
        cfg = config or ContextConfig()
        self._budget = cfg.max_tokens
        self._trigger_ratio = cfg.compression_trigger_ratio
        self._memory_max_lines = cfg.memory_max_lines
        self._keep_system_first = cfg.keep_system_prompt_first
        self._enable_summarization = cfg.enable_summarization
        self._hooks = hook_orchestrator
        self._telemetry = telemetry
        self._system_prompt = DEFAULT_SYSTEM_PROMPT

        # 可注入的静态内容
        self._static_rules = ""       # CHACHA.md 宪法
        self._permanent_memory = ""   # CHACHA_MEMORY.md 永久记忆
        self._skills = ""             # 技能定义
        self._memory_index = ""       # MEMORY.md 轻量索引
        self._session_memory = ""     # 今日会话记忆
        self._global_permanent_memory = ""  # ~/.chacha/USER_MEMORY.md 用户级永久记忆

        self._block_cache: Dict[str, ContextBlock] = {}

    # ====== 注入接口 ======

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt
        self._block_cache.pop(str(BlockSource.SYSTEM_PROMPT), None)

    def set_static_rules(self, rules: str) -> None:
        """注入 CHACHA.md 宪法内容。"""
        self._static_rules = rules
        self._block_cache.pop(str(BlockSource.STATIC_RULE), None)

    def set_global_permanent_memory(self, content: str) -> None:
        """注入 ~/.chacha/USER_MEMORY.md 用户级永久记忆（保护区）。"""
        self._global_permanent_memory = content

    def set_permanent_memory(self, content: str) -> None:
        """注入 CHACHA_MEMORY.md 永久记忆（保护区）。"""
        self._permanent_memory = content
        self._block_cache.pop("permanent_memory", None)

    def set_skills(self, skills: str) -> None:
        """注入技能/工具 schema 定义。"""
        self._skills = skills

    def set_memory_index(self, content: str) -> None:
        """注入 MEMORY.md 轻量索引（动态区）。"""
        self._memory_index = content

    def set_session_memory(self, content: str) -> None:
        """注入今日会话记忆（动态区）。"""
        self._session_memory = content
        
    @staticmethod
    def build_system_prompt(project_root, base_prompt: str = "", memory_manager=None) -> str:
        """从所有来源加载上下文，返回组装好的完整系统提示词。

        来源（按顺序拼接）：
        1. base_prompt — 调用方传入的基础提示词
        2. ~/.chacha/CHACHA.md — 用户级全局宪法（跨项目）
        3. {cwd}/CHACHA.md — 项目级宪法
        4. CHACHA_MEMORY.md — 永久记忆
        5. MEMORY.md — 轻量索引
        """
        from pathlib import Path
        from core.context.memory_manager import MemoryManager

        sections = [base_prompt] if base_prompt else []

        # 1. CHACHA.md 宪法（全局 + 项目，两层拼接）
        chacha_parts = []
        global_chacha = Path.home() / ".chacha" / "CHACHA.md"
        if global_chacha.exists():
            chacha_parts.append(global_chacha.read_text(encoding="utf-8"))
        project_chacha = Path(project_root) / "CHACHA.md"
        if project_chacha.exists():
            chacha_parts.append(project_chacha.read_text(encoding="utf-8"))
        rules_text = "\n\n".join(chacha_parts)
        if rules_text:
            sections.append(f"--- 项目宪法 (CHACHA.md) ---\n{rules_text}")

        # 2. CHACHA_MEMORY.md + MEMORY.md（从传入的 MemoryManager 或新建项目级）
        mgr = memory_manager or MemoryManager(project_root=project_root)
        permanent = mgr.read_permanent_memory()
        if permanent:
            sections.append(f"--- 项目永久记忆 ---\n{permanent}")
        if mgr._session_dir:
            memory_index = mgr.read()
            if memory_index:
                sections.append(f"--- 记忆索引 ---\n{memory_index}")

        return "\n\n".join(sections)

    def clear_cache(self) -> None:
        self._block_cache.clear()

    # ====== 公开接口 ======

    def assemble(
        self,
        state: ConversationState,
        session_id: str = "",
        static_rules: Optional[str] = None,
        skills: Optional[str] = None,
        memory_content: Optional[str] = None,
        additional_contexts: Optional[List[ContextBlock]] = None,
    ) -> AssembledContext:
        """从 ConversationState 组装 AssembledContext。

        v2.0 上下文字段顺序:
            protected: SYSTEM_PROMPT → CHACHA.md → CHACHA_MEMORY.md → SKILL
            dynamic:   MEMORY.md → Session Memory → History → Tool Results → RAG → Hooks
        """
        blocks: list[ContextBlock] = []

        # ---- protected zone ----

        # 1. System Prompt
        blocks.append(self._cached_block(
            BlockSource.SYSTEM_PROMPT, "system", self._system_prompt,
            zone="protected", priority=0, importance=1.0, ttl=600,
        ))

        # 2. CHACHA.md 宪法
        rules = static_rules or self._static_rules
        if rules:
            blocks.append(self._cached_block(
                BlockSource.STATIC_RULE, "system", rules,
                zone="protected", priority=1, importance=0.95, ttl=600,
            ))
        
        
        # 3 ~/.chacha/USER_MEMORY.md 用户级永久记忆（跨项目）
        if self._global_permanent_memory:
            blocks.append(self._cached_block(
                BlockSource.STATIC_RULE, "system",
                f"[Global Permanent Memory]\n{self._global_permanent_memory}",
                zone="protected", priority=2, importance=0.92, ttl=300,
            ))


        # 4. CHACHA_MEMORY.md 永久记忆
        if self._permanent_memory:
            blocks.append(self._cached_block(
                BlockSource.STATIC_RULE, "system",  # 复用 STATIC_RULE source
                f"[Permanent Memory]\n{self._permanent_memory}",
                zone="protected", priority=3, importance=0.9, ttl=300,
            ))

        # 5. SKILL 定义
        skill_content = skills or self._skills
        if skill_content:
            blocks.append(self._cached_block(
                BlockSource.SKILL, "system", skill_content,
                zone="protected", priority=4, importance=0.85, ttl=1200,
            ))

        protected_count = len(blocks)

        # ---- dynamic zone ----

        # 5. MEMORY.md 轻量索引
        if memory_content or self._memory_index:
            blocks.append(ContextBlock(
                source=BlockSource.MEMORY, role="system",
                content=f"[Memory Index]\n{memory_content or self._memory_index}",
                zone="dynamic", priority=10, importance=0.7,
                token_count=self._estimate_tokens(memory_content or self._memory_index),
            ))

        # 6. 今日会话记忆
        if self._session_memory:
            blocks.append(ContextBlock(
                source=BlockSource.MEMORY, role="system",
                content=f"[Today's Session Memory]\n{self._session_memory}",
                zone="dynamic", priority=11, importance=0.65,
                token_count=self._estimate_tokens(self._session_memory),
            ))

        # 7. 对话历史 + 工具结果
        history_start = len(blocks)
        for i, event in enumerate(state.events):
            if isinstance(event, MessageEvent):
                blocks.append(ContextBlock(
                    source=BlockSource.HISTORY,
                    role=event.role,
                    content=event.content,
                    zone="dynamic",
                    priority=20 + i,
                    importance=self._history_importance(i),
                    token_count=self._estimate_tokens(event.content),
                ))
            elif isinstance(event, ToolCallEvent):
                blocks.append(ContextBlock(
                    source=BlockSource.HISTORY,
                    role="assistant",
                    content=f"[Tool Call: {event.tool_name}({event.arguments})]",
                    zone="dynamic",
                    priority=20 + i,
                    importance=self._history_importance(i),
                    token_count=self._estimate_tokens(str(event.arguments)),
                ))
            elif isinstance(event, ObservationEvent):
                # 工具结果（可能已被 Dispatcher 替换为占位符）
                content = event.content
                if event.truncated:
                    content = content[:500] + f"\n...[截断，原始 {len(event.content)} 字符]"
                blocks.append(ContextBlock(
                    source=BlockSource.TOOL_RESULT,
                    role="tool",
                    content=content,
                    zone="dynamic",
                    priority=30 + i,
                    importance=0.5,
                    token_count=self._estimate_tokens(content),
                ))

        # 8. 钩子注入 additional_context
        if additional_contexts:
            blocks.extend(additional_contexts)

        # ---- 计算统计 ----
        total_tokens = sum(b.token_count for b in blocks)
        protected_tokens = sum(b.token_count for b in blocks[:protected_count])
        dynamic_tokens = total_tokens - protected_tokens
        budget = self._budget or 128000
        utilization = total_tokens / budget if budget > 0 else 0
        pressure = min(1.0, utilization * 1.25)

        needs_compression = utilization > self._trigger_ratio
        recommended = self._recommend_compression(pressure)

        meta = ContextAssemblyMeta(
            total_tokens=total_tokens,
            budget_per_request=budget,
            utilization_ratio=round(utilization, 4),
            compression_pressure=round(pressure, 4),
            trigger=TriggerReason.THRESHOLD.value if needs_compression else TriggerReason.NONE.value,
            protected_tokens=protected_tokens,
            dynamic_tokens=dynamic_tokens,
            reasoning_budget_tokens=0,
            reasoning_tokens_used=0,
            blocks_by_source=self._count_by_source(blocks),
            trigger_reason=TriggerReason.THRESHOLD.value if needs_compression else TriggerReason.NONE.value,
        )

        ctx = AssembledContext(
            meta=meta, blocks=blocks,
            needs_compression=needs_compression,
            recommended_level=recommended,
        )

        if self._telemetry:
            self._telemetry.agent.record_context(
                total_tokens=total_tokens,
                utilization=utilization,
                compression_triggered=needs_compression,
            )

        logger.debug(
            "上下文组装完成: %d blocks, %d tokens (利用率 %.1f%%, 压缩=%s)",
            len(blocks), total_tokens, utilization * 100, needs_compression,
        )
        return ctx

    def get_messages(self, state: ConversationState) -> List[Dict[str, Any]]:
        return state.get_messages_for_llm()

    # ====== 内部 ======

    def _cached_block(
        self, source, role: str, content: str,
        zone: str, priority: int, importance: float, ttl: int,
    ) -> ContextBlock:
        cache_key = str(source) if not isinstance(source, str) else source
        cached = self._block_cache.get(cache_key)
        now = time.time()

        if cached and cached.content == content:
            age = now - cached.created_at.timestamp()
            if age < ttl:
                return cached

        block = ContextBlock(
            source=source, role=role, content=content,
            zone=zone, priority=priority,
            importance_score=importance, cache_ttl=ttl,
            token_count=self._estimate_tokens(content),
        )
        self._block_cache[cache_key] = block
        return block

    def _recommend_compression(self, pressure: float) -> CompressionLevel:
        if pressure < 0.5:
            return CompressionLevel.NONE
        if pressure < 0.7:
            return CompressionLevel.FROZEN
        if pressure < 0.85:
            return CompressionLevel.TRIMMED
        if pressure < 0.95:
            return CompressionLevel.SUMMARIZED
        return CompressionLevel.CONSOLIDATED

    def _history_importance(self, position: int) -> float:
        return max(0.3, 1.0 - position * 0.05)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    @staticmethod
    def _count_by_source(blocks: list[ContextBlock]) -> dict[str, int]:
        dist: dict[str, int] = {}
        for b in blocks:
            s = b.source if isinstance(b.source, str) else b.source.value
            dist[s] = dist.get(s, 0) + b.token_count
        return dist
