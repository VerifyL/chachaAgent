"""
core/subagent/definitions.py
内置子Agent 类型定义（参考 Claude Code explore/plan/worker）。

LLM 根据 description 自动判断何时委托子Agent。
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class SubAgentDef:
    """子Agent 类型定义"""
    name: str
    description: str           # LLM 用来自动判断是否委托
    system_prompt: str          # 替换主 system_prompt
    tools_whitelist: List[str]  # 允许的工具名
    max_iterations: int = 10
    skip_claude_md: bool = False  # 不加载 CHACHA.md（同 Claude Code Explore）


SUBAGENT_DEFINITIONS: Dict[str, SubAgentDef] = {
    "explore": SubAgentDef(
        name="explore",
        description=(
            "代码库探索子Agent。用于搜索代码结构、查找定义、梳理依赖关系。"
            "use proactively when searching codebase structure or finding definitions across files."
        ),
        system_prompt=(
            "你是代码探索子Agent。只做搜索和发现，不修改任何文件。\n"
            "使用 read_file 和 grep 遍历代码库，找到所有相关信息后返回结构化摘要。\n"
            "返回格式：发现的符号/文件/依赖关系清单。"
        ),
        tools_whitelist=["read_file", "grep"],
        max_iterations=15,
        skip_claude_md=True,
    ),
    "plan": SubAgentDef(
        name="plan",
        description=(
            "规划设计子Agent。用于分析需求、设计架构方案。"
            "use when user asks for architecture planning, design discussion, or multi-step analysis."
        ),
        system_prompt=(
            "你是规划设计子Agent。分析输入需求，输出结构化方案。\n"
            "方案包含：步骤拆解、涉及的文件、风险评估。\n"
            "可使用 read_file/grep 了解现有代码，使用 load_memory 查看历史决策。"
        ),
        tools_whitelist=["read_file", "grep", "load_memory"],
        max_iterations=10,
    ),
    "worker": SubAgentDef(
        name="worker",
        description=(
            "任务执行子Agent。用于独立代码修改、文件操作、重构。"
            "use for isolated code changes, file editing, refactoring tasks."
        ),
        system_prompt=(
            "你是任务执行子Agent。独立完成指定任务后返回结果摘要。\n"
            "可使用 read_file/grep 了解代码，使用 edit_file 修改文件。\n"
            "修改后列出变更清单。不要向用户提问——直接执行并返回结果。"
        ),
        tools_whitelist=["read_file", "grep", "edit_file"],
        max_iterations=10,
    ),
}
