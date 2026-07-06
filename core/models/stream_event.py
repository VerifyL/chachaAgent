"""
流式事件类型体系 (Pydantic Discriminated Union)

替代裸 dict，提供全链路类型安全的流式通道。
替代已删除的 OrchResponse（同步路径）。
"""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ── 事件类型枚举 ──

class StreamEventType(str, Enum):
    """流式事件类型枚举，替代字符串魔法值"""
    TEXT = "text"
    REASONING = "reasoning"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_EXEC_START = "tool_exec_start"
    TOOL_EXEC_END = "tool_exec_end"
    DONE = "done"
    ERROR = "error"
    COMPACT = "compact"


# ── 各事件模型 ──

class TextEvent(BaseModel):
    """LLM 输出的文本片段（增量）"""
    type: Literal[StreamEventType.TEXT] = StreamEventType.TEXT
    content: str


class ReasoningEvent(BaseModel):
    """推理过程文本（DeepSeek-R1 / o1 等思考链模型）"""
    type: Literal[StreamEventType.REASONING] = StreamEventType.REASONING
    content: str


class ToolCallStartEvent(BaseModel):
    """LLM 决定调用某工具"""
    type: Literal[StreamEventType.TOOL_CALL_START] = StreamEventType.TOOL_CALL_START
    tool_name: str
    tool_index: int


class ToolCallEndEvent(BaseModel):
    """单个工具参数流式接收完毕"""
    type: Literal[StreamEventType.TOOL_CALL_END] = StreamEventType.TOOL_CALL_END
    tool_index: int


class ToolExecStartEvent(BaseModel):
    """工具开始执行（含解析后的参数摘要）"""
    type: Literal[StreamEventType.TOOL_EXEC_START] = StreamEventType.TOOL_EXEC_START
    tool_name: str
    args: str = ""


class ToolExecEndEvent(BaseModel):
    """工具执行完毕（含结果预览）"""
    type: Literal[StreamEventType.TOOL_EXEC_END] = StreamEventType.TOOL_EXEC_END
    tool_name: str
    preview: str = ""


class DoneEvent(BaseModel):
    """流式响应结束"""
    type: Literal[StreamEventType.DONE] = StreamEventType.DONE
    text: str = ""
    tokens: int = 0
    usage: dict = Field(default_factory=dict)


class ErrorEvent(BaseModel):
    """错误事件"""
    type: Literal[StreamEventType.ERROR] = StreamEventType.ERROR
    message: str


class CompactEvent(BaseModel):
    """上下文自动压缩通知"""
    type: Literal[StreamEventType.COMPACT] = StreamEventType.COMPACT
    reason: str


# ── 联合类型（Pydantic v2 discriminated union）──

StreamEvent = Annotated[
    Union[
        TextEvent,
        ReasoningEvent,
        ToolCallStartEvent,
        ToolCallEndEvent,
        ToolExecStartEvent,
        ToolExecEndEvent,
        DoneEvent,
        ErrorEvent,
        CompactEvent,
    ],
    Field(discriminator="type"),
]
