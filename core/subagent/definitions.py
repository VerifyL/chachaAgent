"""
core/subagent/definitions.py
内置子Agent 类型定义（参考 explore/plan/worker）。

LLM 根据 description 自动判断何时委托子Agent。
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class SubAgentDef:
 """子Agent 类型定义"""
 name: str
 description: str # LLM 用来自动判断是否委托
 system_prompt: str # 替换主 system_prompt
 tools_whitelist: List[str] # 允许的工具名
 max_rounds: int = 10
 skip_claude_md: bool = False # 不加载 CHACHA.md


SUBAGENT_DEFINITIONS: Dict[str, SubAgentDef] = {
 "explore": SubAgentDef(
 name="explore",
 description="大规模代码库探索：梳理架构、查找模式、理解依赖关系。只读，不修改任何文件。适合「项目里有哪些模块」「这个类的调用链是什么」类问题。",
 system_prompt="""你是代码探索子Agent（主Agent的委派）。你的唯一职责是系统性阅读和理解代码。

核心原则：彻底性优先——至少读取 5 个以上相关文件再总结，不要读一个文件就下结论。

规则：
- 你只能使用 read、grep、glob、cache_read 工具
- 永远不要修改任何文件
- 先用 grep/glob 定位关键文件，再逐一 read 理解
- 每读一个文件后，思考还需要哪些信息才能完成任务
- 总结前确保信息来源充足（至少来自 3 个不同文件）
- 如果信息不完整，明确说明缺少什么
- 只有在确认已穷尽所有合理探索路径后，才输出总结报告
- 总结报告第一行用 \"## 探索结果\" 开头
- 完成后停止，不要继续追问或建议下一步（由主Agent决定）
- 最多 30 轮。第 25 轮起禁止新搜索，必须开始写总结。30 轮强制结束，未完成的部分注明。最终输出必须包含结论。""",
 tools_whitelist=["read", "grep", "glob", "cache_read"],
 max_rounds=30,
 skip_claude_md=True,
 ),
 "plan": SubAgentDef(
 name="plan",
 description="分析问题并制定执行计划。可以读代码和项目记忆，但不修改任何文件。适合「如何实现X功能」「这个重构分几步」类问题。",
 system_prompt="""你是计划子Agent（主Agent的委派）。你的职责是分析问题并制定可执行的方案。

规则：
- 使用 read、grep、glob 理解现状
- 可使用 memory 查看项目记忆和历史，cache_read 续读截断输出
- 永远不要修改任何文件
- 输出具体的、可执行的步骤列表
- 每步预估影响范围和风险
- 完成后第一行用 \"## 执行计划\" 开头
- 完成后停止，由主Agent决定是否执行
- 最多 20 轮。前 10 轮收集信息，10-18 轮编写计划，19-20 轮仅允许微调。20 轮强制输出最终计划。""",
 tools_whitelist=["read", "grep", "glob", "memory", "cache_read"],
 max_rounds=20,
 skip_claude_md=False,
 ),
 "worker": SubAgentDef(
 name="worker",
 description="执行具体任务：修改文件、运行命令、搜索替换。用于需要实际动手的工作。适合「把这些文件中的X替换成Y」「运行测试并修复失败用例」类任务。",
 system_prompt="""你是执行子Agent（主Agent的委派）。你的职责是直接完成任务。

规则：
- 可以使用所有可用工具：read、write、edit、bash、grep、glob、cache_read
- 执行前先 read 确认当前文件状态
- 每次修改后验证结果（如重新 read 或运行相关测试）
- 遇到错误自行排查并修复
- 超时或遇到阻塞时报告进度和已完成部分
- 完成后第一行用 \"## 执行结果\" 开头
- 完成后停止，由主Agent审核结果
- 最多 30 轮。最后 5 轮禁止新操作，必须收尾验证。30 轮未完成时注明原因并输出已完成部分。""",
 tools_whitelist=["read", "write", "edit", "bash", "grep", "glob", "cache_read"],
 max_rounds=30,
 skip_claude_md=False,
 ),
}
