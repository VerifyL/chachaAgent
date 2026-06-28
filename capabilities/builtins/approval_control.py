"""
capabilities/builtins/approval_control.py
ApprovalControl — 查询/设置审批旁路状态。

为 LLM 暴露 PolicyEngine 的 enable_bypass / disable_bypass / get_bypass_status。
自身 requires_approval=False，避免"旁路工具本身需要审批"的悖论。
"""

import logging
import time as time_mod
from typing import Any, Optional

from capabilities.base import BaseTool
from capabilities.result import ToolResult

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"enable", "disable", "status"}


class ApprovalControl(BaseTool):
    """查询或设置审批旁路：跳过指定工具/分类的审批流程。"""

    name = "approval_control"
    description = (
        "管理审批旁路状态：enable 开启指定工具/分类的旁路（跳过审批），"
        "disable 关闭旁路（恢复审批），status 查看当前旁路状态。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "操作类型：enable（开启旁路）、disable（关闭旁路）、"
                    "status（查看旁路状态）"
                ),
                "enum": sorted(VALID_ACTIONS),
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "工具分类名或工具名列表。支持: memory, readonly, system, edit, "
                    "* (全局), 或具体工具名如 bash/write/edit。"
                    "action=status 时无需提供。"
                ),
            },
            "persist": {
                "type": "boolean",
                "description": (
                    "是否持久化到 ~/.chacha/settings.json。"
                    "默认 false（仅当前会话有效）。action=status 时无需提供。"
                ),
                "default": False,
            },
        },
        "required": ["action"],
    }

    risk = "medium"
    requires_approval = False  # 自身不走审批，否则形成悖论
    no_truncate = False

    # ── 运行时注入 ──
    _policy_engine: Optional[Any] = None

    def configure(self, policy_engine=None, **kwargs) -> None:
        """接收 PolicyEngine 引用（由 agent_bridge.rebuild() 注入）。"""
        if policy_engine is not None:
            self._policy_engine = policy_engine

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        """执行旁路操作，返回 ToolResult。"""
        t0 = time_mod.monotonic()
        try:
            return self._execute(action, **kwargs)
        finally:
            elapsed = int((time_mod.monotonic() - t0) * 1000)
            logger.debug("ApprovalControl: action=%s, %dms", action, elapsed)

    # ── 核心分发 ──

    def _execute(self, action: str, **kwargs: Any) -> ToolResult:
        # 1. 校验 action
        if action not in VALID_ACTIONS:
            return ToolResult(
                status="error",
                content="",
                error=f"未知 action: {action}，合法值: {sorted(VALID_ACTIONS)}",
                error_type="invalid_argument",
                data={"action": action},
            )

        # 2. 校验 policy_engine 注入
        if self._policy_engine is None:
            return ToolResult(
                status="error",
                content="",
                error="PolicyEngine 未注入，approval_control 工具不可用",
                error_type="unknown",
                data={"action": action},
            )

        # 3. 分发
        if action == "status":
            return self._do_status()
        elif action == "enable":
            return self._do_enable(kwargs)
        elif action == "disable":
            return self._do_disable(kwargs)

        # unreachable
        return ToolResult(
            status="error", content="", error="内部错误", error_type="unknown"
        )

    # ── action 实现 ──

    def _do_status(self) -> ToolResult:
        """查询当前旁路状态。"""
        data = self._policy_engine.get_bypass_status()
        lines = [
            f"会话级旁路: {', '.join(data['session']) if data['session'] else '(无)'}",
            f"持久化旁路: {', '.join(data['persistent']) if data['persistent'] else '(无)'}",
            f"实际生效:   {', '.join(data['effective']) if data['effective'] else '(无)'}",
        ]
        return ToolResult(
            status="success",
            content="\n".join(lines),
            data=data,
        )

    def _do_enable(self, kwargs: dict) -> ToolResult:
        """开启审批旁路。"""
        categories = kwargs.get("categories", [])
        persist = kwargs.get("persist", False)
        if not categories:
            return ToolResult(
                status="error",
                content="",
                error="action=enable 需要 categories 参数（非空列表）",
                error_type="invalid_argument",
                data={"action": "enable"},
            )
        resolved = self._policy_engine.enable_bypass(categories, persist=persist)
        scope = "持久化" if persist else "会话级"
        return ToolResult(
            status="success",
            content=f"已开启{scope}旁路: {', '.join(sorted(resolved)) if resolved else '(无匹配)'}",
            data={"resolved": sorted(resolved), "persist": persist},
        )

    def _do_disable(self, kwargs: dict) -> ToolResult:
        """关闭审批旁路。"""
        categories = kwargs.get("categories", [])
        persist = kwargs.get("persist", False)
        if not categories:
            return ToolResult(
                status="error",
                content="",
                error="action=disable 需要 categories 参数（非空列表）",
                error_type="invalid_argument",
                data={"action": "disable"},
            )
        resolved = self._policy_engine.disable_bypass(categories, persist=persist)
        scope = "持久化" if persist else "会话级"
        return ToolResult(
            status="success",
            content=f"已关闭{scope}旁路: {', '.join(sorted(resolved)) if resolved else '(无匹配)'}",
            data={"resolved": sorted(resolved), "persist": persist},
        )
