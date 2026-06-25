"""
core/project_init.py
ProjectInit — 项目初始化器。统一构造 system_prompt + 工具列表 + MemoryManager。

CLI / Web / API 前端只需调用 create()，不直接接触底层对象。
"""

from pathlib import Path
from typing import List

from core.context.memory_manager import MemoryManager
from core.context.static_rule_loader import StaticRuleLoader


class ProjectInit:
    """项目初始化器（高内聚：前端无需关心底层构造细节）"""

    def __init__(self, project_root: Path, session_id: str = ""):
        self._root = project_root
        self._session_id = session_id

        # 加载 CHACHA.md
        self._rules = StaticRuleLoader(self._root).load()

        # 创建 MemoryManager（session 级别）
        self._memory = MemoryManager(
            project_root=self._root, session_id=self._session_id,
        )

    # ====== Getters ======

    @property
    def memory_manager(self) -> MemoryManager:
        return self._memory

    @property
    def session_id(self) -> str:
        return self._session_id

    def build_system_prompt(self) -> str:
        """构建系统提示词（CHACHA.md 自动注入为宪法）。"""
        prompt = (
            "你是 ChachaAgent。当前项目根目录: " + str(self._root) + "。\n"
            "使用提供的工具操作文件和记忆。回复简洁直接，中文优先。\n"
            "所有文件路径均相对于此根目录。"
        )
        if self._rules:
            prompt += f"\n\n--- 项目宪法 (CHACHA.md) ---\n{self._rules}"
        return prompt

    def build_tools(self) -> List:
        """构建工具列表（MemoryManager 已注入）。"""
        from capabilities.registry import build_tools
        return build_tools(root=self._root, memory_manager=self._memory)
