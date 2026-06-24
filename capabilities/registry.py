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
    
    # 加载 ~/.chacha/tools/ 用户自定义工具（同名覆盖内置）
    _load_user_tools(tools, root)

    return tools


def _load_user_tools(tools: List, root: Optional[Path] = None) -> None:
    """扫描 ~/.chacha/tools/*.py，动态加载用户工具。

    同名工具覆盖内置（后加载的覆盖先加载的）。
    自动创建 ~/.chacha/tools/ 目录（如不存在）。
    """
    import importlib.util
    import logging
    import sys

    logger = logging.getLogger(__name__)
    user_dir = Path.home() / ".chacha" / "tools"

    # 首次运行时自动创建目录
    if not user_dir.exists():
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            (user_dir / ".gitkeep").touch()
        except OSError:
            return

    if not user_dir.is_dir():
        return

    for py_file in sorted(user_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name.startswith("."):
            continue
        try:
            module_name = f"_user_tool_{py_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, str(py_file))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            loaded_count = 0
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and hasattr(attr, "name"):
                    try:
                        user_tool = attr(root=root)
                        # 同名覆盖内置
                        tools[:] = [t for t in tools if t.name != user_tool.name]
                        tools.append(user_tool)
                        loaded_count += 1
                    except Exception:
                        pass
            if loaded_count:
                logger.info("加载用户工具: %s (%d 个)", py_file.name, loaded_count)
        except Exception as e:
            logger.warning("加载用户工具失败 %s: %s", py_file.name, e)
