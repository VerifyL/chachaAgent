"""
core/models/context.py
上下文组装结果模型：ContextAssembler 的输出，ContextCompressor 的输入。

设计理念（融合 + Harness 工程指南）：
1. 压缩骨架：7层渐进式压缩（FROZEN→TRIMMED→SUMMARIZED→CONSOLIDATED），
   通过显式化的 compression_pressure 参数化，变成可观测、可调参。
2. DYNAMIC_BOUNDARY 策略：protected 区永不被截断（系统提示+当前任务），
   dynamic 区按 importance_score 排序，压缩时优先删低分块。
3. 三级预算：per_request / per_task / 推理预算，分离追踪。
4. 缓存感知：静态块（系统提示、工具定义）带 cache_ttl，减少重复计算。
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ========================= 1. 枚举定义 =========================


class BlockSource(str, Enum):
    """上下文块的来源分类"""

    SYSTEM_PROMPT = "system_prompt"  # 系统提示词（always first, protected）
    STATIC_RULE = "static_rule"  # CHACHA.md 静态规范（分层加载：~/.chacha/ → 项目/ → 子目录/）
    SKILL = "skill"  # 技能/能力定义（内置技能、OpenClaw 技能、工具 schema）
    MEMORY = "memory"  # MEMORY.md 记忆内容（启动预注入 + 主题按需加载）
    HISTORY = "history"  # 对话历史
    TOOL_RESULT = "tool_result"  # 工具执行结果（文件读、命令执行等）
    RAG_RESULT = "rag_result"  # Code-RAG 检索结果（LanceDB 语义搜索 + Tree-sitter 符号图）
    SUBAGENT_RESULT = "subagent_result"  # 子Agent 任务完成汇总
    ADDITIONAL_CONTEXT = "additional_context"  # 钩子注入的额外消息


class CompressionLevel(str, Enum):
    """
    渐进式压缩层级。

    NONE          → 未压缩（原始内容）
    FROZEN        → 冻结：工具输出只保留前N行+退出码+状态，零 LLM 成本
    TRIMMED       → 修剪：规则引擎去冗余（空行、格式符、截断超长），不调 LLM
    SUMMARIZED    → 摘要：LLM 生成语义摘要，保留关键信息，丢弃细节
    CONSOLIDATED  → 整合：多条相关块合并为一条，最低 token 成本（记忆整合用）
    """

    NONE = "none"
    FROZEN = "frozen"
    TRIMMED = "trimmed"
    SUMMARIZED = "summarized"
    CONSOLIDATED = "consolidated"


class TriggerReason(str, Enum):
    """压缩触发原因"""

    NONE = "none"  # 未触发
    THRESHOLD = "threshold"  # 超过 compression_trigger_ratio
    TIME_GATE = "time_gate"  # 时间门：距上次压缩超过 N 小时
    SESSION_GATE = "session_gate"  # 会话门：累计 N 个会话未压缩
    MANUAL = "manual"  # 用户/钩子显式触发


# ========================= 2. 上下文块 =========================


class ContextBlock(BaseModel):
    """
    一个上下文片段。

    生命周期：
      - 由 ContextAssembler 创建（NONE 状态）
      - 经 ContextCompressor 渐进式压缩（FROZEN→TRIMMED→SUMMARIZED→CONSOLIDATED）
      - 最终由 get_messages() 转为 LLM API 格式

    TODO(阶段4): ContextAssembler 从 StaticRuleLoader/MemoryManager/RAG 收集块
    TODO(阶段4): ContextCompressor 实现 FROZEN/TRIMMED/SUMMARIZED 压缩策略
    TODO(阶段4): persisted_path 的实际读写逻辑（写入 .chacha/compressed/）
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    id: str = Field(default_factory=lambda: str(uuid4()), description="块唯一标识")

    # ---------- 内容 ----------
    source: BlockSource = Field(..., description="内容来源")
    role: str = Field(..., description="LLM 消息角色: system | user | assistant | tool")
    content: str = Field(..., description="文本内容")

    # ---------- 空间管理（DYNAMIC_BOUNDARY）----------
    zone: Literal["protected", "dynamic"] = Field(
        "dynamic", description="保护区永不被截断（系统提示+当前任务），动态区按重要性压缩"
    )
    priority: int = Field(0, ge=0, description="组装排序优先级（0最高，数字越大越靠后）")

    # ---------- 压缩追踪 ----------
    compression_level: CompressionLevel = Field(
        CompressionLevel.NONE, description="当前压缩层级，从 NONE 渐进到 CONSOLIDATED"
    )
    original_token_count: Optional[int] = Field(
        None, ge=0, description="压缩前 token 数（FROZEN/TRIMMED/SUMMARIZED 时记录，用于审计）"
    )
    token_count: int = Field(0, ge=0, description="当前 token 数")

    # ---------- 原文存档（占位引用模式）----------
    persisted_path: Optional[str] = Field(
        None, description="压缩后原文存档路径（如 compressed/s1/b-abc.json），content 中仅保留占位引用"
    )

    # ---------- 冻结保留详情（选择性保留）----------
    frozen_kept_lines: Optional[int] = Field(None, ge=0, description="冻结时保留的行数（含 error/warning/首尾关键行）")
    frozen_total_lines: Optional[int] = Field(None, ge=0, description="冻结前原始总行数")

    # ---------- 防重复压缩 ----------
    compression_history: List[str] = Field(
        default_factory=list,
        description="压缩轨迹（如 ['NONE→FROZEN', 'FROZEN→SUMMARIZED']），ContextCompressor 跳过已到终态者",
    )

    # ---------- 摘要缓存 ----------
    content_hash: Optional[str] = Field(None, description="原文 SHA256 哈希（仅 NONE 状态有效），用于复用已有压缩结果")

    # ---------- 压缩决策（显式化 内部评分逻辑）----------
    importance_score: float = Field(
        0.5, ge=0.0, le=1.0, description="重要性评分（recency×0.4 + relevance×0.4 + initial×0.2），压缩时优先删低分块"
    )

    # ---------- 缓存感知（Harness 静态/动态分离）----------
    cache_ttl: Optional[int] = Field(
        None, ge=0, description="缓存有效期（秒），静态块（系统提示）设 300-600，动态块设 None 不缓存"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))),
        description="创建时间（用于时间门触发和 recency 计算）",
    )

    # ---------- 校验 ----------
    @model_validator(mode="after")
    def _validate_protected_no_compression(self) -> "ContextBlock":
        """保护区的块不应该被压缩（防御性校验）"""
        if self.zone == "protected" and self.compression_level != CompressionLevel.NONE:
            # 不抛异常，只记录不一致（由 ContextCompressor 负责保证）
            pass
        return self

    @model_validator(mode="after")
    def _validate_original_token_count(self) -> "ContextBlock":
        """压缩后应有原始 token 数记录"""
        if self.compression_level != CompressionLevel.NONE and self.original_token_count is None:
            # 降级：用当前 token_count 作为近似（仅限 FROZEN）
            pass
        return self


# ========================= 3. 组装元信息 =========================


class ContextAssemblyMeta(BaseModel):
    """上下文组装的元信息"""

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    session_id: Optional[str] = Field(None, description="会话 ID")
    project_id: Optional[str] = Field(None, description="项目 ID")
    assembled_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))), description="组装时间"
    )

    # ---------- 组装触发类型 ----------
    trigger: str = Field("normal", description="组装触发类型: normal | compression | first_turn | recovery")

    # ---------- Token 统计 ----------
    total_tokens: int = Field(0, ge=0, description="所有 blocks 的 token 总和")
    protected_tokens: int = Field(0, ge=0, description="保护区 token 数")
    dynamic_tokens: int = Field(0, ge=0, description="动态区 token 数")

    # ---------- 三级预算（Harness 指南 + 窗口上限）----------
    budget_per_request: int = Field(128000, ge=1, description="单次 LLM 调用的 token 上限（含输入输出）")
    budget_per_task: Optional[int] = Field(None, ge=1, description="任务级 token 预算（含多次调用），为空则不限")

    # ---------- 压缩决策 ----------
    utilization_ratio: float = Field(0.0, ge=0.0, le=2.0, description="total / budget_per_request，>1 表示已超限")
    compression_pressure: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="""
压缩激进程度。驱动层级跃迁:
  <0.5     → 不压缩
  0.5~0.7  → FROZEN（冻结工具输出）
  0.7~0.85 → TRIMMED（规则修剪）
  0.85~0.95 → SUMMARIZED（LLM摘要）
  >0.95    → CONSOLIDATED（记忆整合）
""",
    )
    trigger_reason: TriggerReason = Field(TriggerReason.NONE, description="当前压缩触发原因")

    # ---------- 推理预算（思考预算分离追踪）----------
    reasoning_budget_tokens: int = Field(0, ge=0, description="思考 token 配额（0 表示无限制/不使用）")
    reasoning_tokens_used: int = Field(0, ge=0, description="当前会话累计思考 token 消耗")

    # ---------- 来源分布分析 ----------
    blocks_by_source: Dict[str, int] = Field(
        default_factory=dict, description="各来源的 token 分布，格式: {source: token_count}"
    )


# ========================= 4. 上下文组装结果 =========================


class AssembledContext(BaseModel):
    """
    上下文组装结果主体。

    由 ContextAssembler 产出 → ContextManager 做压缩决策 → LLMInvoker 格式化调用。
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    meta: ContextAssemblyMeta = Field(default_factory=ContextAssemblyMeta, description="组装元信息")
    blocks: List[ContextBlock] = Field(default_factory=list, description="按 priority 排序的上下文片段列表")

    needs_compression: bool = Field(False, description="utilization_ratio > trigger_ratio 时为 True")
    recommended_level: CompressionLevel = Field(
        CompressionLevel.NONE, description="基于 compression_pressure 推荐的压缩层级"
    )

    # ---------- 便捷方法 ----------

    def get_messages(self) -> List[Dict[str, Any]]:
        """将所有 blocks 转为 LLM API（OpenAI/Anthropic）格式的消息列表。

        仅返回 content 非空的 block，按 priority 排序。
        """
        messages: List[Dict[str, Any]] = []
        for block in sorted(self.blocks, key=lambda b: b.priority):
            if block.content.strip():
                messages.append({"role": block.role, "content": block.content})
        return messages

    def get_protected_slice(self) -> List[ContextBlock]:
        """仅返回保护区（zone=protected）的 blocks。"""
        return [b for b in self.blocks if b.zone == "protected"]

    def get_dynamic_slice(self) -> List[ContextBlock]:
        """仅返回动态区（zone=dynamic）的 blocks，按 importance_score 降序。"""
        return sorted(
            [b for b in self.blocks if b.zone == "dynamic"],
            key=lambda b: b.importance_score,
            reverse=True,
        )

    def get_statistics(self) -> str:
        """人类可读的统计摘要。"""
        stat_parts = [
            f"总 Token: {self.meta.total_tokens} / {self.meta.budget_per_request}",
            f"利用率: {self.meta.utilization_ratio:.1%}",
            f"保护区: {self.meta.protected_tokens} | 动态区: {self.meta.dynamic_tokens}",
            f"压缩压力: {self.meta.compression_pressure:.2f} → {self.recommended_level.value}",
            f"触发原因: {self.meta.trigger_reason.value}",
        ]
        if self.meta.reasoning_tokens_used:
            stat_parts.append(f"思考: {self.meta.reasoning_tokens_used} / {self.meta.reasoning_budget_tokens}")
        if self.meta.blocks_by_source:
            distribution = " | ".join(f"{src}: {tokens}" for src, tokens in self.meta.blocks_by_source.items())
            stat_parts.append(f"分布: {distribution}")
        return "\n".join(stat_parts)

    @classmethod
    def empty(cls) -> "AssembledContext":
        """创建空上下文结果。"""
        return cls()
