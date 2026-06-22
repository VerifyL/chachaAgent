"""
interface/cli/app.py
ChachaAgent CLI — prompt_toolkit + Rich。
Enter 发送，Shift+Enter 换行，支持多行粘贴/编辑。
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.table import Table
from rich import box

from interface.cli.agent_bridge import AgentBridge
from core.project_init import ProjectInit
from core.session_service import SessionService
from core.cli_theme import load_theme, write_default_theme

RICH_CONSOLE = Console()

# ====== 多行续写标记 ======
CONTINUE_MARKER = "... "


class ChachaCLI:
    """基于 prompt_toolkit + Rich 的 CLI"""

    def __init__(self, project_root: str = "."):
        self._project = Path(project_root).resolve()
        self._bridge: Optional[AgentBridge] = None
        self._session: Optional[SessionService] = None
        self._sending = False
        self._debug = False

        # 主题
        write_default_theme()
        self._t = load_theme()

    # ====== 启动 ======

    async def initialize(self) -> str:
        self._ensure_default_constitution()
        self._session = SessionService(self._project)
        si = self._session.project_init

        self._bridge = AgentBridge(
            system_prompt=si.build_system_prompt(),
            tools=si.build_tools(),
            project_root=self._project,
        )
        msg = await self._bridge.initialize()
        self._session.set_llm(self._bridge._invoker)
        # 告诉 ChatEngine checkpoint 目录
        self._bridge._engine.set_checkpoint_dir(
            self._session.memory_manager._session_dir)
        return msg

    # ====== 主循环 ======

    @staticmethod
    def _ensure_default_constitution() -> None:
        """首次运行自动创建 ~/.chacha/CHACHA.md"""
        import shutil
        root_md = Path.home() / ".chacha" / "CHACHA.md"
        if root_md.exists():
            return
        builtin = Path(__file__).resolve().parents[2] / "core" / "CHACHA.md.template"
        if builtin.exists():
            root_md.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(builtin, root_md)

    async def run(self) -> None:
        init_msg = await self.initialize()

        # 欢迎
        self._print_system("ChachaAgent v0.2 — " + self._project.name)
        self._print_system(init_msg)
        if self._session.project_init._rules:
            self._print_system("[cyan]📜 CHACHA.md 已加载[/]")
        self._print_system("Ctrl+N 新会话  Ctrl+S 保存  Ctrl+D 调试  Ctrl+B 会话列表  /help 命令")
        self._print_system("")

        # 输入循环
        session = PromptSession(
            history=FileHistory(str(Path.home() / ".chacha" / "cli_history")),
            key_bindings=self._make_bindings(),
            multiline=False,  # 单行模式，Shift+Enter 自动续行
            bottom_toolbar=self._status_text,
        )

        while True:
            try:
                RICH_CONSOLE.print()  # 空行隔开
                text = await session.prompt_async(
                    HTML(f"<{self._t['prompt']}>❯ </{self._t['prompt']}>"),
                )
            except KeyboardInterrupt:
                continue  # Ctrl+C 清空输入
            except EOFError:
                break  # Ctrl+D 退出

            text = text.strip()
            if not text:
                continue
            if text == "/exit":
                self._print_system("👋 再见")
                break

            await self._handle_input(text)

    # ====== 输入处理 ======

    async def _handle_input(self, text: str) -> None:
        if text.startswith("/"):
            result = await self._handle_command(text)
            self._print_system(result)
        else:
            await self._run_dialog(text)

    async def _run_dialog(self, text: str) -> None:
        import time
        t0 = time.monotonic()
        tokens = 0
        errors: list[str] = []
        response_parts: list[str] = []
        tool_trace: list[dict] = []
        in_tools = False

        self._print_user(text)
        RICH_CONSOLE.print(f"[{self._t['agent_header']}]🤖 Chacha[/]")

        try:
            async for chunk in self._bridge.send_message(text):
                if chunk["type"] == "text":
                    response_parts.append(chunk["content"])
                    if in_tools:
                        # 工具阶段后的最终文本 → 关闭工具块再流式
                        in_tools = False
                        RICH_CONSOLE.print(f"[{self._t['separator']}]" + "─" * 30 + "[/]")
                    RICH_CONSOLE.print(chunk["content"], end="")
                elif chunk["type"] == "tool_call_start":
                    if not in_tools:
                        in_tools = True
                        RICH_CONSOLE.print()  # 换行
                        RICH_CONSOLE.print(
                            f"[{self._t['separator']}]" + "━" * 30 + "[/]"
                        )
                    tool_trace.append({
                        "tool": chunk["tool_name"], "t0": time.monotonic(),
                    })
                    RICH_CONSOLE.print(
                        f"  [{self._t['tool_thinking']}]🔧 {chunk['tool_name']}[/]"
                    )
                elif chunk["type"] == "tool_call_end":
                    if tool_trace:
                        t = tool_trace[-1]
                        t["ms"] = int((time.monotonic() - t["t0"]) * 1000)
                    preview = chunk.get("preview", "")[:80]
                    RICH_CONSOLE.print(
                        f"  [{self._t['tool_done']}]✅ {chunk['tool_name']} — {preview}[/]"
                    )
                elif chunk["type"] == "error":
                    errors.append(chunk["message"])
                    RICH_CONSOLE.print(f"[red]错误: {chunk['message']}[/]")
                elif chunk["type"] == "done":
                    tokens = chunk.get("tokens", 0)
                elif chunk["type"] == "compact":
                    self._print_system(f"🔄 自动压缩: {chunk['reason']}")
        except Exception as e:
            errors.append(str(e))
            RICH_CONSOLE.print(f"[red]异常: {e}[/]")

        # 审计
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        self._session.add_round(
            tokens=tokens, duration_ms=elapsed_ms, errors=errors,
            user_input=text, assistant_text="".join(response_parts),
        )

        audit = f"⏱ {elapsed_ms}ms  |  💬 {tokens}T  |  🔄 第{self._session.rounds}轮"
        if errors:
            audit += f"  |  ⚠ {len(errors)}错"
        if self._debug and tool_trace:
            steps = "; ".join(f"{t['tool']}({t.get('ms','?')}ms)" for t in tool_trace)
            audit += f"  |  🐛 {steps}"
        RICH_CONSOLE.print()
        RICH_CONSOLE.print(f"[{self._t['audit']}]{audit}[/]")
        RICH_CONSOLE.print(f"[{self._t['separator']}]" + "─" * 40 + "[/]")

        # Checkpoint 已在 ChatEngine.save_checkpoint() 中自动保存

    # ====== 命令 ======

    async def _handle_command(self, text: str) -> str:
        parts = text.lstrip("/").strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # 配置
        if cmd in ("model", "url", "key"):
            return await self._bridge.handle_command(text)

        # Session
        if cmd == "session":
            return await self._session_cmd(arg)
        if cmd == "new":
            sid = self._session.new()
            await self._bridge.reset()
            return f"🆕 新会话: {sid}"
        if cmd == "save":
            self._bridge._engine.save_checkpoint()
            return f"💾 Checkpoint 已保存 ({len(self._bridge._messages)} 条)"

        # 记忆
        if cmd == "memory":
            days = self._session.memory_manager.list_days()
            return "📁 记忆:\n" + "\n".join(f"  {d}.md" for d in days) if days else "暂无"
        if cmd == "dream":
            return f"🎯 {await self._session.run_dream()}"
        if cmd == "dreamglobal":
            return f"🌍 {await self._session.run_global_dream()}"

        # 调试
        if cmd == "debug":
            self._debug = not self._debug
            return f"🐛 Debug: {'开' if self._debug else '关'}"
        if cmd == "audit":
            return self._session.audit_report()
        if cmd == "status":
            return self._session.status_report()
        if cmd == "compact":
            return await self._do_compact()

        if cmd == "help":
            self._print_help()
            return ""

        return await self._bridge.handle_command(text)

    async def _session_cmd(self, arg: str) -> str:
        sessions = self._session.list_sessions()
        if not arg:
            return self._render_session_list(sessions)

        sub, sub_arg = (arg.split(None, 1) + [""])[:2]
        if sub == "del":
            return await self._del_by_index(sub_arg, sessions)
        if sub == "new":
            sid = self._session.new()
            await self._bridge.reset()
            return f"🆕 新 session: {sid}"

        # 按编号切换
        return await self._switch_by_index(sub, sessions)

    def _render_session_list(self, sessions: list) -> str:
        if not sessions:
            return "暂无 session"
        lines = ["📂 Sessions:"]
        for i, s in enumerate(sessions, 1):
            marker = "●" if s["id"] == self._session.session_id else " "
            lines.append(f"  {marker} [{i}] {s['time']}  {s['preview'][:40]}")
        lines.append("  /session <#> 切换  /session del <#> 删除")
        return "\n".join(lines)

    def _session_by_index(self, idx_str: str, sessions: list) -> str:
        try:
            idx = int(idx_str)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1]["id"]
        except ValueError:
            pass
        return idx_str  # 尝试按真实 ID

    async def _switch_by_index(self, idx_str: str, sessions: list) -> str:
        sid = self._session_by_index(idx_str, sessions)
        old_sid = self._session.session_id
        result = await self._session.switch_to(sid)
        if "不存在" not in result and "已经" not in result:
            await self._reload_bridge(old_sid)
        return result

    async def _del_by_index(self, idx_str: str, sessions: list) -> str:
        sid = self._session_by_index(idx_str, sessions)
        return await self._session.delete_session(sid)

    async def _reload_bridge(self, old_sid: str) -> None:
        # 保存旧 session
        self._bridge._engine.save_checkpoint()
        # 重置引擎并切换 checkpoint 目录
        self._bridge._engine.reset()
        self._bridge._engine.set_checkpoint_dir(
            self._session.memory_manager._session_dir)

    async def _do_compact(self) -> str:
        if not self._bridge:
            return "未连接"
        try:
            from core.context.context_compressor import ContextCompressor
            n = len(self._bridge._messages)
            msgs, _ = ContextCompressor.auto_compact(
                self._bridge._messages,
                getattr(self._bridge, "_context_window", 1_048_576),
                llm=getattr(self._bridge, "_invoker", None),
                **getattr(self._bridge, "_compress_cfg", {}),
            )
            self._bridge._messages = msgs
            return f"📦 {n} → {len(msgs)} 条"
        except Exception as e:
            return f"压缩失败: {e}"

    # ====== 键绑定 ======

    def _make_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-n")
        def _(event):
            sid = self._session.new()
            asyncio.create_task(self._bridge.reset())
            self._print_system(f"🆕 新会话: {sid}")

        @kb.add("c-s")
        def _(event):
            self._bridge._engine.save_checkpoint()
            self._print_system(f"💾 Checkpoint 已保存 ({len(self._bridge._messages)} 条)")

        @kb.add("c-f")
        def _(event):
            self._debug = not self._debug
            self._print_system(f"🐛 Debug: {'开' if self._debug else '关'}")

        @kb.add("c-b")
        def _(event):
            self._print_sidebar()

        @kb.add("c-x")
        async def _(event):
            result = await self._do_compact()
            self._print_system(result)

        @kb.add("c-l")
        def _(event):
            RICH_CONSOLE.clear()

        return kb

    def _status_text(self) -> str:
        if not self._bridge:
            return "ChachaAgent v0.2"
        extra = ""
        if self._session and self._session.rounds:
            extra = f" | 💬 {self._session.total_tokens}T | 🔄 {self._session.rounds}轮"
        return f"{self._bridge.model}{extra}"

    # ====== 输出 ======

    def _print_user(self, text: str) -> None:
        """用户输入 Panel：圆角 + 黄边 + 黄色粗体文字"""
        from rich.panel import Panel
        RICH_CONSOLE.print()
        RICH_CONSOLE.print(Panel(
            f"[{self._t['user_text']}]{text}[/]",
            title=f"[{self._t['user_title']}] ❯ You [/]",
            title_align="left",
            border_style=self._t["user_border"],
            box=box.ROUNDED,
            padding=(0, 1),
        ))

    def _print_system(self, text: str) -> None:
        RICH_CONSOLE.print(f"[{self._t['system']}]{text}[/]")

    def _print_tool(self, text: str, style: str = "status") -> None:
        if style == "thinking":
            RICH_CONSOLE.print(f"  [{self._t['tool_thinking']}]🔧 {text}[/]")
        else:
            RICH_CONSOLE.print(f"  [{self._t['tool_done']}]✅ {text}[/]")

    def _print_sidebar(self) -> None:
        sessions = self._session.list_sessions()
        if not sessions:
            self._print_system("暂无 session")
            return
        RICH_CONSOLE.print()
        table = Table(box=box.SIMPLE, show_header=True,
                      header_style="bold cyan", border_style="dim")
        table.add_column("#", width=4, style="bold yellow")
        table.add_column("", width=2)
        table.add_column("时间", width=14)
        table.add_column("预览", width=50)
        for i, s in enumerate(sessions, 1):
            marker = "●" if s["id"] == self._session.session_id else ""
            table.add_row(str(i), marker, s.get("time", ""), s.get("preview", ""))
        RICH_CONSOLE.print(table)
        self._print_system("  /session <#> 切换  /session del <#> 删除  /session new 新建")

    def _print_help(self) -> None:
        items = {
            "配置": [
                ("/model <name>", "切换模型"),
                ("/url <url>", "切换 API URL"),
                ("/key <sk->", "设置 API Key"),
                ("/status", "系统状态"),
            ],
            "Session": [
                ("/session", "列出所有 session"),
                ("/session <id>", "切换到指定 session"),
                ("/session del <id>", "删除 session（含记忆）"),
                ("/session new", "新建 session"),
                ("/new", "新建 session（快捷）"),
                ("/save", "保存 checkpoint"),
            ],
            "记忆": [
                ("/memory", "查看记忆文件"),
                ("/dream", "运行 Session Dream"),
                ("/dream global", "运行 GlobalDream"),
            ],
            "调试": [
                ("/audit", "完整审计报告"),
                ("/compact", "压缩上下文"),
                ("/debug", "切换调试模式"),
            ],
            "快捷键": [
                ("Ctrl+N", "新会话"),
                ("Ctrl+S", "保存 checkpoint"),
                ("Ctrl+D", "调试模式"),
                ("Ctrl+B", "会话列表"),
                ("Ctrl+X", "压缩上下文"),
                ("Ctrl+L", "清屏"),
            ],
        }
        table = Table(box=box.SIMPLE, show_header=False, border_style="dim yellow")
        table.add_column(style="bold yellow", width=22)
        table.add_column(style="bright_white")
        for cat, cmds in items.items():
            table.add_section()
            RICH_CONSOLE.print(f"\n[{self._t['help_title']}] {cat} [/]")
            for cmd, desc in cmds:
                RICH_CONSOLE.print(
                    f"  [{self._t['help_cmd']}]{cmd:<20}[/]  "
                    f"[{self._t['help_desc']}]{desc}[/]"
                )
        return ""


# ====== 入口 ======

def main():
    project = sys.argv[1] if len(sys.argv) > 1 else "."
    cli = ChachaCLI(project)
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
