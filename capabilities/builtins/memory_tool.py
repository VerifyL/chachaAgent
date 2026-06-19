"""
capabilities/builtins/memory_tool.py
LoadMemory + Remember — 记忆读写工具（BaseTool）。

LLM 自主调用：
  load_memory(query) → 搜索所有记忆文件
  remember(content)  → 写入今日记忆
"""

import logging
from typing import Optional

from capabilities.base import BaseTool
from core.context.memory_manager import MemoryManager

logger = logging.getLogger(__name__)


class LoadMemoryTool(BaseTool):
    """搜索记忆：load_memory(query=None) → 无参数列出日期，有参数跨所有文件搜索"""

    name = "load_memory"
    description = "读取或搜索过去的记忆。无参数时列出可用日期文件，传入 query 时搜索相关内容。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（可选，为空则列出可用日期）"},
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, memory_manager: Optional[MemoryManager] = None):
        self._mgr = memory_manager
        #self._mgr = memory_manager or MemoryManager()

    async def execute(self, query: str = "") -> str:
        if self._mgr is None:
            return "记忆系统未初始化"
        if not query.strip():
            days = self._mgr.list_days(limit=20)
            if not days:
                return "暂无记忆文件。"
            return "可用记忆日期:\n" + "\n".join(f"  {d}.md" for d in days)

        result = self._mgr.search(query)
        return result or f"未找到与 '{query}' 相关的记忆。"


class RememberTool(BaseTool):
    """写入记忆：remember(content) → 追加到今日文件"""

    name = "remember"
    description = "将当前对话中的重要信息记录到今日记忆（7天后自动清理）。如需长期保留，请用 write_topic 写入对应主题。"


    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容（简洁摘要）"},
        },
        "required": ["content"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, memory_manager: Optional[MemoryManager] = None):
        self._mgr = memory_manager
        #self._mgr = memory_manager or MemoryManager()

    async def execute(self, content: str) -> str:
        if self._mgr is None:
            return "记忆系统未初始化"
        path = self._mgr.remember(content.strip())
        return f"已记录到 {path.name}"


class WriteTopicTool(BaseTool):
    """写入主题记忆：write_topic(topic, content) → 追加到对应主题文件"""

    name = "write_topic"
    description = (
        "将重要信息记录到对应主题的长期记忆中。适用场景："
        "用户明确表达偏好 → topic='user-preferences'；"
        "项目级技术/架构决策 → topic='project-decisions'；"
        "踩坑教训、值得记住的经验 → topic='lessons-learned'；"
        "成功修复的错误及方案 → topic='errors-fixed'；"
        "当前项目的里程碑、进度 → topic='project-progress'。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "主题名，五个选项之一：user-preferences | project-decisions | lessons-learned | errors-fixed | project-progress",
            },
            "content": {
                "type": "string",
                "description": "要记录的内容（简洁摘要，支持多行）",
            },
        },
        "required": ["topic", "content"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, memory_manager=None):
        self._mgr = memory_manager

    async def execute(self, topic: str, content: str) -> str:
        if self._mgr is None:
            return "记忆系统未初始化"
        valid_topics = {"user-preferences", "project-decisions", "lessons-learned", "errors-fixed", "project-progress"}
        if topic not in valid_topics:
            return f"无效主题 '{topic}'。可选: {', '.join(sorted(valid_topics))}"
        path = self._mgr.write_topic(topic, content.strip())
        return f"已记录到 topics/{path.name}"


class ReadTopicTool(BaseTool):
    """读取主题记忆：read_topic(topic) → 返回指定主题内容。不传参数时列出所有主题。"""

    name = "read_topic"
    description = "读取某个主题的长期记忆。不传参数时列出所有可用的主题名称，传入 topic 时返回该主题完整内容。"
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "主题名（可选，不传则列出所有主题）",
            },
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, memory_manager=None):
        self._mgr = memory_manager

    async def execute(self, topic: str = "") -> str:
        if self._mgr is None:
            return "记忆系统未初始化"
        if not topic.strip():
            names = self._mgr.list_topics()
            if not names:
                return "暂无主题记忆文件。"
            return "可用主题:\n" + "\n".join(f"  {n}" for n in names)
        content = self._mgr.read_topic(topic.strip())
        return content or f"[{topic}] 暂无内容或主题不存在。"
