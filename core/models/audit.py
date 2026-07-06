"""
core/models/audit.py
审计日志模型：不可变审计事件，用于安全合规追踪。

设计原则：
1. 与会话事件（session.py）分离 — 会话事件驱动对话状态，审计事件专注安全/合规
2. 所有审计事件不可变（frozen），带 UTC 时间戳
3. 原生支持 JSONL 序列化（一行一条记录）
4. 内建敏感信息脱敏标记（SensitiveString）
5. 事件分类覆盖：工具调用、记忆变更、成本、权限、会话生命周期、模型调用
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ========================= 1. 敏感信息处理 =========================

class SensitiveString(BaseModel):
    """敏感字符串包装器：存储原文，序列化时自动脱敏。"""
    model_config = ConfigDict(frozen=True)

    value: str
    _masked: str = ""

    def model_dump(self, **kwargs) -> str:
        """序列化时返回脱敏后的值"""
        return self.masked

    def model_dump_json(self, **kwargs) -> str:
        """JSON 序列化键名由外部控制，此处仅返回字符串"""
        return '"[REDACTED]"'

    @property
    def masked(self) -> str:
        if len(self.value) <= 4:
            return "[REDACTED]"
        return self.value[:2] + "*" * (len(self.value) - 4) + self.value[-2:]

    def __str__(self) -> str:
        return self.masked

    def __repr__(self) -> str:
        return f"SensitiveString(masked={self.masked!r})"


# ========================= 2. 审计事件分类 =========================

class AuditEventCategory(str, Enum):
    """审计事件分类枚举"""
    TOOL_CALL = "tool_call"                # 工具调用（含参数摘要、结果状态）
    MEMORY_CHANGE = "memory_change"        # 记忆变更（读写删）
    COST = "cost"                          # 成本记录（token 消耗、金额）
    PERMISSION = "permission"              # 权限审批（请求/结果/缓存命中）
    SESSION = "session"                    # 会话生命周期（开始/结束/检查点）
    MODEL_CALL = "model_call"              # 模型调用（请求/响应摘要）
    CONFIG_CHANGE = "config_change"        # 配置变更（热重载）
    SYSTEM = "system"                      # 系统事件（启动/关闭/环境校验）


# ========================= 3. 审计事件基类 =========================

class AuditEvent(BaseModel):
    """审计事件基类（不可变）—— JSONL 中每个对象均为一条完整审计记录。"""
    model_config = ConfigDict(
        frozen=True,
        use_enum_values=True,
    )

    id: str = Field(default_factory=lambda: str(uuid4()), description="事件唯一 ID")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))),
        description="事件发生时间（UTC）",
    )
    category: AuditEventCategory = Field(..., description="事件分类")
    session_id: Optional[str] = Field(None, description="所属会话 ID")
    project_id: Optional[str] = Field(None, description="所属项目 ID")

    def to_jsonl(self) -> str:
        """序列化为 JSONL 一行（不含换行符，调用方负责写入 + 换行）。"""
        return self.model_dump_json()


# ========================= 4. 具体审计事件类型 =========================

# ---------- 4.1 工具调用 ----------

class ToolCallAuditEvent(AuditEvent):
    """工具调用审计"""
    category: AuditEventCategory = Field(default=AuditEventCategory.TOOL_CALL, frozen=True)

    tool_name: str = Field(..., description="工具名称")
    tool_use_id: str = Field(..., description="工具调用唯一 ID")
    arguments_summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="参数摘要（已脱敏后的键值对）"
    )
    status: Literal["success", "error", "blocked"] = Field(..., description="执行结果状态")
    error_message: Optional[str] = Field(None, description="错误信息（失败时）")
    duration_ms: Optional[int] = Field(None, description="执行耗时（毫秒）")
    output_truncated: bool = Field(False, description="输出是否被截断")
    blocked_by_policy: Optional[str] = Field(None, description="被哪个策略拦截（如 command_blacklist）")

    @staticmethod
    def sanitize_arguments(args: Dict[str, Any]) -> Dict[str, Any]:
        """对工具参数做脱敏：将已知敏感 key 的值替换为占位符。"""
        sensitive_keys = {"api_key", "password", "token", "secret", "authorization"}
        sanitized = {}
        for k, v in args.items():
            if k.lower() in sensitive_keys:
                sanitized[k] = "[REDACTED]"
            elif isinstance(v, str) and len(v) > 200:
                sanitized[k] = v[:200] + "..."
            else:
                sanitized[k] = v
        return sanitized


# ---------- 4.2 成本记录 ----------

class CostAuditEvent(AuditEvent):
    """成本审计"""
    category: AuditEventCategory = Field(default=AuditEventCategory.COST, frozen=True)

    model_name: str = Field(..., description="使用的模型名称")
    provider: str = Field(..., description="模型提供商")
    input_tokens: int = Field(0, ge=0, description="输入 token 数")
    output_tokens: int = Field(0, ge=0, description="输出 token 数")
    cost_usd: float = Field(0.0, ge=0, description="本次调用成本（美元）")
    cumulative_cost_usd: float = Field(0.0, ge=0, description="会话累计成本（美元）")
    cost_limit_usd: Optional[float] = Field(None, description="配置的成本上限")
    circuit_breaker_triggered: bool = Field(False, description="是否触发熔断")


# ---------- 4.3 记忆变更 ----------

class MemoryChangeAuditEvent(AuditEvent):
    """记忆变更审计"""
    category: AuditEventCategory = Field(default=AuditEventCategory.MEMORY_CHANGE, frozen=True)

    operation: Literal["read", "write", "delete", "prune", "auto_clean"] = Field(
        ..., description="操作类型"
    )
    file_path: str = Field(..., description="变动的记忆文件路径（相对 .chacha/memory）")
    change_summary: Optional[str] = Field(None, description="变更内容摘要（最大 500 字符）")
    lines_before: Optional[int] = Field(None, description="变更前行数")
    lines_after: Optional[int] = Field(None, description="变更后行数")


# ---------- 4.4 权限审批 ----------

class PermissionAuditEvent(AuditEvent):
    """权限审批审计"""
    category: AuditEventCategory = Field(default=AuditEventCategory.PERMISSION, frozen=True)

    request_id: str = Field(..., description="审批请求 ID")
    tool_name: str = Field(..., description="请求的工具名称")
    command_or_action: str = Field(..., description="请求的命令或操作")
    reason: str = Field(..., description="发起审批的原因")
    approved: Optional[bool] = Field(None, description="审批结果（None=待审批）")
    cache_hit: bool = Field(False, description="是否命中审批缓存")
    cache_ttl_seconds: Optional[int] = Field(None, description="缓存有效期（秒）")


# ---------- 4.5 会话生命周期 ----------

class SessionAuditEvent(AuditEvent):
    """会话审计"""
    category: AuditEventCategory = Field(default=AuditEventCategory.SESSION, frozen=True)

    event: Literal["started", "ended", "checkpoint_created", "checkpoint_restored", "resumed"] = Field(
        ..., description="会话事件类型"
    )
    parent_session_id: Optional[str] = Field(None, description="父会话 ID（子 Agent 场景）")
    checkpoint_id: Optional[str] = Field(None, description="检查点 ID")
    total_tokens_at_event: Optional[int] = Field(None, description="事件时的累计 token")
    total_cost_at_event: Optional[float] = Field(None, description="事件时的累计成本")
    duration_ms_at_event: Optional[int] = Field(None, description="事件时的累计耗时")


# ---------- 4.6 模型调用 ----------

class ModelCallAuditEvent(AuditEvent):
    """模型调用审计（不含完整 prompt/response，仅元数据）"""
    category: AuditEventCategory = Field(default=AuditEventCategory.MODEL_CALL, frozen=True)

    model_name: str = Field(..., description="模型名称")
    provider: str = Field(..., description="模型提供商")
    prompt_tokens: int = Field(0, ge=0, description="prompt token 数")
    completion_tokens: int = Field(0, ge=0, description="completion token 数")
    latency_ms: int = Field(0, ge=0, description="调用延迟（毫秒）")
    retry_count: int = Field(0, ge=0, description="重试次数")
    status: Literal["success", "error", "retry", "rate_limited"] = Field(..., description="调用状态")


# ========================= 5. 联合类型 =========================

AuditRecord = Union[
    ToolCallAuditEvent,
    CostAuditEvent,
    MemoryChangeAuditEvent,
    PermissionAuditEvent,
    SessionAuditEvent,
    ModelCallAuditEvent,
    AuditEvent,  # 含自定义 category 的系统/配置事件
]


# ========================= 6. 便捷工厂函数 =========================

def audit_factory(
    category: AuditEventCategory,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    **event_fields,
) -> AuditRecord:
    """根据 category 自动创建对应类型的审计事件。

    示例:
        evt = audit_factory(
            session_id="s1",
            project_id="p1",
            tool_name="edit",
            tool_use_id="call_2",
            arguments={"path": "foo.py", "old_string": "x", "new_string": "y"},
            status="success",
        )
    """
    category_to_cls: Dict[AuditEventCategory, type] = {
        AuditEventCategory.TOOL_CALL: ToolCallAuditEvent,
        AuditEventCategory.COST: CostAuditEvent,
        AuditEventCategory.MEMORY_CHANGE: MemoryChangeAuditEvent,
        AuditEventCategory.PERMISSION: PermissionAuditEvent,
        AuditEventCategory.SESSION: SessionAuditEvent,
        AuditEventCategory.MODEL_CALL: ModelCallAuditEvent,
    }

    cls = category_to_cls.get(category)
    if cls is None:
        # 未注册的分类（如 SYSTEM、CONFIG_CHANGE）使用基类并自动填充 category
        return AuditEvent(
            category=category,
            session_id=session_id,
            project_id=project_id,
            **event_fields,
        )
    return cls(
        session_id=session_id,
        project_id=project_id,
        **event_fields,
    )
