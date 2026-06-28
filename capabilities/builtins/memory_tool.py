"""
memory_tool.py — MemoryTool: 管理项目记忆（读写主题、搜索）。

为 LLM 暴露 MemoryManager 的安全子集。
不暴露：write_permanent_memory / delete_session / prune_old_days / cache 系列。
"""

import logging
import time as time_mod
from typing import Any

from capabilities.base import BaseTool
from capabilities.result import ToolResult

logger = logging.getLogger(__name__)

# ── 合法 action 集合 ──
READ_ACTIONS = {"permanent_read", "topic_read", "topics", "recent", "search"}
WRITE_ACTIONS = {"topic_write"}
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS

# action → needs session  映射
_SESSION_REQUIRED = READ_ACTIONS - {"permanent_read"} | WRITE_ACTIONS


class MemoryTool(BaseTool):
    """管理项目记忆：读写主题记忆、追加每日记录、读取永久记忆、搜索。"""

    name = "memory"
    description = (
        "管理项目记忆：读写主题记忆、读取永久记忆、搜索历史。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "操作类型：topic_read（读主题）、"
                    "topic_write（写主题）、permanent_read（读永久记忆）、"
                    "recent（读近期记忆）、topics（列出主题）、search（搜索记忆）"
                ),
                "enum": sorted(ALL_ACTIONS),
            },
            "content": {
                "type": "string",
                "description": "记忆内容（action=topic_write 时必填）",
            },
            "topic": {
                "type": "string",
                "description": "主题名称（action=topic_read|topic_write 时必填）",
            },
            "days": {
                "type": "integer",
                "description": "读取最近天数（action=recent 时可选，默认 3）",
                "default": 3,
            },
            "query": {
                "type": "string",
                "description": "搜索关键词（action=search 时必填）",
            },
        },
        "required": ["action"],
    }

    risk = "low"
    requires_approval = False
    no_truncate = False

    # ── 运行时注入 ──
    memory_manager: Any = None  # MemoryManager 实例，由 registry 注入

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        """执行记忆操作，返回 ToolResult。"""
        t0 = time_mod.monotonic()
        try:
            return self._execute(action, **kwargs)
        finally:
            elapsed = int((time_mod.monotonic() - t0) * 1000)
            logger.debug("MemoryTool: action=%s, %dms", action, elapsed)

    # ── 核心分发 ──

    def _execute(self, action: str, **kwargs: Any) -> ToolResult:
        # 1. 校验 action
        if action not in ALL_ACTIONS:
            return ToolResult(
                status="error",
                content="",
                error=f"未知 action: {action}，合法值: {sorted(ALL_ACTIONS)}",
                error_type="invalid_argument",
                data={"action": action},
            )

        # 2. 校验 memory_manager 注入
        if self.memory_manager is None:
            return ToolResult(
                status="error",
                content="",
                error="MemoryManager 未注入，memory 工具不可用",
                error_type="unknown",
                data={"action": action},
            )

        # 3. session 守卫（除 permanent_read 外都需要 session）
        if action in _SESSION_REQUIRED and self.memory_manager._session_dir is None:
            return ToolResult(
                status="error",
                content="",
                error=f"action={action} 需要 session，但当前未设置 session_id",
                error_type="no_session",
                data={"action": action},
            )

        # 4. 分发
        if action == "topic_read":
            return self._do_topic_read(kwargs)
        elif action == "topic_write":
            return self._do_topic_write(kwargs)
        elif action == "permanent_read":
            return self._do_permanent_read()
        elif action == "recent":
            return self._do_recent(kwargs)
        elif action == "topics":
            return self._do_topics()
        elif action == "search":
            return self._do_search(kwargs)

        # unreachable
        return ToolResult(
            status="error", content="", error="内部错误", error_type="unknown"
        )

    # ── action 实现 ──

    def _do_topic_read(self, kwargs: dict) -> ToolResult:
        topic = kwargs.get("topic", "")
        if not topic.strip():
            return ToolResult(
                status="error",
                content="",
                error="action=topic_read 需要 topic 参数",
                error_type="invalid_argument",
                data={"action": "topic_read"},
            )
        text = self.memory_manager.read_topic(topic)
        if not text.strip():
            return ToolResult(
                status="success",
                content=f"[主题 '{topic}' 不存在或为空]",
                data={"action": "topic_read", "topic": topic, "empty": True},
            )
        return ToolResult(
            status="success",
            content=text,
            data={"action": "topic_read", "topic": topic},
        )

    def _do_topic_write(self, kwargs: dict) -> ToolResult:
        topic = kwargs.get("topic", "")
        content = kwargs.get("content", "")
        if not topic.strip():
            return ToolResult(
                status="error",
                content="",
                error="action=topic_write 需要 topic 参数",
                error_type="invalid_argument",
                data={"action": "topic_write"},
            )
        if not content.strip():
            return ToolResult(
                status="error",
                content="",
                error="action=topic_write 需要 content 参数",
                error_type="invalid_argument",
                data={"action": "topic_write", "topic": topic},
            )
        try:
            path = self.memory_manager.write_topic(topic, content)
            return ToolResult(
                status="success",
                content=f"已写入主题 '{topic}': {path.name}",
                data={"action": "topic_write", "topic": topic, "file": str(path)},
            )
        except (RuntimeError, IOError) as e:
            return ToolResult(
                status="error",
                content="",
                error=str(e),
                error_type="unknown",
                data={"action": "topic_write", "topic": topic},
            )

    def _do_permanent_read(self) -> ToolResult:
        text = self.memory_manager.read_permanent_memory()
        if not text.strip():
            return ToolResult(
                status="success",
                content="[没有永久记忆]",
                data={"action": "permanent_read", "empty": True},
            )
        return ToolResult(
            status="success",
            content=text,
            data={"action": "permanent_read"},
        )

    def _do_recent(self, kwargs: dict) -> ToolResult:
        days = kwargs.get("days", 3)
        if not isinstance(days, int) or days < 1:
            days = 3
        if days > 30:
            days = 30  # 安全上限
        text = self.memory_manager.read_recent_days(days)
        if not text.strip():
            return ToolResult(
                status="success",
                content=f"[最近 {days} 天没有记忆]",
                data={"action": "recent", "days": days, "empty": True},
            )
        return ToolResult(
            status="success",
            content=text,
            data={"action": "recent", "days": days},
        )

    def _do_topics(self) -> ToolResult:
        names = self.memory_manager.list_topics()
        if not names:
            return ToolResult(
                status="success",
                content="[暂无主题]",
                data={"action": "topics", "empty": True},
            )
        return ToolResult(
            status="success",
            content="\n".join(names),
            data={"action": "topics", "count": len(names)},
        )

    def _do_search(self, kwargs: dict) -> ToolResult:
        query = kwargs.get("query", "")
        if not query.strip():
            return ToolResult(
                status="error",
                content="",
                error="action=search 需要 query 参数",
                error_type="invalid_argument",
                data={"action": "search"},
            )
        results = self.memory_manager.search(query)
        if not results.strip():
            return ToolResult(
                status="success",
                content=f"[未找到与 '{query}' 相关的记忆]",
                data={"action": "search", "query": query, "empty": True},
            )
        return ToolResult(
            status="success",
            content=results,
            data={"action": "search", "query": query},
        )
