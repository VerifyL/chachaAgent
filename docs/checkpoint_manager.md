# 会话检查点管理器 (`core/checkpoint_manager.py`)

本文档说明 `CheckpointManager` 的保存、恢复、列表和清理功能。检查点是会话的完整 JSON 快照，支持断点续传。

## 概述

检查点全量保存 `ConversationState`（events + metadata + loop_state）到文件。恢复时通过 `model_validate_json()` 重建内存对象。

**保存内容**：
- ✅ 所有 events（用户消息、助手回复、工具结果）
- ✅ SessionMetadata（token 统计、成本、耗时）
- ✅ AgentLoopState（当前迭代、等待状态）
- ✅ 历史检查点列表

---

## 1. 保存

### 1.1 基本用法

```python
from core.checkpoint_manager import CheckpointManager

mgr = CheckpointManager()
cp = mgr.save(state, description="高危操作前保存")
# → SessionCheckpoint(checkpoint_id, event_index=4, ...)
```

**文件位置**：`.chacha_agent/checkpoints/{session_id}/{checkpoint_id}.json`

### 1.2 自动保存时机

在 Orchestrator 中集成的自动保存节点：

| 时机 | 触发方 | 优先级 |
|------|--------|--------|
| 高危操作前 | Orchestrator | P0 |
| 每 10 轮对话后 | Orchestrator | P1 |
| 用户手动触发 | CLI/Web | P1 |
| 会话结束 | Orchestrator | P1 |

---

## 2. 恢复

### 2.1 恢复最新

```python
state = mgr.restore("session-abc")
# → ConversationState(events=[...], metadata=...)
```

### 2.2 恢复指定版本

```python
state = mgr.restore("session-abc", checkpoint_id="ckpt-001")
```

### 2.3 恢复后继续对话

```python
restored = mgr.restore(sid)
restored.add_event(MessageEvent(source="user", role="user", content="继续工作"))
# → Orchestrator.run(state=restored)
```

---

## 3. 管理

| 方法 | 说明 |
|------|------|
| `list(session_id)` | 列出所有检查点 |
| `delete(session_id, checkpoint_id)` | 删除指定 |
| `purge(session_id, max_age_hours=72)` | 清理 N 小时前的旧检查点（保留最新 1 个） |

---

## 4. 文件损坏处理

损坏的 `.json` 文件不会被解析（捕获 `JSONDecodeError`，记录日志后返回 `None`），不影响其他检查点。
