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
    description = "将重要信息记录到长期记忆。使用时机：用户表达偏好、做出决策、修复错误、项目进展时。"
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
