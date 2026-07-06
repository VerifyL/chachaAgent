# tests/unit/test_session_models.py
"""
针对 core/models/session.py 的单元测试
覆盖：序列化/反序列化、必填字段校验、时间戳行为
"""

from datetime import datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from core.models.session import (
    AgentLoopState,
    Attachment,
    BaseEvent,
    CheckpointEvent,
    CompactEvent,
    ConversationState,
    MessageEvent,
    ObservationEvent,
    PermissionRequestEvent,
    SessionCheckpoint,
    SessionEvent,
    SessionMetadata,
    ToolCallEvent,
)


def assert_valid_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def _assert_timestamp_has_tz(ts: datetime) -> bool:
    """验证时间戳有时区信息（项目使用 CST, UTC+8）。"""
    return ts.tzinfo is not None


# ============================================================================
# 1. Attachment 测试
# ============================================================================

class TestAttachment:
    def test_default_creation(self):
        a = Attachment(data=b"hello")
        assert a.type == "image"
        assert a.mime_type == "application/octet-stream"
        assert a.data == b"hello"

    def test_invalid_mime_type(self):
        with pytest.raises(ValidationError):
            Attachment(data=b"abc", mime_type="video/mp4")

    def test_serialization_roundtrip(self):
        a = Attachment(
            type="audio",
            data=b"\x00\x01\x02",
            mime_type="audio/wav",
            filename="sound.wav"
        )
        json_str = a.model_dump_json()
        restored = Attachment.model_validate_json(json_str)
        assert restored.data == b"\x00\x01\x02"
        assert restored.type == "audio"
        assert restored.filename == "sound.wav"


# ============================================================================
# 2. 事件类型测试（修正：全部补上 source 字段）
# ============================================================================

class TestBaseEvent:
    def test_default_id_and_timestamp(self):
        e = BaseEvent(source="user")
        assert assert_valid_uuid(e.id)
        assert _assert_timestamp_has_tz(e.timestamp)

    def test_frozen_immutable(self):
        e = BaseEvent(source="user")
        with pytest.raises(ValidationError):
            e.source = "agent"


class TestMessageEvent:
    def test_required_fields(self):
        # 缺少 content
        with pytest.raises(ValidationError):
            MessageEvent(source="user", role="user")

    def test_serialization(self):
        msg = MessageEvent(source="user", role="assistant", content="Hello, world!")
        j = msg.model_dump_json()
        restored = MessageEvent.model_validate_json(j)
        assert restored.role == "assistant"
        assert restored.content == "Hello, world!"
        assert restored.attachments == []


class TestToolCallEvent:
    def test_required_fields(self):
        with pytest.raises(ValidationError):
            ToolCallEvent(source="agent")  # 缺 tool_name, arguments, tool_use_id

    def test_thought_optional(self):
        tc = ToolCallEvent(
            source="agent",
            tool_name="read_file",
            arguments={"path": "test.py"},
            tool_use_id="call_123"
        )
        assert tc.thought is None

    def test_serialization(self):
        tc = ToolCallEvent(
            source="agent",
            tool_name="search",
            arguments={"query": "bug"},
            tool_use_id="call_456",
            thought="need to search"
        )
        json_str = tc.model_dump_json()
        restored = ToolCallEvent.model_validate_json(json_str)
        assert restored.tool_use_id == "call_456"
        assert restored.arguments == {"query": "bug"}


class TestObservationEvent:
    def test_success_status(self):
        obs = ObservationEvent(
            source="tool",
            tool_use_id="call_1",
            content="result",
            status="success"
        )
        assert obs.error is None

    def test_error_status_with_error(self):
        obs = ObservationEvent(
            source="tool",
            tool_use_id="call_1",
            content="",
            status="error",
            error="file not found"
        )
        assert obs.error == "file not found"

    def test_truncated_flag(self):
        obs = ObservationEvent(
            source="tool",
            tool_use_id="call_1",
            content="...",
            status="success",
            truncated=True
        )
        assert obs.truncated is True


class TestPermissionRequestEvent:
    def test_required_fields(self):
        with pytest.raises(ValidationError):
            PermissionRequestEvent(source="system")  # 缺 request_id, tool_name, command_or_action, reason

    def test_approved_none_by_default(self):
        pr = PermissionRequestEvent(
            source="agent",
            request_id="req_1",
            tool_name="shell",
            command_or_action="rm -rf /",
            reason="user request"
        )
        assert pr.approved is None


class TestCompactEvent:
    def test_serialization(self):
        ce = CompactEvent(
            source="system",
            before_token_count=5000,
            after_token_count=2000,
            summary="Compressed history"
        )
        j = ce.model_dump_json()
        restored = CompactEvent.model_validate_json(j)
        assert restored.before_token_count == 5000


class TestCheckpointEvent:
    def test_serialization(self):
        ck = CheckpointEvent(
            source="system",
            checkpoint_id="ckpt-1",
            description="save point"
        )
        j = ck.model_dump_json()
        restored = CheckpointEvent.model_validate_json(j)
        assert restored.checkpoint_id == "ckpt-1"


# ============================================================================
# 3. 联合类型反序列化测试 (使用 TypeAdapter)
# ============================================================================

class TestSessionEventUnion:
    def test_union_deserialize_message_event(self):
        data = {
            "id": "uuid-1",
            "timestamp": "2025-01-01T00:00:00Z",
            "source": "user",
            "role": "user",
            "content": "hello"
        }
        event = TypeAdapter(SessionEvent).validate_python(data)
        assert isinstance(event, MessageEvent)

    def test_union_deserialize_tool_call_event(self):
        data = {
            "source": "agent",
            "tool_name": "test",
            "arguments": {},
            "tool_use_id": "t1"
        }
        event = TypeAdapter(SessionEvent).validate_python(data)
        assert isinstance(event, ToolCallEvent)


# ============================================================================
# 4. AgentLoopState 测试
# ============================================================================

class TestAgentLoopState:
    def test_default_values(self):
        state = AgentLoopState()
        assert state.iteration == 0
        assert state.pending_tool_calls == []
        assert state.waiting_for is None

    def test_mutable_state(self):
        state = AgentLoopState()
        state.iteration += 1
        assert state.iteration == 1

    def test_cache_serialization(self):
        obs = ObservationEvent(
            source="tool",
            tool_use_id="call_x",
            content="result",
            status="success"
        )
        state = AgentLoopState(
            tool_results_cache={"call_x": obs}
        )
        j = state.model_dump_json()
        restored = AgentLoopState.model_validate_json(j)
        assert "call_x" in restored.tool_results_cache
        assert restored.tool_results_cache["call_x"].status == "success"


# ============================================================================
# 5. SessionMetadata 测试
# ============================================================================

class TestSessionMetadata:
    def test_project_id_required(self):
        with pytest.raises(ValidationError):
            SessionMetadata()

    def test_created_at_utc(self):
        meta = SessionMetadata(project_id="proj1")
        assert _assert_timestamp_has_tz(meta.created_at)
        assert _assert_timestamp_has_tz(meta.updated_at)

    def test_parent_session_id_optional(self):
        meta = SessionMetadata(project_id="proj1")
        assert meta.parent_session_id is None

    def test_statistics_defaults(self):
        meta = SessionMetadata(project_id="proj1")
        assert meta.total_tokens == 0
        assert meta.total_cost_usd == 0.0
        assert meta.total_duration_ms == 0


# ============================================================================
# 6. ConversationState 综合测试
# ============================================================================

class TestConversationState:
    def test_add_event_updates_timestamp(self):
        meta = SessionMetadata(project_id="proj2")
        state = ConversationState(metadata=meta)
        old_ts = state.metadata.updated_at
        msg = MessageEvent(source="user", role="user", content="test")
        state.add_event(msg)
        assert state.metadata.updated_at >= old_ts

    def test_get_messages_for_llm_basic(self):
        meta = SessionMetadata(project_id="demo")
        state = ConversationState(metadata=meta)
        # 用户消息
        state.add_event(MessageEvent(source="user", role="user", content="read file"))
        # 工具调用
        state.add_event(ToolCallEvent(
            source="agent",
            tool_name="read_file",
            arguments={"path": "a.py"},
            tool_use_id="call_1"
        ))
        # 观察结果
        state.add_event(ObservationEvent(
            source="tool",
            tool_use_id="call_1",
            content="print('hello')",
            status="success"
        ))
        msgs = state.get_messages_for_llm()
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert "tool_calls" in msgs[1]
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "call_1"

    def test_checkpoint_creation(self):
        meta = SessionMetadata(project_id="check")
        state = ConversationState(metadata=meta)
        state.add_event(MessageEvent(source="user", role="user", content="start"))
        cp = SessionCheckpoint(
            event_index=0,
            metadata_snapshot=state.metadata,
            loop_state_snapshot=state.loop_state
        )
        state.checkpoints.append(cp)
        assert len(state.checkpoints) == 1
        assert state.checkpoints[0].event_index == 0

    def test_update_metadata(self):
        meta = SessionMetadata(project_id="upd")
        state = ConversationState(metadata=meta)
        state.update_metadata(total_tokens=100, total_cost_usd=0.02)
        assert state.metadata.total_tokens == 100
        assert state.metadata.total_cost_usd == 0.02

    def test_timestamps_are_utc(self):
        meta = SessionMetadata(project_id="tz")
        state = ConversationState(metadata=meta)
        assert state.metadata.created_at.tzinfo is not None
        assert state.metadata.updated_at.tzinfo is not None
        event = MessageEvent(source="user", role="user", content="hi")
        assert event.timestamp.tzinfo is not None
