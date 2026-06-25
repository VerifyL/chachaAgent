"""
capabilities/registry.py
ToolRegistry — 统一工具注册表。

所有工具在此集中注册，`build_tools()` 和 CLI 共用同一来源，
消除两处重复维护的问题。
"""

from pathlib import Path
from typing import List, Optional


def build_tools(root: Optional[Path] = None, memory_manager=None) -> List:
    """返回完整的工具列表（单一路径）。"""
    from capabilities.builtins.project_overview import ProjectOverviewTool
    from capabilities.builtins.file_outline import FileOutlineTool
    from capabilities.builtins.list_files import ListFilesTool
    from capabilities.builtins.depe_analyzer import DepsAnalyzerTool
    from capabilities.builtins.code_intel import CodeIntelTool
    from capabilities.builtins.subagent_tool import SubAgentTool
    from capabilities.builtins.expand_subagent import ExpandSubAgentTool
    from capabilities.builtins.chunk_streamer import ReadFileTool, ReadFilesTool, GrepTool
    from capabilities.builtins.code_patcher import EditFileTool
    from capabilities.builtins.diff_patcher import ApplyPatchTool
    from capabilities.sandbox import Sandbox
    from capabilities.builtins.git_tools import GitDiffTool, GitLogTool, GitStatusTool
    from capabilities.builtins.approval_control import ApprovalControlTool
    from capabilities.builtins.cache_reader import CacheReaderTool
    from capabilities.builtins.memory_tool import (
        LoadMemoryTool, WriteTopicTool, ReadTopicTool,
    )

    tools = [
        ProjectOverviewTool(root=root),
        FileOutlineTool(root=root),
        ListFilesTool(root=root),
        DepsAnalyzerTool(root=root),
        CodeIntelTool(root=root),
        SubAgentTool(),
        ExpandSubAgentTool(),
        ReadFileTool(root=root),
        ReadFilesTool(root=root),
        GrepTool(root=root),
        EditFileTool(root=root),
        ApplyPatchTool(root=root),
        LoadMemoryTool(memory_manager=memory_manager),
        WriteTopicTool(memory_manager=memory_manager),
        ReadTopicTool(memory_manager=memory_manager),
        GitDiffTool(root=root),
        GitLogTool(root=root),
        GitStatusTool(root=root),
        ApprovalControlTool(),
        CacheReaderTool(),
        Sandbox(),
    ]
    
    return tools
