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

# 默认系统提示词 — 当 project_init 不可用时的兜底基线
DEFAULT_SYSTEM_PROMPT = (
    "你是 ChachaAgent，一个通用 AI 助手。"
    "回复简洁直接，中文优先。"
    "可使用文件读写、搜索、Shell 执行等工具完成任务。"
    "写入前确认现有内容，操作后验证结果。"
    "每次回复末尾自检（偏好/决策/错误/经验/进度），调用 memory 记录。"
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
            memory_index = mgr.read_index()
            if memory_index:
                sections.append(f"--- 记忆索引 ---\n{memory_index}")

        return "\n\n".join(sections)

    def clear_cache(self) -> None:
        self._block_cache.clear()

    # ====== 公开接口 ======

    def _build_protected_and_memory_blocks(
        self,
        static_rules: Optional[str] = None,
        skills: Optional[str] = None,
        memory_content: Optional[str] = None,
    ) -> tuple[list[ContextBlock], int]:
        """构建 protected zone（4 blocks）+ dynamic zone 记忆注入（2 blocks）。

        返回 (blocks, protected_count)。
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

        # 5. MEMORY.md 轻量索引
        mem_content = memory_content or self._memory_index
        if mem_content:
            blocks.append(ContextBlock(
                source=BlockSource.MEMORY, role="system",
                content=f"[Memory Index]\n{mem_content}",
                zone="dynamic", priority=10, importance=0.7,
                token_count=self._estimate_tokens(mem_content),
            ))

        # 6. 今日会话记忆
        if self._session_memory:
            blocks.append(ContextBlock(
                source=BlockSource.MEMORY, role="system",
                content=f"[Today's Session Memory]\n{self._session_memory}",
                zone="dynamic", priority=9, importance=0.6,
                token_count=self._estimate_tokens(self._session_memory),
            ))

        return blocks, protected_count

    @staticmethod
    def _finalize_context(
        blocks: list[ContextBlock],
        protected_count: int,
        cm: "ContextManager",
        log_suffix: str = "",
    ) -> "AssembledContext":
        """计算统计、追加预算提示条、构建 AssembledContext 并记录 telemetry。

        assemble() 和 assemble_from_messages_direct() 共享的尾部逻辑。
        """
        total_tokens = sum(b.token_count for b in blocks)
        protected_tokens = sum(b.token_count for b in blocks[:protected_count])
        dynamic_tokens = total_tokens - protected_tokens
        budget = cm._budget or 1_048_576
        utilization = total_tokens / budget if budget > 0 else 0
        pressure = min(1.0, utilization * 1.25)

        budget_hint = (
            f"[Token Budget] 总预算: {budget} | 已用: {total_tokens} "
            f"({utilization:.0%}) | 剩余: {budget - total_tokens} | 压力: {pressure:.0%}"
        )
        blocks.append(ContextBlock(
            source=BlockSource.ADDITIONAL_CONTEXT, role="system",
            content=budget_hint, zone="dynamic", priority=999,
            importance=0.1, token_count=0,
        ))

        needs_compression = utilization > cm._trigger_ratio
        recommended = cm._recommend_compression(pressure)

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
            blocks_by_source=cm._count_by_source(blocks),
        )

        ctx = AssembledContext(
            meta=meta, blocks=blocks,
            needs_compression=needs_compression,
            recommended_level=recommended,
        )

        if cm._telemetry and cm._telemetry.agent:
            cm._telemetry.agent.record_context(
                total_tokens=total_tokens,
                utilization=utilization,
                compression_triggered=needs_compression,
            )

        logger.debug(
            "上下文组装完成%s: %d blocks, %d tokens (利用率 %.1f%%, 压缩=%s)",
            log_suffix, len(blocks), total_tokens, utilization * 100, needs_compression,
        )
        return ctx

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
        blocks, protected_count = self._build_protected_and_memory_blocks(
            static_rules=static_rules, skills=skills, memory_content=memory_content,
        )

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
                            f"\n...[二次截断，原始 {len(event.content)} 字符。"
                            f"使用 cache_read 工具凭 cache_key={event.cache_key} 续读]"
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

        return ContextManager._finalize_context(blocks, protected_count, self)

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
        """消息列表 → assemble()（保留兼容）。新代码请用 assemble_from_messages_direct()。"""
        return ContextManager.assemble_from_messages_direct(messages, cm, **kwargs)

    @staticmethod
    def assemble_from_messages_direct(
        messages: List[Dict[str, Any]],
        cm: "ContextManager",
        additional_contexts: Optional[List[ContextBlock]] = None,
        **kwargs,
    ) -> AssembledContext:
        """直接从消息 dict 列表构建 AssembledContext，跳过 ConversationState 往返。

        与 assemble() 产出相同，但绕过 events 中间态，减少转换损耗。
        cm 为 None 时跳过 protected zone 构建（用于 auto_compact 等场景）。
        """
        import json

        blocks, protected_count = cm._build_protected_and_memory_blocks(
            static_rules=kwargs.get("static_rules"),
            skills=kwargs.get("skills"),
            memory_content=kwargs.get("memory_content"),
        )

        # 7. 对话历史（直接遍历 dict，跳过已有 protected）
        skipped_system = False
        for i, m in enumerate(messages):
            role = m.get("role", "")
            content = m.get("content", "") or ""

            if role == "system":
                if not skipped_system:
                    skipped_system = True
                    continue  # 跳过第一条 system（已被 protected 替代）
                # 后续 system 消息保留
                blocks.append(ContextBlock(
                    source=BlockSource.HISTORY, role="system", content=str(content),
                    zone="dynamic", priority=100 + i,
                    importance=cm._history_importance(i),
                    token_count=cm._estimate_tokens(str(content)),
                ))
            elif role == "user":
                blocks.append(ContextBlock(
                    source=BlockSource.HISTORY, role="user", content=str(content),
                    zone="dynamic", priority=100 + i,
                    importance=cm._history_importance(i),
                    token_count=cm._estimate_tokens(str(content)),
                ))
            elif role == "assistant":
                tool_calls = m.get("tool_calls")
                if tool_calls:
                    # 工具调用：存到 _extra 供 blocks_to_messages 恢复
                    tc_text = ""
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tc_text += f"[Tool Call: {fn.get('name', '?')}({fn.get('arguments', '{}')})] "
                    blocks.append(ContextBlock(
                        source=BlockSource.HISTORY, role="assistant",
                        content=str(content) if content else tc_text.strip(),
                        zone="dynamic", priority=100 + i,
                        importance=cm._history_importance(i),
                        token_count=cm._estimate_tokens(str(content) + tc_text),
                    ))
                    # 注入 _extra 保留 tool_calls
                    if not hasattr(blocks[-1], '_extra') or blocks[-1]._extra is None:
                        blocks[-1]._extra = {}
                    blocks[-1]._extra["tool_calls"] = tool_calls
                elif content:
                    blocks.append(ContextBlock(
                        source=BlockSource.HISTORY, role="assistant", content=str(content),
                        zone="dynamic", priority=100 + i,
                        importance=cm._history_importance(i),
                        token_count=cm._estimate_tokens(str(content)),
                    ))
            elif role == "tool":
                blocks.append(ContextBlock(
                    source=BlockSource.TOOL_RESULT, role="tool", content=str(content),
                    zone="dynamic", priority=100 + i, importance=0.5,
                    token_count=cm._estimate_tokens(str(content)),
                ))

        # 8. RAG / SubAgent / 钩子注入
        if additional_contexts:
            blocks.extend(additional_contexts)

        return cm._finalize_context(blocks, protected_count, cm, log_suffix="(direct)")

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
        if pressure < 0.7:
            return CompressionLevel.NONE  # 冻结已由 Dispatcher 实时完成，此处无需 FROZEN
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
