"""
capabilities/base.py
BaseTool — 统一工具基类，所有工具实现此抽象。

用法:
    class ReadTool(BaseTool):
        name = "read"
        description = "读取文件内容"
        async def execute(self, path: str) -> str: ...
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

from capabilities.result import ToolResult


class BaseTool(ABC):
    """工具基类 — 定义自描述 + 执行接口 + 安全策略"""

    # ====== 元数据（子类覆盖） ======

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}
    risk: str = "low"  # low | medium | high
    requires_approval: bool = False  # 是否需要用户确认
    no_truncate: bool = False  # 设为 True 时 ToolExecutor 不截断该工具的输出

    # ====== 运行时注入 ======

    project_root: Optional[Path] = None  # 项目根目录（由 registry 注入）

    # ====== 子类实现 ======

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具，返回 ToolResult"""
        ...

    # ====== 自动生成 ======

    def to_function_schema(self) -> Dict[str, Any]:
        """生成 LLM function calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_context_metadata(self) -> Dict[str, Any]:
        """生成工具上下文元数据（PolicyEngine 审批用）"""
        return {
            "name": self.name,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
        }
