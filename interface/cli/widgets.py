"""
interface/cli/widgets.py
ChatMessage / ToolCallBanner / StatusBar — CLI 小组件。
"""

from typing import Optional

from textual.widgets import Label, Static


class ChatMessage:
    """聊天消息渲染"""

    @staticmethod
    def prefix(role: str) -> str:
        """返回角色标签（不包含内容，用于流式渲染开头）"""
        if role == "user":
            return "\n[bold magenta]You:[/bold magenta]\n"
        elif role == "assistant":
            return "\n[bold cyan]Chacha:[/bold cyan]\n"
        return "\n[dim]System:[/dim]\n"

    @staticmethod
    def render(role: str, content: str) -> str:
        """返回 RichLog 兼容的 Markup 字符串"""
        prefix = ChatMessage.prefix(role)
        return f"{prefix}{content}\n"


class ToolCallBanner:
    """工具调用横幅"""

    @staticmethod
    def render(tool_name: str, stage: str, preview: str = "") -> str:
        """工具调用进度显示"""
        if stage == "start":
            return f"\n[bold yellow]🔧 Calling {tool_name}...[/bold yellow]"
        elif stage == "end":
            p = f"\n[dim]{preview[:200]}[/dim]" if preview else ""
            return f"[dim]✅ {tool_name} done[/dim]{p}"
        return ""


class StatusBar(Static):
    """底部状态栏：模型 | Token | 轮次 | 思考中..."""

    def update(
        self,
        model: str = "",
        tokens: int = 0,
        rounds: int = 0,
        session: str = "",
        extra: str = "",
    ) -> None:
        parts = []
        if model:
            parts.append(f"[bold]{model}[/]")
        if tokens:
            parts.append(f"💬 {tokens}")
        if rounds:
            parts.append(f"🔄 {rounds}轮")
        if extra:
            parts.append(f"[yellow]{extra}[/]")
        text = "  |  ".join(parts) if parts else "就绪"
        super().update(text)
