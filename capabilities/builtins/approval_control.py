"""
capabilities/builtins/approval_control.py
ApprovalControlTool - 审批模式控制工具。

LLM 调用 set_approval_mode(mode, categories, persist) 来关闭/开启审批旁路。
off: 关闭指定分类审批; on: 恢复; status: 查询当前状态。
默认会话级，persist=True 写入 ~/.chacha/settings.json 持久化。
"""

from typing import Any, Dict, List, Optional, Set

from capabilities.base import BaseTool

CATEGORY_HELP = "all | bash | file_write | shell"


class ApprovalControlTool(BaseTool):
    """审批模式控制工具。"""

    name = "set_approval_mode"
    description = (
        "Set approval bypass mode for tool categories. "
        "mode='off': skip approval for given categories. "
        "mode='on': restore approval. "
        "mode='status': show current bypass state. "
        "categories: " + CATEGORY_HELP + " (default: ['all']). "
        "persist=true: save to ~/.chacha/settings.json for permanent bypass. "
        "Default is session-only (cleared on exit)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["off", "on", "status"],
                "description": "off=bypass approval, on=restore, status=show state",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool categories: " + CATEGORY_HELP + ". Default: ['all']",
            },
            "persist": {
                "type": "boolean",
                "description": "Save to ~/.chacha/settings.json for persistence. Default: false.",
            },
        },
        "required": ["mode"],
    }
    risk = "medium"
    requires_approval = False

    def __init__(self, policy_engine=None):
        self._policy = policy_engine

    def configure(self, policy_engine=None, **kwargs) -> None:
        """接收运行时注入的 PolicyEngine。"""
        if policy_engine is not None:
            self._policy = policy_engine

    async def execute(
        self,
        mode: str = "status",
        categories: Optional[List[str]] = None,
        persist: bool = False,
    ) -> str:
        if self._policy is None:
            return "PolicyEngine not available."

        if mode == "status":
            status = self._policy.get_bypass_status()
            lines = ["Current bypass status:"]
            lines.append(f"  session:    {status['session'] or '(none)'}")
            lines.append(f"  persistent: {status['persistent'] or '(none)'}")
            lines.append(f"  effective:  {status['effective'] or '(none)'}")
            return "\n".join(lines)

        cats = categories or ["all"]
        valid = set(self._policy._category_map.keys()) | {"all"}
        invalid = set(cats) - valid
        if invalid:
            return f"Invalid categories: {sorted(invalid)}. Valid: {sorted(valid)}"

        if mode == "off":
            resolved = self._policy.enable_bypass(cats, persist=persist)
            scope = "persistent" if persist else "session"
            return (
                f"Approval bypassed ({scope}): {sorted(resolved)}. "
                "These tools will no longer require approval."
            )

        if mode == "on":
            resolved = self._policy.disable_bypass(cats, persist=persist)
            scope = "persistent" if persist else "session"
            return (
                f"Approval restored ({scope}): {sorted(resolved)}. "
                "These tools now require approval again."
            )

        return f"Unknown mode: {mode}"
