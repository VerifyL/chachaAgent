"""
tests/unit/test_rpc_schema.py
单元测试：protocol/rpc_schema.py RPC 消息模型
覆盖：GatewayMessage seq 自增、RPCRequest/RPCResponse 构造、各类事件序列化、
      PermissionResponse 关联、AuditTrailEvent 审计引用、联合类型反序列化、多模态预留
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from protocol.rpc_schema import (
    RPCError,
    RPCRequest,
    RPCResponse,
    RPCEvent,
    GatewayMessage,
    TokenChunkEvent,
    ToolCallDelta,
    ToolStatusEvent,
    PermissionRequestEvent,
    PermissionResponse,
    AuditTrailEvent,
    SessionLifecycleEvent,
    SystemNotificationEvent,
    RPCMessage,
    GatewayPayload,
)
from core.models.audit import AuditEvent, AuditEventCategory, CostAuditEvent


# ========== 1. 基础消息测试 ==========

class TestRPCRequest:
    def test_minimal(self):
        req = RPCRequest(method="user/message")
        assert req.jsonrpc == "2.0"
        assert len(req.id) == 36  # UUID4
        assert req.params == {}

    def test_with_params(self):
        req = RPCRequest(method="tool/execute", params={"tool_name": "read_file", "path": "/tmp"})
        assert req.params["tool_name"] == "read_file"

    def test_id_auto_generated(self):
        req1 = RPCRequest(method="test")
        req2 = RPCRequest(method="test")
        assert req1.id != req2.id

    def test_jsonrpc_cannot_be_changed(self):
        req = RPCRequest(method="test")
        with pytest.raises(ValidationError):
            req.jsonrpc = "1.0"

    def test_serialization(self):
        req = RPCRequest(method="user/message", params={"content": "hello"})
        j = req.model_dump_json()
        parsed = json.loads(j)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "user/message"
        assert parsed["params"]["content"] == "hello"


class TestRPCResponse:
    def test_success(self):
        resp = RPCResponse(id="req_1", result={"status": "ok"})
        assert resp.result == {"status": "ok"}
        assert resp.error is None

    def test_error(self):
        resp = RPCResponse(
            id="req_1",
            error=RPCError(code=-32600, message="Invalid Request"),
        )
        assert resp.result is None
        assert resp.error.code == -32600

    def test_id_required(self):
        with pytest.raises(ValidationError):
            RPCResponse()

    def test_serialization(self):
        resp = RPCResponse(id="req_1", result={"data": [1, 2, 3]})
        j = resp.model_dump_json()
        parsed = json.loads(j)
        assert parsed["result"] == {"data": [1, 2, 3]}


class TestRPCEvent:
    def test_minimal(self):
        evt = RPCEvent(method="system/ping")
        assert evt.jsonrpc == "2.0"
        assert "id" not in evt.model_dump()  # 事件无 id 字段
        assert evt.params == {}

    def test_with_params(self):
        evt = RPCEvent(method="stream/token", params={"delta": "Hello"})
        assert evt.params["delta"] == "Hello"

    def test_extra_fields_forbidden(self):
        """事件不应包含未定义的字段"""
        with pytest.raises(ValidationError):
            RPCEvent(method="test", unknown_field="value")


# ========== 2. GatewayMessage 测试 ==========

class TestGatewayMessage:
    def test_wrap_request(self):
        req = RPCRequest(method="user/message", params={"content": "hi"})
        msg = GatewayMessage(seq=1, session_id="s1", payload=req)
        assert msg.seq == 1
        assert msg.session_id == "s1"
        assert isinstance(msg.payload, RPCRequest)

    def test_wrap_response(self):
        resp = RPCResponse(id="req_1", result="ok")
        msg = GatewayMessage(seq=2, payload=resp)
        assert msg.seq == 2

    def test_wrap_event(self):
        evt = TokenChunkEvent()
        msg = GatewayMessage(seq=3, session_id="s1", payload=evt)
        assert isinstance(msg.payload, TokenChunkEvent)

    def test_seq_required(self):
        with pytest.raises(ValidationError):
            GatewayMessage(payload=RPCRequest(method="test"))

    def test_negative_seq_rejected(self):
        with pytest.raises(ValidationError):
            GatewayMessage(seq=-1, payload=RPCRequest(method="test"))

    def test_project_id_optional(self):
        msg = GatewayMessage(seq=0, payload=RPCRequest(method="test"))
        assert msg.project_id is None

    def test_serialization(self):
        msg = GatewayMessage(
            seq=42,
            project_id="proj-a",
            session_id="s1",
            payload=RPCRequest(method="user/message", params={"content": "hello"}),
        )
        j = msg.model_dump_json()
        parsed = json.loads(j)
        assert parsed["seq"] == 42
        assert parsed["project_id"] == "proj-a"
        assert parsed["payload"]["method"] == "user/message"


# ========== 3. 流式输出事件测试 ==========

class TestTokenChunkEvent:
    def test_default(self):
        evt = TokenChunkEvent()
        assert evt.method == "stream/token"
        assert evt.params["delta"] == ""
        assert evt.params["finish_reason"] is None

    def test_set_delta(self):
        evt = TokenChunkEvent().set_delta("你好")
        assert evt.params["delta"] == "你好"

    def test_set_finish(self):
        evt = TokenChunkEvent().set_finish("stop")
        assert evt.params["finish_reason"] == "stop"

    def test_set_tool_call_delta(self):
        delta = ToolCallDelta(index=0, id="call_1", function_name="read_file", arguments_delta='{"pa')
        evt = TokenChunkEvent().set_tool_call_delta(delta)
        assert evt.params["tool_call_delta"]["id"] == "call_1"
        assert evt.params["tool_call_delta"]["arguments_delta"] == '{"pa'

    def test_tool_call_delta_partial(self):
        """tool_call_delta 支持增量填充"""
        delta = ToolCallDelta(index=0, arguments_delta='th":')
        assert delta.function_name is None
        assert delta.id is None

    def test_serialization(self):
        evt = TokenChunkEvent().set_delta("Hello").set_finish("stop")
        j = evt.model_dump_json()
        parsed = json.loads(j)
        assert parsed["method"] == "stream/token"
        assert parsed["params"]["delta"] == "Hello"
        assert parsed["params"]["finish_reason"] == "stop"


# ========== 4. 工具状态事件测试 ==========

class TestToolStatusEvent:
    def test_default(self):
        evt = ToolStatusEvent()
        assert evt.method == "tool/status"
        assert evt.params["status"] == "pending"

    def test_status_enum_values(self):
        for status in ["pending", "running", "done", "error"]:
            evt = ToolStatusEvent(params={"status": status})
            assert evt.params["status"] == status

    def test_with_progress(self):
        evt = ToolStatusEvent(
            params={
                "tool_use_id": "call_1",
                "tool_name": "read_file",
                "status": "running",
                "progress": "Reading line 42/100...",
            }
        )
        assert evt.params["progress"] == "Reading line 42/100..."

    def test_with_summary(self):
        evt = ToolStatusEvent(
            params={
                "tool_use_id": "call_1",
                "tool_name": "pytest",
                "status": "done",
                "duration_ms": 2300,
                "output_summary": "3 tests passed",
            }
        )
        assert evt.params["duration_ms"] == 2300


# ========== 5. 权限请求/响应测试 ==========

class TestPermission:
    def test_request_event(self):
        evt = PermissionRequestEvent(
            params={
                "request_id": "req_1",
                "tool_name": "shell",
                "command_or_action": "rm -rf /tmp/test",
                "reason": "用户要求清理临时文件",
            }
        )
        assert evt.method == "permission/request"
        assert evt.params["command_or_action"] == "rm -rf /tmp/test"

    def test_response(self):
        resp = PermissionResponse(request_id="req_1", approved=True)
        assert resp.request_id == "req_1"
        assert resp.approved is True

    def test_response_denied(self):
        resp = PermissionResponse(request_id="req_2", approved=False)
        assert resp.approved is False

    def test_request_response_association(self):
        """请求-响应的 request_id 关联"""
        req_evt = PermissionRequestEvent(
            params={
                "request_id": "req_99",
                "tool_name": "git",
                "command_or_action": "git push --force",
                "reason": "用户要求强制推送",
            }
        )
        resp = PermissionResponse(request_id="req_99", approved=False)
        assert req_evt.params["request_id"] == resp.request_id

    def test_response_serialization(self):
        resp = PermissionResponse(request_id="req_1", approved=True)
        j = resp.model_dump_json()
        parsed = json.loads(j)
        assert parsed["request_id"] == "req_1"
        assert parsed["approved"] is True


# ========== 6. 审计事件测试 ==========

class TestAuditTrailEvent:
    def test_wraps_audit_record(self):
        audit = CostAuditEvent(
            session_id="s1",
            project_id="p1",
            model_name="gpt-4",
            provider="openai",
            input_tokens=500,
            output_tokens=200,
            cost_usd=0.015,
            cumulative_cost_usd=2.50,
        )
        evt = AuditTrailEvent(audit=audit)
        assert evt.method == "audit/trail"
        assert evt.audit.model_name == "gpt-4"

    def test_model_dump_expands_audit_to_params(self):
        audit = CostAuditEvent(
            model_name="gpt-4",
            provider="openai",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.003,
            cumulative_cost_usd=0.01,
        )
        evt = AuditTrailEvent(audit=audit)
        data = evt.model_dump()
        assert "params" in data
        assert data["params"]["model_name"] == "gpt-4"

    def test_serialization(self):
        audit = CostAuditEvent(
            model_name="gpt-4",
            provider="openai",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.003,
            cumulative_cost_usd=0.01,
        )
        evt = AuditTrailEvent(audit=audit)
        data = evt.model_dump()
        assert "params" in data
        assert "audit" not in data  # audit 被展开到 params
        assert data["params"]["model_name"] == "gpt-4"


# ========== 7. 会话生命周期测试 ==========

class TestSessionLifecycleEvent:
    def test_started(self):
        evt = SessionLifecycleEvent(
            params={"event": "started", "session_id": "s1", "project_id": "p1"}
        )
        assert evt.method == "session/lifecycle"
        assert evt.params["event"] == "started"

    def test_ended_with_stats(self):
        evt = SessionLifecycleEvent(
            params={
                "event": "ended",
                "session_id": "s1",
                "total_tokens": 5000,
                "total_cost_usd": 0.15,
            }
        )
        assert evt.params["total_tokens"] == 5000

    def test_checkpoint(self):
        evt = SessionLifecycleEvent(
            params={
                "event": "checkpoint_created",
                "session_id": "s1",
                "checkpoint_id": "ckpt-1",
            }
        )
        assert evt.params["checkpoint_id"] == "ckpt-1"


# ========== 8. 系统通知测试 ==========

class TestSystemNotificationEvent:
    def test_info(self):
        evt = SystemNotificationEvent(
            params={"level": "info", "message": "环境校验通过"}
        )
        assert evt.method == "system/notification"
        assert evt.params["level"] == "info"

    def test_error_with_details(self):
        evt = SystemNotificationEvent(
            params={
                "level": "error",
                "message": "配置文件解析失败",
                "source_module": "core.config_manager",
                "details": "TOML 第 15 行语法错误",
            }
        )
        assert evt.params["source_module"] == "core.config_manager"


# ========== 9. 联合类型与场景测试 ==========

class TestUnionTypes:
    def test_gateway_payload_deserialize_rpc_request(self):
        data = {"jsonrpc": "2.0", "id": "r1", "method": "user/message"}
        payload = TypeAdapter(GatewayPayload).validate_python(data)
        assert isinstance(payload, RPCRequest)

    def test_gateway_payload_deserialize_token_chunk(self):
        data = {"jsonrpc": "2.0", "method": "stream/token", "params": {"delta": "hi"}}
        payload = TypeAdapter(GatewayPayload).validate_python(data)
        assert isinstance(payload, TokenChunkEvent)

    def test_gateway_payload_deserialize_tool_status(self):
        data = {"jsonrpc": "2.0", "method": "tool/status", "params": {"status": "running"}}
        payload = TypeAdapter(GatewayPayload).validate_python(data)
        assert isinstance(payload, ToolStatusEvent)

    def test_rpc_message_deserialize_request(self):
        data = {"jsonrpc": "2.0", "id": "r1", "method": "test"}
        msg = TypeAdapter(RPCMessage).validate_python(data)
        assert isinstance(msg, RPCRequest)


def test_full_gateway_flow():
    """模拟完整的网关消息流：请求 → 流式 → 工具状态 → 权限 → 审计 → 响应"""
    # 1. 用户消息请求
    req = RPCRequest(method="user/message", params={"content": "帮我读文件"})
    gw_req = GatewayMessage(seq=1, session_id="s1", payload=req)
    assert gw_req.seq == 1

    # 2. 流式输出
    chunks = [
        GatewayMessage(seq=2, session_id="s1", payload=TokenChunkEvent().set_delta("正在")),
        GatewayMessage(seq=3, session_id="s1", payload=TokenChunkEvent().set_delta("读取")),
        GatewayMessage(seq=4, session_id="s1", payload=TokenChunkEvent().set_finish("stop")),
    ]
    assert chunks[-1].payload.params["finish_reason"] == "stop"

    # 3. 工具状态
    tool = GatewayMessage(
        seq=5, session_id="s1",
        payload=ToolStatusEvent(params={
            "tool_use_id": "call_1",
            "tool_name": "read_file",
            "status": "done",
            "output_summary": "文件内容共 100 行",
        }),
    )
    assert tool.payload.params["output_summary"] == "文件内容共 100 行"

    # 4. 权限请求 + 响应
    perm_req = PermissionRequestEvent(params={
        "request_id": "req_1",
        "tool_name": "shell",
        "command_or_action": "git push",
        "reason": "推送变更",
    })
    perm_resp = PermissionResponse(request_id="req_1", approved=True)
    assert perm_resp.approved is True

    # 5. 审计
    audit = CostAuditEvent(
        model_name="gpt-4", provider="openai",
        input_tokens=100, output_tokens=50,
        cost_usd=0.003, cumulative_cost_usd=0.01,
    )
    audit_gw = GatewayMessage(seq=6, session_id="s1", payload=AuditTrailEvent(audit=audit))
    assert audit_gw.payload.audit.cost_usd == 0.003

    # 6. 最终响应
    final = GatewayMessage(seq=7, session_id="s1", payload=RPCResponse(id=req.id, result={"status": "ok"}))
    assert final.payload.id == req.id

    # 7. 会话结束
    end_evt = GatewayMessage(
        seq=8, session_id="s1",
        payload=SessionLifecycleEvent(params={"event": "ended", "session_id": "s1"}),
    )
    assert end_evt.payload.params["event"] == "ended"
