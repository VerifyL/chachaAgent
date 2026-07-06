# core/models/session.py
"""
会话状态模型：会话元数据、不可变事件日志、Agent循环运行时状态、检查点。

设计原则：
1. 所有组件（agents, tools, LLMs）都是不可变的 Pydantic 模型。
2. 唯一可变实体是 ConversationState，集中管理整个会话。
3. 事件是不可变的、仅追加的日志，支持完整回放与审计。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ========================= 1. 基础类型定义 =========================


class Attachment(BaseModel):
    """消息附件（预留多模态内容）
    TODO(v1.5): get_messages_for_llm() 目前仅处理文本，需扩展为支持多模态消息格式
    """

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    type: Literal["image", "audio", "file"] = "image"
    data: bytes
    mime_type: str = "application/octet-stream"
    filename: Optional[str] = None

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, v: str) -> str:
        allowed_prefixes = ("image/", "audio/", "text/", "application/")
        if not any(v.startswith(prefix) for prefix in allowed_prefixes):
            raise ValueError(f"Unsupported MIME type: {v}")
        return v


# ========================= 2. 事件类型（不可变日志） =========================


class BaseEvent(BaseModel):
    """所有事件的基类（不可变）"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))))
    source: Literal["user", "agent", "tool", "system"]


class MessageEvent(BaseEvent):
    role: Literal["user", "assistant", "system"]
    content: str
    attachments: List[Attachment] = Field(default_factory=list)


class ToolCallEvent(BaseEvent):
    tool_name: str
    arguments: Dict[str, Any]
    tool_use_id: str
    thought: Optional[str] = None


class ObservationEvent(BaseEvent):
    tool_use_id: str
    content: str
    status: Literal["success", "error"]
    error: Optional[str] = None
    truncated: bool = False
    cache_key: Optional[str] = None
    duration_ms: Optional[int] = None


class PermissionRequestEvent(BaseEvent):
    request_id: str
    tool_name: str
    command_or_action: str
    reason: str
    approved: Optional[bool] = None


class CompactEvent(BaseEvent):
    before_token_count: int
    after_token_count: int
    summary: Optional[str] = None


class CheckpointEvent(BaseEvent):
    checkpoint_id: str
    description: Optional[str] = None


SessionEvent = MessageEvent | ToolCallEvent | ObservationEvent | PermissionRequestEvent | CompactEvent | CheckpointEvent


# ========================= 3. Agent Loop 运行时状态 =========================


class AgentLoopState(BaseModel):
    model_config = ConfigDict(frozen=False)

    iteration: int = 0
    pending_tool_calls: List[ToolCallEvent] = Field(default_factory=list)
    waiting_for: Optional[Literal["permission", "tool_result"]] = None
    waiting_for_id: Optional[str] = None
    tool_results_cache: Dict[str, ObservationEvent] = Field(default_factory=dict)


# ========================= 4. 会话元数据与完整状态 =========================


class SessionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(default_factory=lambda: str(uuid4()))
    parent_session_id: Optional[str] = None
    project_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))))

    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0


class SessionCheckpoint(BaseModel):
    checkpoint_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))))
    description: Optional[str] = None
    event_index: int
    metadata_snapshot: SessionMetadata
    loop_state_snapshot: AgentLoopState


class ConversationState(BaseModel):
    model_config = ConfigDict(frozen=False)

    metadata: SessionMetadata
    events: List[SessionEvent] = Field(default_factory=list)
    loop_state: AgentLoopState = Field(default_factory=AgentLoopState)
    checkpoints: List[SessionCheckpoint] = Field(default_factory=list)

    def add_event(self, event: SessionEvent) -> None:
        """追加不可变事件到日志，并返回新的 metadata（因为 SessionMetadata 是冻结的）"""
        self.events.append(event)
        self.metadata = self.metadata.model_copy(update={"updated_at": datetime.now(tz=timezone(timedelta(hours=8)))})

    def update_metadata(self, **kwargs) -> None:
        """更新元数据字段，返回新的 metadata 实例"""
        self.metadata = self.metadata.model_copy(update=kwargs)
        self.metadata = self.metadata.model_copy(update={"updated_at": datetime.now(tz=timezone(timedelta(hours=8)))})

    def get_messages_for_llm(self) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for event in self.events:
            if isinstance(event, MessageEvent):
                messages.append(
                    {
                        "role": event.role,
                        "content": event.content,
                    }
                )
            elif isinstance(event, ToolCallEvent):
                messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": event.tool_use_id,
                                "type": "function",
                                "function": {
                                    "name": event.tool_name,
                                    "arguments": event.arguments,
                                },
                            }
                        ],
                    }
                )
            elif isinstance(event, ObservationEvent):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": event.tool_use_id,
                        "content": event.content,
                    }
                )
        return messages
