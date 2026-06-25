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
import uuid
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
    "Always respond in the user's language.\n\n"
    "Rules:\n"
    "- When the user asks about something mentioned earlier in this conversation, "
    "answer from the conversation history. Do NOT call tools like read_topic or "
    "load_memory to look up what was already said. Only use tools if the "
    "information is NOT in the current conversation.\n"
    "- When the user says 'remember' or asks you to save information, use "
    "the remember and write_topic tools to persist it. Do not just acknowledge.\n\n"
    "## Topic auto-recording (CRITICAL — do NOT skip)\n"
    "After EVERY meaningful exchange that involves any of the following, "
    "you MUST call write_topic to persist it. This is as important as giving "
    "the correct answer. Err on the side of writing too much rather than too little.\n\n"
    "### project-decisions (technology/architecture choices)\n"
    "Trigger when: the user or you makes a decision about technology stack, "
    "architecture pattern, library choice, file/module layout, naming convention, "
    "API design, or any tradeoff discussion that shapes the project.\n"
    "Example: 'We decided to use FastAPI over Flask because of async support.'\n"
    "Example: 'The cache layer will be a separate module, not mixed with db logic.'\n\n"
    "### lessons-learned (pitfalls, patterns, reusable insights)\n"
    "Trigger when: you encounter a non-obvious bug, a tool behaves unexpectedly, "
    "a pattern works well (or badly), a workaround is discovered, or something "
    "surprising is learned that would help future development.\n"
    "Example: 'edit_file requires exact string match — even one space difference fails.'\n"
    "Example: 'bash in sandbox cannot access files outside the project root.'\n\n"
    "### errors-fixed (bug fix records with solution)\n"
    "Trigger when: a bug is diagnosed and fixed. Record the symptom, root cause, "
    "and the fix. This creates a searchable bug database for future reference.\n"
    "Example: 'ImportError: missing X — root cause was PYTHONPATH missing src/, fixed by adding it.'\n"
    "Example: 'Race condition in task queue — fixed by adding asyncio.Lock.'\n\n"
    "### project-progress (milestones, completed features)\n"
    "Trigger when: a feature is completed, a significant refactor is done, "
    "a version is released, tests pass for a new module, or any measurable "
    "progress is made. Also record TODO items that emerge.\n"
    "Example: 'Completed: user login endpoint with JWT auth, all tests passing.'\n"
    "Example: 'Refactored memory_manager.py — split read/write concerns into separate classes.'\n"
    "Example: 'TODO: add rate limiting to the chat endpoint.'\n\n"
    "### user-preferences (personal style, habits)\n"
    "Trigger when: the user states a preference about coding style, tool choices, "
    "language, communication style, or workflow habits.\n"
    "Example: 'User prefers Chinese replies.'\n"
    "Example: 'User prefers minimal comments, only docstrings.'\n\n"
    "IMPORTANT: Do NOT wait for the user to say 'remember'. If the conversation "
    "contains any of the above patterns, call write_topic proactively after "
    "finishing your response. A silent topic file is a sign you are not doing your job."
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
        if mgr.session_dir:
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
        history_trimmed: bool = False,   # 历史被裁剪后才注入 MEMORY.md
    ) -> AssembledContext:
        """
        从 ConversationState 组装 AssembledContext。

        顺序 (v3.0)：
            protected: SYSTEM_PROMPT+CHACHA.md → USER_MEMORY → CHACHA_MEMORY → SKILLS
            dynamic:   MEMORY.md(条件) → 对话历史 → 工具结果 → RAG/Hooks
        """
        blocks: list[ContextBlock] = []

        # ==================== protected zone ====================

        # 1. System Prompt + CHACHA.md 合并为一条
        system_text = self._system_prompt
        rules = static_rules or self._static_rules
        if rules:
            system_text += f"\n\n{rules}"
        blocks.append(self._cached_block(
            BlockSource.SYSTEM_PROMPT, "system", system_text,
            zone="protected", priority=0, importance=1.0, ttl=600,
        ))

        # 2. USER_MEMORY.md 用户级永久记忆
        if self._global_permanent_memory:
            blocks.append(self._cached_block(
                BlockSource.STATIC_RULE, "system",
                f"[Global Permanent Memory]\n{self._global_permanent_memory}",
                zone="protected", priority=1, importance=0.92, ttl=300,
            ))

        # 3. CHACHA_MEMORY.md 项目永久记忆
        if self._permanent_memory:
            blocks.append(self._cached_block(
                BlockSource.STATIC_RULE, "system",
                f"[Permanent Memory]\n{self._permanent_memory}",
                zone="protected", priority=2, importance=0.9, ttl=300,
            ))

        # 4. SKILLS / Tool schemas
        skill_content = skills or self._skills
        if skill_content:
            blocks.append(self._cached_block(
                BlockSource.SKILL, "system", skill_content,
                zone="protected", priority=3, importance=0.85, ttl=1200,
            ))

        protected_count = len(blocks)

        # ==================== dynamic zone ====================

        # 5. MEMORY.md 轻量索引（常驻动态区）
        if memory_content or self._memory_index:
            blocks.append(ContextBlock(
                source=BlockSource.MEMORY, role="system",
                content=f"[Memory Index]\n{memory_content or self._memory_index}",
                zone="dynamic", priority=10, importance=0.7,
                token_count=self._estimate_tokens(memory_content or self._memory_index),
            ))

        # 6. 今日会话记忆（跨 session 切换后由 set_session_memory 注入）
        if self._session_memory:
            blocks.append(ContextBlock(
                source=BlockSource.MEMORY, role="system",
                content=f"[Today's Session Memory]\n{self._session_memory}",
                zone="dynamic", priority=9, importance=0.6,
                token_count=self._estimate_tokens(self._session_memory),
            ))

        # 6. 对话历史
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
                    content=event.content or f"[Tool Call: {event.tool_name}({event.arguments})]",
                    zone="dynamic",
                    priority=20 + i,
                    importance=self._history_importance(i),
                    token_count=self._estimate_tokens(str(event.arguments)),
                ))
            elif isinstance(event, ObservationEvent):
                content = event.content
                if event.truncated:
                    # 分级截断：保留更多可读内容
                    max_chars = 8000
                    if len(event.content) > max_chars:
                        content = event.content[:max_chars] + (
                            f"\n...[截断，原始 {len(event.content)} 字符，"
                            f"可用 cache_key={event.cache_key} 继续读取]"
                        )
                    else:
                        content = event.content
                blocks.append(ContextBlock(
                    source=BlockSource.TOOL_RESULT,
                    role="tool",
                    content=content,
                    zone="dynamic",
                    priority=30 + i,
                    importance=0.5,
                    token_count=self._estimate_tokens(content),
                ))

        # 7. RAG / SubAgent / 钩子注入
        if additional_contexts:
            blocks.extend(additional_contexts)

        # ---- 计算统计 ----
        total_tokens = sum(b.token_count for b in blocks)
        protected_tokens = sum(b.token_count for b in blocks[:protected_count])
        dynamic_tokens = total_tokens - protected_tokens
        budget = self._budget or 128000
        utilization = total_tokens / budget if budget > 0 else 0
        pressure = min(1.0, utilization * 1.25)

        # Token 预算条（帮助 LLM 感知上下文压力）
        budget_hint = (
            f"[Token Budget] 总预算: {budget} | 已用: {total_tokens} "
            f"({utilization:.0%}) | 剩余: {budget - total_tokens} | 压力: {pressure:.0%}"
        )
        blocks.append(ContextBlock(
            source=BlockSource.ADDITIONAL_CONTEXT, role="system",
            content=budget_hint, zone="dynamic", priority=999,
            importance=0.1, token_count=0,
        ))

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
        )

        ctx = AssembledContext(
            meta=meta, blocks=blocks,
            needs_compression=needs_compression,
            recommended_level=recommended,
        )

        if self._telemetry and self._telemetry.agent:
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
        """兼容旧 API：直接返回原始 state 的消息。"""
        return state.get_messages_for_llm()

    # ====== 转换工具 ======

    @staticmethod
    def blocks_to_messages(ctx: AssembledContext) -> List[Dict[str, Any]]:
        """AssembledContext.blocks → OpenAI 消息格式（保留完整 tool_calls）。"""
        msgs: List[Dict[str, Any]] = []
        for b in ctx.blocks:
            entry: Dict[str, Any] = {"role": b.role, "content": b.content}
            # 保留 tool_calls（若块附加了额外元数据）
            extra = getattr(b, "_extra", None)
            if extra and "tool_calls" in extra:
                entry["tool_calls"] = extra["tool_calls"]
            msgs.append(entry)
        return msgs

    @staticmethod
    def messages_to_state(messages: List[Dict[str, Any]],
                          session_id: str = "",
                          project_id: str = "") -> ConversationState:
        """原始 OpenAI 消息列表 → ConversationState。"""
        from core.models.session import ConversationState, SessionMetadata
        import json
        meta = SessionMetadata(project_id=project_id or "",
                               session_id=session_id or str(uuid.uuid4()))
        state = ConversationState(metadata=meta)
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role in ("user", "system"):
                state.add_event(MessageEvent(
                    source="user" if role == "user" else "system",
                    role=role, content=content or "",
                ))
            elif role == "assistant":
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        fn = tc.get("function", {})
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        state.add_event(ToolCallEvent(
                            source="agent",
                            tool_name=fn.get("name", "?"),
                            arguments=args,
                            tool_use_id=tc.get("id", ""),
                        ))
                else:
                    state.add_event(MessageEvent(
                        source="agent", role="assistant", content=content or "",
                    ))
            elif role == "tool":
                state.add_event(ObservationEvent(
                    source="tool",
                    tool_use_id=m.get("tool_call_id", ""),
                    content=content or "",
                    status="success",
                ))
        return state

    @staticmethod
    def assemble_from_messages(
        messages: List[Dict[str, Any]],
        cm: "ContextManager",
        **kwargs,
    ) -> AssembledContext:
        """快捷：消息列表 → assemble()"""
        state = ContextManager.messages_to_state(messages)
        return cm.assemble(state, **kwargs)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        try:
            from core.context.token_counter import TokenCounter
            return TokenCounter().count_text(text)
        except Exception:
            return max(1, len(text) // 4)

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
    def _count_by_source(blocks: list[ContextBlock]) -> dict[str, int]:
        dist: dict[str, int] = {}
        for b in blocks:
            s = b.source if isinstance(b.source, str) else b.source.value
            dist[s] = dist.get(s, 0) + b.token_count
        return dist
