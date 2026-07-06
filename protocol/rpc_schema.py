"""
protocol/rpc_schema.py
JSON-RPC 2.0 消息模型 —— ChachaAgent 的统一通信协议。

用途：
  所有组件（CLI/Web 前端、Gateway、Orchestrator、LLMInvoker、ToolExecutor）
  通过本模块定义的消息格式进行通信。Gateway 负责路由，不解析业务 payload。

设计原则：
  1. 严格遵循 JSON-RPC 2.0 规范（jsonrpc/id/method/params/error）
  2. GatewayMessage 为外层包装，承载 seq/project_id/session_id 路由信息
  3. 事件（Event）无 id 字段，表示服务端单向推送
  4. 多模态预留：union 类型中用注释标注 ImageChunk/AudioChunk 扩展点 (v1.5+)

消息类型总览：
  ┌─ GatewayMessage（网关包装）
  │    ├─ RPCRequest     —— 客户端→服务端请求
  │    ├─ RPCResponse    —— 服务端→客户端响应
  │    └─ RPCEvent       —— 服务端单向推送
  │         ├─ TokenChunkEvent      流式文本输出
  │         ├─ ToolStatusEvent      工具执行状态
  │         ├─ PermissionRequestEvent 权限请求
  │         ├─ AuditTrailEvent      审计事件
  │         ├─ SessionLifecycleEvent 会话生命周期
  │         └─ SystemNotificationEvent 系统通知
  └─ PermissionResponse  —— 权限审批响应（嵌入 RPCResponse.result）
"""

from typing import Any, Dict, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.models.audit import AuditRecord

# ========================= 1. 基础类型 =========================


class RPCError(BaseModel):
    """JSON-RPC 2.0 错误对象"""

    code: int = Field(..., description="错误码（-32700 至 -32000 为标准协议码）")
    message: str = Field(..., description="人类可读的错误描述")
    data: Optional[Any] = Field(None, description="附加错误信息")


# ========================= 2. 网关层包装 =========================


class GatewayMessage(BaseModel):
    """
    网关层消息包装，承载路由信息。

    seq 自增保证全局有序，project_id + session_id 用于会话复用和多项目隔离。
    payload 为 JSON-RPC 2.0 消息，Gateway 按 method 路由，不解析 params 细节。
    """

    model_config = ConfigDict(use_enum_values=True)

    seq: int = Field(..., ge=0, description="全局自增序列号，Gateway 分配")
    project_id: Optional[str] = Field(None, description="项目 ID，用于多项目隔离")
    session_id: Optional[str] = Field(None, description="会话 ID，用于会话复用")

    payload: Union[
        "RPCRequest",
        "RPCResponse",
        "RPCEvent",
    ] = Field(..., description="JSON-RPC 2.0 消息体")

    # ==== 多模态预留 (v1.5+) ====
    # payload 的 union 类型可扩展为：
    #   Union[RPCRequest, RPCResponse, RPCEvent, ImageChunk, AudioChunk]
    # ImageChunk: {type: "image", data: bytes, mime_type: str, width?: int, height?: int}
    # AudioChunk: {type: "audio", data: bytes, mime_type: str, duration_ms?: int}


# ========================= 3. JSON-RPC 2.0 消息基类 =========================


class RPCRequest(BaseModel):
    """JSON-RPC 2.0 请求"""

    model_config = ConfigDict(extra="allow")

    jsonrpc: str = Field(default="2.0", frozen=True, description="JSON-RPC 版本")
    id: str = Field(default_factory=lambda: str(uuid4()), description="请求唯一 ID")
    method: str = Field(..., description="调用方法名（如 user/message、tool/execute）")
    params: Optional[Dict[str, Any]] = Field(default_factory=dict, description="方法参数")


class RPCResponse(BaseModel):
    """JSON-RPC 2.0 响应（result 和 error 互斥）"""

    model_config = ConfigDict(extra="allow")

    jsonrpc: str = Field(default="2.0", frozen=True, description="JSON-RPC 版本")
    id: str = Field(..., description="对应请求的 ID")
    result: Optional[Any] = Field(None, description="成功结果")
    error: Optional[RPCError] = Field(None, description="错误信息")


class RPCEvent(BaseModel):
    """
    JSON-RPC 2.0 通知（服务端单向推送，无 id 字段，无需响应）

    子类通过 method 字段区分事件类型。
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    jsonrpc: str = Field(default="2.0", frozen=True, description="JSON-RPC 版本")
    method: str = Field(..., description="事件方法名（如 stream/token、tool/status）")
    params: Optional[Dict[str, Any]] = Field(default_factory=dict, description="事件参数")


# ========================= 4. 具体事件类型 =========================


class ToolCallDelta(BaseModel):
    """流式输出中 tool_calls 的增量片段"""

    model_config = ConfigDict(frozen=True)

    index: int = Field(0, ge=0, description="tool_call 在列表中的索引")
    id: Optional[str] = Field(None, description="tool_call ID（首次出现时填充）")
    function_name: Optional[str] = Field(None, description="函数名（首次出现时填充）")
    arguments_delta: str = Field("", description="参数的增量 JSON 片段")


class TokenChunkEvent(RPCEvent):
    """
    流式文本输出事件。

    LLMInvoker 每收到一个 token 就推送此事件，前端累积渲染。
    finish_reason=stop 时流结束，finish_reason=tool_calls 时携带 tool_call_delta。
    """

    method: str = Field(default="stream/token", frozen=True, description="事件方法名")
    params: Dict[str, Any] = Field(
        default_factory=lambda: {
            "delta": "",
            "finish_reason": None,
            "tool_call_delta": None,
        },
        description="流式事件参数",
    )

    def set_delta(self, text: str) -> "TokenChunkEvent":
        self.params["delta"] = text
        return self

    def set_finish(self, reason: str) -> "TokenChunkEvent":
        self.params["finish_reason"] = reason
        return self

    def set_tool_call_delta(self, delta: ToolCallDelta) -> "TokenChunkEvent":
        self.params["tool_call_delta"] = delta.model_dump()
        return self

    @model_validator(mode="after")
    def _check_method_token(self) -> "TokenChunkEvent":
        if self.method != "stream/token":
            raise ValueError("Expected method='stream/token'")
        return self


class ToolStatusEvent(RPCEvent):
    """
    工具执行状态变更事件。

    Orchestrator/ToolExecutor 在工具状态变化时推送，前端展示进度条或状态标签。
    """

    method: str = Field(default="tool/status", frozen=True, description="事件方法名")
    params: Dict[str, Any] = Field(
        default_factory=lambda: {
            "tool_use_id": "",
            "tool_name": "",
            "status": "pending",
            "progress": None,
            "duration_ms": None,
            "output_summary": None,
        },
        description="工具状态参数",
    )

    @model_validator(mode="after")
    def _check_method_tool(self) -> "ToolStatusEvent":
        if self.method != "tool/status":
            raise ValueError("Expected method='tool/status'")
        return self


class PermissionRequestEvent(RPCEvent):
    """
    权限请求事件（服务端→客户端）。

    前端展示审批弹窗，用户点击允许/拒绝后返回 PermissionResponse。
    """

    method: str = Field(default="permission/request", frozen=True, description="事件方法名")
    params: Dict[str, Any] = Field(
        default_factory=lambda: {
            "request_id": "",
            "tool_name": "",
            "command_or_action": "",
            "reason": "",
        },
        description="权限请求参数",
    )

    @model_validator(mode="after")
    def _check_method_perm(self) -> "PermissionRequestEvent":
        if self.method != "permission/request":
            raise ValueError("Expected method='permission/request'")
        return self


class PermissionResponse(BaseModel):
    """权限审批响应（客户端→服务端，嵌入 RPCResponse.result）"""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(..., description="对应 PermissionRequestEvent 的 request_id")
    approved: bool = Field(..., description="审批结果")


class AuditTrailEvent(RPCEvent):
    """
    审计事件推送。

    复用 core/models/audit.py 的 AuditRecord，不重复定义字段。
    前端/日志系统接收后写入 audit.jsonl 或展示。
    """

    method: str = Field(default="audit/trail", frozen=True, description="事件方法名")
    audit: AuditRecord = Field(..., description="审计记录")

    @model_validator(mode="after")
    def _check_method_audit(self) -> "AuditTrailEvent":
        if self.method != "audit/trail":
            raise ValueError("Expected method='audit/trail'")
        return self

    def model_dump(self, **kwargs) -> Dict[str, Any]:
        """序列化时展开 audit 到 params，排除独立的 audit 字段"""
        data = super().model_dump(**kwargs)
        data["params"] = self.audit.model_dump(**kwargs)
        data.pop("audit", None)
        return data


class SessionLifecycleEvent(RPCEvent):
    """
    会话生命周期事件。

    会话启停、检查点创建/恢复时推送，Gateway 据此管理会话路由表。
    """

    method: str = Field(default="session/lifecycle", frozen=True, description="事件方法名")
    params: Dict[str, Any] = Field(
        default_factory=lambda: {
            "event": "",
            "session_id": "",
            "project_id": None,
            "parent_session_id": None,
            "checkpoint_id": None,
            "total_tokens": None,
            "total_cost_usd": None,
        },
        description="会话生命周期参数（event: started|ended|checkpoint_created|checkpoint_restored|resumed）",
    )

    @model_validator(mode="after")
    def _check_method_session(self) -> "SessionLifecycleEvent":
        if self.method != "session/lifecycle":
            raise ValueError("Expected method='session/lifecycle'")
        return self


class SystemNotificationEvent(RPCEvent):
    """
    系统通知事件（错误/警告/信息）。

    用于向客户端推送非致命错误或状态提示，如「环境校验失败」「热重载失败」。
    """

    method: str = Field(default="system/notification", frozen=True, description="事件方法名")
    params: Dict[str, Any] = Field(
        default_factory=lambda: {
            "level": "info",
            "message": "",
            "source_module": None,
            "details": None,
        },
        description="系统通知参数（level: info|warning|error）",
    )

    @model_validator(mode="after")
    def _check_method_notify(self) -> "SystemNotificationEvent":
        if self.method != "system/notification":
            raise ValueError("Expected method='system/notification'")
        return self


# ========================= 5. 联合类型 =========================

# 注意：具体事件类型必须在 RPCRequest/RPCResponse 之前，
# 否则 Pydantic 会将带 jsonrpc+method 的事件误判为 RPCRequest。
RPCMessage = Union[
    TokenChunkEvent,
    ToolStatusEvent,
    PermissionRequestEvent,
    AuditTrailEvent,
    SessionLifecycleEvent,
    SystemNotificationEvent,
    RPCRequest,
    RPCResponse,
]
"""JSON-RPC 2.0 消息联合类型"""

GatewayPayload = Union[
    TokenChunkEvent,
    ToolStatusEvent,
    PermissionRequestEvent,
    AuditTrailEvent,
    SessionLifecycleEvent,
    SystemNotificationEvent,
    RPCRequest,
    RPCResponse,
    # ==== v1.5+ 多模态扩展预留 ====
    # TODO(v1.5): ImageChunk, AudioChunk — 见 GatewayMessage 中的注释
]
"""Gateway 可路由的所有消息类型"""
