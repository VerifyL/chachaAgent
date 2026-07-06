"""
capabilities/registry.py
ToolRegistry — 统一工具注册表。

所有工具在此集中注册，`build_tools()` 和 CLI 共用同一来源。

当前状态：11 工具全部就绪
 （read / write / edit / bash / grep / glob / task / memory / cache_read / clock / approval_control）。
"""

from pathlib import Path
from typing import List, Optional


def build_tools(
    root: Optional[Path] = None,
    memory_manager=None,
    subagent_spawner=None,
    mcp_tools: Optional[List] = None,
) -> List:
    """返回完整的工具列表（单一路径）。

    逐个添加：read / write / edit / bash / grep / glob / task / memory / cache_read / approval_control
    若提供 mcp_tools，则合并到工具列表末尾。
    统一注入 project_root，确保所有工具可用。
    """
    # ✅ read — 读取文件内容
    from capabilities.builtins.read_tool import ReadTool
    read_tool = ReadTool()
    # ✅ write — 创建/覆盖文件
    from capabilities.builtins.write_tool import WriteTool
    write_tool = WriteTool()
    # ✅ edit — 精确文本替换
    from capabilities.builtins.edit_tool import EditTool
    edit_tool = EditTool()
    # ✅ bash — 执行 shell 命令
    from capabilities.builtins.bash_tool import BashTool
    bash_tool = BashTool()
    # ✅ grep — 正则文本搜索
    from capabilities.builtins.grep_tool import GrepTool
    grep_tool = GrepTool()
    # ✅ glob — 按模式查找文件
    from capabilities.builtins.glob_tool import GlobTool
    glob_tool = GlobTool()
    # ✅ task — 委派子Agent 执行独立任务
    from capabilities.builtins.task_tool import TaskTool
    task_tool = TaskTool()
    if subagent_spawner:
        task_tool.configure(subagent_spawner=subagent_spawner)
    # ✅ memory — 管理项目记忆
    from capabilities.builtins.memory_tool import MemoryTool
    memory_tool = MemoryTool()
    memory_tool.memory_manager = memory_manager
    # ✅ cache_read — 续读被截断的缓存输出
    from capabilities.builtins.cache_read_tool import CacheReadTool
    cache_read_tool = CacheReadTool()
    # ✅ clock — 获取当前日期时间
    from capabilities.builtins.clock_tool import ClockTool
    clock_tool = ClockTool()
    # ✅ approval_control — 查询/设置审批旁路
    from capabilities.builtins.approval_control import ApprovalControl
    approval_control = ApprovalControl()

    tools = [
        read_tool,
        write_tool,
        edit_tool,
        bash_tool,
        grep_tool,
        glob_tool,
        task_tool,
        memory_tool,
        cache_read_tool,
        clock_tool,
        approval_control,
    ]

    # 统一注入 project_root（之前仅 approval_control 有）
    if root is not None:
        for tool in tools:
            if hasattr(tool, 'project_root'):
                tool.project_root = root

    # 合并 MCP 工具（若有）
    if mcp_tools:
        tools.extend(mcp_tools)

    return tools
