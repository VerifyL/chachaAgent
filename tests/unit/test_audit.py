"""
tests/unit/test_audit.py
单元测试：core/models/audit.py 审计日志模型
覆盖：各类事件构造、JSON/JSONL 序列化、敏感信息脱敏、联合类型反序列化、工厂函数
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from core.models.audit import (
    AuditEvent,
    AuditEventCategory,
    SensitiveString,
    ToolCallAuditEvent,
    CostAuditEvent,
    MemoryChangeAuditEvent,
    PermissionAuditEvent,
    SessionAuditEvent,
    ModelCallAuditEvent,
    AuditRecord,
    audit_factory,
)


# ========== 1. SensitiveString 测试 ==========

class TestSensitiveString:
    def test_short_value_fully_redacted(self):
        s = SensitiveString(value="ab")
        assert s.masked == "[REDACTED]"
        assert str(s) == "[REDACTED]"

    def test_normal_value_partial_mask(self):
        s = SensitiveString(value="sk-1234567890abcdef")
        # 19 chars → masked = 前2 + 15个* + 后2
        assert s.masked == "sk***************ef"
        assert "1234567890" not in s.masked

    def test_exactly_four_chars(self):
        s = SensitiveString(value="1234")
        assert s.masked == "[REDACTED]"

    def test_dump_returns_masked(self):
        s = SensitiveString(value="secret-key-12345")
        dumped = s.model_dump()
        assert "secret-key-12345" not in dumped
        assert "*" in dumped

    def test_immutable(self):
        s = SensitiveString(value="my-secret")
        with pytest.raises(ValidationError):
            s.value = "new-secret"

    def test_repr_does_not_leak_value(self):
        s = SensitiveString(value="top-secret")
        r = repr(s)
        assert "top-secret" not in r


# ========== 2. AuditEvent 基类测试 ==========

class TestAuditEvent:
    def test_default_id_and_timestamp(self):
        e = AuditEvent(category=AuditEventCategory.SYSTEM)
        assert len(e.id) == 36  # UUID4
        assert e.timestamp.tzinfo == timezone.utc

    def test_frozen_immutable(self):
        e = AuditEvent(category=AuditEventCategory.SYSTEM)
        with pytest.raises(ValidationError):
            e.category = AuditEventCategory.COST

    def test_to_jsonl_minimal(self):
        e = AuditEvent(category=AuditEventCategory.SYSTEM)
        line = e.to_jsonl()
        parsed = json.loads(line)
        assert parsed["category"] == "system"

    def test_category_enum_serialized_as_string(self):
        e = AuditEvent(category=AuditEventCategory.CONFIG_CHANGE)
        line = e.to_jsonl()
        parsed = json.loads(line)
        assert parsed["category"] == "config_change"

    def test_missing_category_raises(self):
        with pytest.raises(ValidationError):
            AuditEvent()  # 缺少必填的 category

    def test_session_id_optional(self):
        e = AuditEvent(category=AuditEventCategory.SYSTEM)
        assert e.session_id is None

    def test_project_id_optional(self):
        e = AuditEvent(category=AuditEventCategory.SYSTEM, project_id="proj-1")
        assert e.project_id == "proj-1"


# ========== 3. ToolCallAuditEvent 测试 ==========

class TestToolCallAuditEvent:
    def test_required_fields(self):
        e = ToolCallAuditEvent(
            tool_name="read_file",
            tool_use_id="call_001",
            status="success",
        )
        assert e.tool_name == "read_file"
        assert e.category == "tool_call"
        assert e.arguments_summary == {}
        assert e.output_truncated is False

    def test_status_blocked(self):
        e = ToolCallAuditEvent(
            tool_name="rm",
            tool_use_id="call_002",
            status="blocked",
            blocked_by_policy="command_blacklist",
        )
        assert e.status == "blocked"
        assert e.blocked_by_policy == "command_blacklist"

    def test_sanitize_arguments_redacts_sensitive_keys(self):
        args = {
            "path": "/tmp/test.py",
            "api_key": "sk-secret-12345",
            "password": "p@ssw0rd",
            "authorization": "Bearer xyz",
            "token": "abc123",
        }
        cleaned = ToolCallAuditEvent.sanitize_arguments(args)
        assert cleaned["path"] == "/tmp/test.py"
        for key in ("api_key", "password", "authorization", "token"):
            assert cleaned[key] == "[REDACTED]"

    def test_sanitize_arguments_truncates_long_values(self):
        args = {"content": "x" * 300}
        cleaned = ToolCallAuditEvent.sanitize_arguments(args)
        assert len(cleaned["content"]) == 203  # 200 + "..."
        assert cleaned["content"].endswith("...")

    def test_arguments_summary_serialized(self):
        e = ToolCallAuditEvent(
            tool_name="search",
            tool_use_id="call_003",
            status="success",
            arguments_summary={"query": "bug", "file": "main.py"},
            duration_ms=150,
        )
        line = e.to_jsonl()
        parsed = json.loads(line)
        assert parsed["arguments_summary"] == {"query": "bug", "file": "main.py"}
        assert parsed["duration_ms"] == 150


# ========== 4. CostAuditEvent 测试 ==========

class TestCostAuditEvent:
    def test_full_event(self):
        e = CostAuditEvent(
            model_name="gpt-4",
            provider="openai",
            input_tokens=500,
            output_tokens=200,
            cost_usd=0.015,
            cumulative_cost_usd=2.50,
            cost_limit_usd=10.0,
            circuit_breaker_triggered=False,
        )
        assert e.category == "cost"
        assert e.input_tokens == 500
        assert e.output_tokens == 200
        assert e.cost_usd == 0.015
        assert e.circuit_breaker_triggered is False

    def test_circuit_breaker(self):
        e = CostAuditEvent(
            model_name="gpt-4",
            provider="openai",
            input_tokens=1000,
            output_tokens=1000,
            cost_usd=0.06,
            cumulative_cost_usd=10.05,
            cost_limit_usd=10.0,
            circuit_breaker_triggered=True,
        )
        assert e.cumulative_cost_usd > (e.cost_limit_usd or 0)
        assert e.circuit_breaker_triggered is True

    def test_negative_tokens_rejected(self):
        with pytest.raises(ValidationError):
            CostAuditEvent(
                model_name="gpt-4",
                provider="openai",
                input_tokens=-1,
            )

    def test_negative_cost_rejected(self):
        with pytest.raises(ValidationError):
            CostAuditEvent(
                model_name="gpt-4",
                provider="openai",
                cost_usd=-0.01,
            )

    def test_serialization_roundtrip(self):
        e = CostAuditEvent(
            session_id="s1",
            project_id="p1",
            model_name="claude-3",
            provider="anthropic",
            input_tokens=300,
            output_tokens=100,
            cost_usd=0.012,
            cumulative_cost_usd=0.05,
            cost_limit_usd=5.0,
            circuit_breaker_triggered=False,
        )
        line = e.to_jsonl()
        parsed = json.loads(line)
        assert parsed["model_name"] == "claude-3"
        assert parsed["cumulative_cost_usd"] == 0.05


# ========== 5. MemoryChangeAuditEvent 测试 ==========

class TestMemoryChangeAuditEvent:
    def test_write_operation(self):
        e = MemoryChangeAuditEvent(
            operation="write",
            file_path="projects/p1/memory/MEMORY.md",
            change_summary="添加用户偏好：始终使用中文回复",
            lines_before=10,
            lines_after=12,
        )
        assert e.operation == "write"
        assert "MEMORY.md" in e.file_path

    def test_prune_operation(self):
        e = MemoryChangeAuditEvent(
            operation="prune",
            file_path="projects/p1/memory/MEMORY.md",
            change_summary="自动剪枝移除了 50 行旧内容",
            lines_before=250,
            lines_after=200,
        )
        assert e.operation == "prune"

    def test_auto_clean_operation(self):
        e = MemoryChangeAuditEvent(
            operation="auto_clean",
            file_path="projects/p1/topics/topic_old.md",
            change_summary="LRU 清理删除过期主题文件",
        )
        assert e.operation == "auto_clean"
        assert e.lines_before is None

    def test_operation_enum_enforced(self):
        with pytest.raises(ValidationError):
            MemoryChangeAuditEvent(
                operation="copy",  # 非法的操作类型
                file_path="MEMORY.md",
            )

    def test_serialization(self):
        e = MemoryChangeAuditEvent(
            session_id="s2",
            operation="read",
            file_path="projects/p1/memory/MEMORY.md",
        )
        line = e.to_jsonl()
        parsed = json.loads(line)
        assert parsed["operation"] == "read"
        assert parsed["category"] == "memory_change"


# ========== 6. PermissionAuditEvent 测试 ==========

class TestPermissionAuditEvent:
    def test_pending_approval(self):
        e = PermissionAuditEvent(
            request_id="req_001",
            tool_name="shell",
            command_or_action="rm -rf /tmp/test",
            reason="用户请求删除临时文件",
            approved=None,
            cache_hit=False,
        )
        assert e.approved is None
        assert e.category == "permission"

    def test_approved_with_cache(self):
        e = PermissionAuditEvent(
            request_id="req_002",
            tool_name="shell",
            command_or_action="git commit -m 'fix'",
            reason="自动提交变更",
            approved=True,
            cache_hit=True,
            cache_ttl_seconds=300,
        )
        assert e.approved is True
        assert e.cache_hit is True

    def test_denied(self):
        e = PermissionAuditEvent(
            request_id="req_003",
            tool_name="shell",
            command_or_action="sudo rm -rf /",
            reason="用户请求执行特权命令",
            approved=False,
        )
        assert e.approved is False

    def test_required_fields(self):
        with pytest.raises(ValidationError):
            PermissionAuditEvent()  # 缺少 request_id, tool_name 等


# ========== 7. SessionAuditEvent 测试 ==========

class TestSessionAuditEvent:
    def test_session_started(self):
        e = SessionAuditEvent(
            session_id="s1",
            project_id="p1",
            event="started",
        )
        assert e.event == "started"

    def test_session_ended_with_stats(self):
        e = SessionAuditEvent(
            session_id="s1",
            project_id="p1",
            event="ended",
            total_tokens_at_event=5000,
            total_cost_at_event=0.15,
            duration_ms_at_event=120000,
        )
        assert e.total_tokens_at_event == 5000
        assert e.total_cost_at_event == 0.15

    def test_checkpoint_created(self):
        e = SessionAuditEvent(
            session_id="s1",
            event="checkpoint_created",
            checkpoint_id="ckpt-001",
        )
        assert e.checkpoint_id == "ckpt-001"

    def test_invalid_event_value(self):
        with pytest.raises(ValidationError):
            SessionAuditEvent(
                event="invalid_event",
            )


# ========== 8. ModelCallAuditEvent 测试 ==========

class TestModelCallAuditEvent:
    def test_successful_call(self):
        e = ModelCallAuditEvent(
            session_id="s1",
            model_name="gpt-4",
            provider="openai",
            prompt_tokens=1000,
            completion_tokens=500,
            latency_ms=2000,
            retry_count=0,
            status="success",
        )
        assert e.status == "success"
        assert e.retry_count == 0
        assert e.category == "model_call"

    def test_rate_limited(self):
        e = ModelCallAuditEvent(
            model_name="gpt-4",
            provider="openai",
            prompt_tokens=100,
            completion_tokens=0,
            latency_ms=100,
            retry_count=3,
            status="rate_limited",
        )
        assert e.status == "rate_limited"
        assert e.completion_tokens == 0

    def test_negative_latency_rejected(self):
        with pytest.raises(ValidationError):
            ModelCallAuditEvent(
                model_name="gpt-4",
                provider="openai",
                latency_ms=-1,
                status="success",
            )


# ========== 9. 联合类型与工厂函数测试 ==========

class TestAuditRecordUnion:
    def test_union_deserialize_tool_call(self):
        data = {
            "category": "tool_call",
            "tool_name": "read_file",
            "tool_use_id": "c1",
            "status": "success",
        }
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, ToolCallAuditEvent)

    def test_union_deserialize_cost(self):
        data = {
            "category": "cost",
            "model_name": "gpt-4",
            "provider": "openai",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.003,
            "cumulative_cost_usd": 0.01,
        }
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, CostAuditEvent)

    def test_union_deserialize_memory_change(self):
        data = {
            "category": "memory_change",
            "operation": "write",
            "file_path": "memory.md",
        }
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, MemoryChangeAuditEvent)

    def test_union_deserialize_permission(self):
        data = {
            "category": "permission",
            "request_id": "r1",
            "tool_name": "shell",
            "command_or_action": "ls",
            "reason": "test",
        }
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, PermissionAuditEvent)

    def test_union_deserialize_session(self):
        data = {
            "category": "session",
            "event": "started",
        }
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, SessionAuditEvent)

    def test_union_deserialize_model_call(self):
        data = {
            "category": "model_call",
            "model_name": "gpt-4",
            "provider": "openai",
            "status": "success",
        }
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, ModelCallAuditEvent)

    def test_union_deserialize_system(self):
        data = {"category": "system"}
        record = TypeAdapter(AuditRecord).validate_python(data)
        assert isinstance(record, AuditEvent)
        assert record.category == "system"


class TestAuditFactory:
    def test_factory_creates_tool_call(self):
        evt = audit_factory(
            AuditEventCategory.TOOL_CALL,
            session_id="s1",
            tool_name="grep",
            tool_use_id="g1",
            status="success",
        )
        assert isinstance(evt, ToolCallAuditEvent)
        assert evt.tool_name == "grep"
        assert evt.session_id == "s1"

    def test_factory_creates_cost(self):
        evt = audit_factory(
            AuditEventCategory.COST,
            model_name="gpt-4",
            provider="openai",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.003,
            cumulative_cost_usd=0.01,
        )
        assert isinstance(evt, CostAuditEvent)

    def test_factory_creates_memory_change(self):
        evt = audit_factory(
            AuditEventCategory.MEMORY_CHANGE,
            operation="read",
            file_path="MEMORY.md",
        )
        assert isinstance(evt, MemoryChangeAuditEvent)

    def test_factory_creates_permission(self):
        evt = audit_factory(
            AuditEventCategory.PERMISSION,
            request_id="r1",
            tool_name="shell",
            command_or_action="ls",
            reason="test",
        )
        assert isinstance(evt, PermissionAuditEvent)

    def test_factory_creates_session(self):
        evt = audit_factory(
            AuditEventCategory.SESSION,
            event="started",
        )
        assert isinstance(evt, SessionAuditEvent)

    def test_factory_creates_model_call(self):
        evt = audit_factory(
            AuditEventCategory.MODEL_CALL,
            model_name="gpt-4",
            provider="openai",
            status="success",
        )
        assert isinstance(evt, ModelCallAuditEvent)

    def test_factory_creates_system(self):
        evt = audit_factory(AuditEventCategory.SYSTEM)
        assert isinstance(evt, AuditEvent)


# ========== 10. JSONL 批量序列化场景 ==========

class TestJSONLBatch:
    def test_multiple_events_jsonl_format(self):
        """模拟一次会话中的审计事件序列化为 JSONL"""
        events: list[AuditRecord] = [
            SessionAuditEvent(session_id="s1", project_id="p1", event="started"),
            ToolCallAuditEvent(tool_name="read_file", tool_use_id="c1", status="success"),
            CostAuditEvent(
                model_name="gpt-4", provider="openai",
                input_tokens=100, output_tokens=50,
                cost_usd=0.003, cumulative_cost_usd=0.003,
            ),
            SessionAuditEvent(session_id="s1", project_id="p1", event="ended",
                              total_tokens_at_event=150, total_cost_at_event=0.003),
        ]

        # 模拟写入 audit.jsonl
        lines = [e.to_jsonl() for e in events]

        # 验证每行都能独立解析
        for line in lines:
            parsed = json.loads(line)
            assert "id" in parsed
            assert "timestamp" in parsed
            assert "category" in parsed

        # 验证事件顺序
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["event"] == "started"
        assert parsed[1]["tool_name"] == "read_file"
        assert parsed[2]["model_name"] == "gpt-4"
        assert parsed[3]["event"] == "ended"

    def test_all_events_are_frozen(self):
        """所有审计事件均不可变"""
        events = {
            "system": AuditEvent(category=AuditEventCategory.SYSTEM),
            "tool": ToolCallAuditEvent(tool_name="t", tool_use_id="id", status="success"),
            "cost": CostAuditEvent(model_name="m", provider="p",
                                   input_tokens=0, output_tokens=0, cost_usd=0, cumulative_cost_usd=0),
            "memory": MemoryChangeAuditEvent(operation="read", file_path="f"),
            "permission": PermissionAuditEvent(request_id="r", tool_name="t",
                                               command_or_action="c", reason="r"),
            "session": SessionAuditEvent(event="started"),
            "model_call": ModelCallAuditEvent(model_name="m", provider="p", status="success"),
        }
        for name, evt in events.items():
            with pytest.raises(ValidationError, match="frozen"):
                evt.id = "new-id"
