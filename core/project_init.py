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
            project_root=self._root,
            session_id=self._session_id,
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
            "回复简洁直接，中文优先。\n"
            "所有文件路径均相对于此根目录。"
        )
        if self._rules:
            prompt += f"\n\n--- 项目宪法 (CHACHA.md) ---\n{self._rules}"
        # 工具使用规则（工具就绪后生效）
        prompt += (
            "\n\n--- 工具使用规则 ---\n"
            "1. 信任工具结果：truncated=false 时输出完整，byte_count 准确，不重复执行已完成操作。\n"
            "2. edit 精确性：old_string 必须包含足够上下文确保唯一匹配；new_string 必须与 old_string 不同"
            "（相同为无效调用）；写入前 read 确认，写入后 read 验证。\n"
            "3. 高风险操作：write/edit/bash 如不确定后果，先询问用户。\n"
            "4. 回复前自检：用户偏好 / 技术决策 / 修复错误 / 踩坑经验 / 里程碑完成后，调用 memory remember 记录。"
        )
        return prompt

    def build_tools(self) -> List:
        """构建工具列表（MemoryManager 已注入）。"""
        from capabilities.registry import build_tools

        return build_tools(root=self._root, memory_manager=self._memory)
