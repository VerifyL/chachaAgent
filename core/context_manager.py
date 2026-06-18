"""
core/context_manager.py
ContextManager — 上下文管理器：组装消息、触发压缩、注入记忆。

设计理念（DYNAMIC_BOUNDARY + Harness 三阶段组装）：
1. DYNAMIC_BOUNDARY：protected 区（系统提示+CHACHA.md+技能）永不被截断
2. Token 预算检查：utilization > trigger_ratio → needs_compression=True
3. 钩子集成：PRE/POST_CONTEXT_ASSEMBLY 可注入追加 ContextBlock
4. 阶段 2 简化版：从 ConversationState 直接转换；阶段 4 升级为完整三阶段引擎

用法:
    mgr = ContextManager(config.context, hooks, telemetry)
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

# ========================= 默认体系提示 =========================

DEFAULT_SYSTEM_PROMPT = (
    "You are ChaChaAgent, a helpful AI assistant with access to tools. "
    "Use tools when needed to read files, execute commands, or search code. "
    "Always respond in the user's language."
)


# ========================= 上下文管理器 =========================

class ContextManager:
    """
    上下文管理器。

    阶段 2 功能：从 ConversationState 组装 AssembledContext + 预算检查。
    阶段 4 升级：接入 ContextAssembler（记忆/RAG） + ContextCompressor（实际压缩）。

    TODO(阶段4): 接入 ContextAssembler 实现三阶段组装（需求分析→并行检索→合并排序）
    TODO(阶段4): 接入 ContextCompressor 实现实际压缩（FROZEN→TRIMMED→SUMMARIZED）
    TODO(阶段4): 接入 StaticRuleLoader 分层加载 CHACHA.md
    TODO(阶段4): 接入 MemoryManager 加载 MEMORY.md + Auto Dream 清洗
    TODO(阶段4): 接入 TokenCounter 精确计数，替换 _estimate_tokens 粗略估算
    """

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

        # 静态块缓存：source → cached ContextBlock
        self._block_cache: Dict[str, ContextBlock] = {}

    # ====== 公开接口 ======

    def assemble(
        self,
        state: ConversationState,
        session_id: str = "",
        project_id: str = "",
        static_rules: Optional[str] = None,   # CHACHA.md 内容
        skills: Optional[str] = None,          # 技能定义
        memory_manager: Optional[Any] = None,  # MemoryManager（自动加载索引）
    ) -> AssembledContext:
        """从会话状态组装上下文。

        static_rules / skills 由 StaticRuleLoader / SkillLoader 提供。
        memory_manager 传入时自动加载 MEMORY.md 索引（autoDream 产物）。
        """
        t0 = time.monotonic()
        blocks: List[ContextBlock] = []

        # 1. 系统提示（protected，缓存优先）
        blocks.append(self._cached_block(
            BlockSource.SYSTEM_PROMPT, "system", self._system_prompt,
            zone="protected", priority=0, importance=1.0, ttl=600,
        ))

        # 2. 静态规则 CHACHA.md（protected）
        if static_rules:
            blocks.append(self._cached_block(
                BlockSource.STATIC_RULE, "system", static_rules,
                zone="protected", priority=1, importance=0.9, ttl=600,
            ))

        # 3. 技能定义（protected）
        if skills:
            blocks.append(self._cached_block(
                BlockSource.SKILL, "system", skills,
                zone="protected", priority=1, importance=0.9, ttl=1200,
            ))

        # 4. 记忆 MEMORY.md 索引（autoDream 构建的轻量索引，可通过 enable_memory_injection 关闭）
        if memory_manager and self._config.enable_memory_injection:
            try:
                index = memory_manager.read()
                if index:
                    blocks.append(ContextBlock(
                        source=BlockSource.MEMORY, role="system",
                        content=index[:self._memory_max_lines * 80],
                        zone="dynamic", priority=2, importance_score=0.85,
                    ))
            except Exception:
                pass

        # 5. 对话历史 + 工具结果（按 priority 排序）
        current_tool_idx = 0
        for event in state.events:
            if isinstance(event, MessageEvent):
                blocks.append(ContextBlock(
                    source=BlockSource.HISTORY,
                    role=event.role,
                    content=event.content,
                    zone="dynamic",
                    priority=3,
                    importance_score=self._history_importance(len(blocks)),
                    token_count=self._estimate_tokens(event.content),
                ))
            elif isinstance(event, ToolCallEvent):
                pass  # tool_call 本身不注入上下文，结果才有价值
            elif isinstance(event, ObservationEvent):
                current_tool_idx += 1
                blocks.append(ContextBlock(
                    source=BlockSource.TOOL_RESULT,
                    role="tool",
                    content=event.content,
                    zone="dynamic",
                    priority=4,
                    importance_score=0.5,
                    token_count=self._estimate_tokens(event.content),
                ))

        # 6. 计算统计
        total_tokens = sum(b.token_count for b in blocks)
        protected_tokens = sum(b.token_count for b in blocks if b.zone == "protected")
        dynamic_tokens = total_tokens - protected_tokens

        utilization = min(2.0, total_tokens / self._budget) if self._budget > 0 else 0.0
        needs_compression = utilization > self._trigger_ratio
        pressure = min(1.0, utilization * 1.25)  # 压力略高于利用率
        trigger_reason = TriggerReason.THRESHOLD if needs_compression else TriggerReason.NONE
        recommended = self._recommend_compression(pressure) if needs_compression else CompressionLevel.NONE

        # 7. 来源分布
        source_dist: Dict[str, int] = {}
        for b in blocks:
            s = str(b.source)
            source_dist[s] = source_dist.get(s, 0) + b.token_count

        meta = ContextAssemblyMeta(
            session_id=session_id,
            project_id=project_id,
            trigger="compression" if needs_compression else "normal",
            total_tokens=total_tokens,
            protected_tokens=protected_tokens,
            dynamic_tokens=dynamic_tokens,
            budget_per_request=self._budget,
            utilization_ratio=utilization,
            compression_pressure=pressure,
            trigger_reason=trigger_reason,
            blocks_by_source=source_dist,
        )

        ctx = AssembledContext(
            meta=meta, blocks=blocks,
            needs_compression=needs_compression,
            recommended_level=recommended,
        )

        # 8. 遥测
        if self._telemetry:
            self._telemetry.agent.record_context(
                total_tokens=total_tokens,
                utilization=utilization,
                compression_triggered=needs_compression,
            )

        logger.debug("上下文组装完成: %d blocks, %d tokens (利用率 %.1f%%, 压缩=%s)",
                     len(blocks), total_tokens, utilization * 100, needs_compression)
        return ctx

    def get_messages(self, state: ConversationState) -> List[Dict[str, Any]]:
        """便捷方法：从 ConversationState 直接获取 LLM 格式消息（不经组装）。"""
        return state.get_messages_for_llm()

    # ====== 内部 ======

    def _cached_block(
        self, source: BlockSource, role: str, content: str,
        zone: str, priority: int, importance: float, ttl: int,
    ) -> ContextBlock:
        """获取缓存的静态块。缓存命中时复用，否则创建新块。"""
        cache_key = str(source)
        cached = self._block_cache.get(cache_key)
        now = time.time()

        if cached and cached.content == content:
            age = now - cached.created_at.timestamp()
            if age < ttl:
                return cached

        block = ContextBlock(
            source=source, role=role, content=content,
            zone=zone, priority=priority,  # type: ignore
            importance_score=importance, cache_ttl=ttl,
            token_count=self._estimate_tokens(content),
        )
        self._block_cache[cache_key] = block
        return block

    def _recommend_compression(self, pressure: float) -> CompressionLevel:
        """根据压缩压力推荐层级。"""
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
        """基于位置计算对话历史的重要性（越新越高）。"""
        return max(0.3, 1.0 - position * 0.05)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（≈ 字符数 / 4）。"""
        return max(1, len(text) // 4)

    # ====== 查询 ======

    def set_system_prompt(self, prompt: str) -> None:
        """替换默认系统提示。"""
        self._system_prompt = prompt
        self._block_cache.pop(str(BlockSource.SYSTEM_PROMPT), None)

    def clear_cache(self) -> None:
        self._block_cache.clear()
