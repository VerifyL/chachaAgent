"""
interface/cli/app.py
ChachaAgent CLI — Claude Code 风格 Textual TUI。

架构映射:
  CHACHA.md:    启动加载为「宪法」  → StaticRuleLoader
  会话:          /new /save          → SessionManager
  记忆:          /memory /dream       → MemoryManager + DreamPipeline
  压缩:          /compact             → ContextCompressor
  审计:          /audit /trace        → 每轮后自动展示 Token/耗时
  子Agent:       /agent type task     → SubAgentSpawner
  Debug:         Ctrl+D               → token/压缩/规则 预览
"""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Footer, Header, Input, RichLog
from textual.binding import Binding

from interface.cli.widgets import ChatMessage, ToolCallBanner, StatusBar
from interface.cli.agent_bridge import AgentBridge
from interface.cli.session_manager import SessionManager

STYLE_CSS = """
#chat-area { background: $surface; height: 1fr; }
#chat-container { height: 1fr; }
#input-container { height: auto; min-height: 3; padding: 1; border-top: solid $panel-darken-2; }
#main-input { width: 100%; background: $surface; border: solid $primary; }
#status-line { height: 1; dock: bottom; background: $boost; color: $text-muted; padding: 0 1; }
"""


class ChachaApp(App):
    """ChachaAgent CLI"""

    CSS = STYLE_CSS
    TITLE = "ChachaAgent"
    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=False),
        Binding("ctrl+l", "clear_screen", "清屏", show=True),
        Binding("ctrl+s", "save", "保存会话", show=True),
        Binding("ctrl+d", "toggle_debug", "调试面板", show=True),
        Binding("ctrl+n", "new_session", "新会话", show=True),
        Binding("ctrl+x", "compact", "压缩上下文", show=True),
    ]

    def __init__(self, project_root: str = "."):
        super().__init__()
        self._project = Path(project_root).resolve()
        self._bridge: AgentBridge | None = None
        self._session: SessionManager | None = None
        self._sending = False
        self._debug = False

    # ====== 挂载 ======

    async def on_mount(self) -> None:
        from capabilities.builtins.chunk_streamer import ReadFileTool, GrepTool
        from capabilities.builtins.code_patcher import EditFileTool
        from capabilities.builtins.memory_tool import LoadMemoryTool, RememberTool, WriteTopicTool, ReadTopicTool


        # 2. 系统提示词（不硬编码工具名）
        system_prompt = (
            "你是 ChachaAgent。当前项目: " + self._project.name + "。\n"
            "使用提供的工具操作文件和记忆。回复简洁直接，中文优先。"
        )

        # 3. 工具列表（由 function calling 传入，不写在提示词）
        tools = [
            ReadFileTool(root=self._project),
            GrepTool(root=self._project),
            EditFileTool(root=self._project),
            LoadMemoryTool(),
            RememberTool(),
            WriteTopicTool(),
            ReadTopicTool(),
        ]


        # 4. 桥接 + 会话
        self._bridge = AgentBridge(
            system_prompt=system_prompt, tools=tools, project_root=self._project,
        )
        init_msg = await self._bridge.initialize()
        self._session = SessionManager(self._project, self._bridge)

        # 5. 欢迎
        self._log_system("[bold white]ChachaAgent v0.1[/] — 项目: " + self._project.name)
        self._log_system(f"[dim]{init_msg}[/]")
       
        self._log_system(
            "[dim]Ctrl+N 新会话 | Ctrl+S 保存 | Ctrl+D 调试 | "
            "Ctrl+X 压缩 | /help 命令[/]"
        )
        self._update_status()

    # ====== 布局 ======

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            RichLog(id="chat-area", highlight=True, markup=True, wrap=True, max_lines=5000),
            id="chat-container",
        )
        yield Container(
            Input(id="main-input", placeholder="输入消息... (/help /status /memory /compact)"),
            id="input-container",
        )
        yield StatusBar(id="status-line")

    # ====== 输入 ======

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self._sending:
            return
        event.input.value = ""
        self._sending = True

        if text.startswith("/"):
            result = await self._handle_command(text)
            self._log_system(result)
            self._update_status()
            self._sending = False
            return

        self._log_user(text)
        await self._run_dialog(text)
        self._sending = False

    async def _run_dialog(self, text: str) -> None:
        """一轮对话：思考 → 流式输出 → 审计 → 记忆"""
        import time
        t0 = time.monotonic()

        chat = self.query_one("#chat-area", RichLog)
        chat.write(ChatMessage.prefix("assistant"))

        tokens = 0
        errors = []
        response_parts = []

        buffer = ""
        try:
            async for chunk in self._bridge.send_message(text):
                if chunk["type"] == "text":
                    response_parts.append(chunk["content"])
                    buffer += chunk["content"]
                    # 遇到换行才刷新
                    if "\n" in buffer:
                        parts = buffer.split("\n")
                        for line in parts[:-1]:
                            chat.write(line, animate=False)
                        buffer = parts[-1]
                elif chunk["type"] == "tool_call_start":
                    if buffer:
                        chat.write(buffer, animate=False)
                        buffer = ""
                    self._update_status(extra=f"⏳ {chunk['tool_name']}...")
                    chat.write(ToolCallBanner.render(chunk["tool_name"], "start"))
                elif chunk["type"] == "tool_call_end":
                    if buffer:
                        chat.write(buffer, animate=False)
                        buffer = ""
                    self._update_status()
                    chat.write(ToolCallBanner.render(
                        chunk["tool_name"], "end", chunk.get("preview", "")))
                elif chunk["type"] == "error":
                    if buffer:
                        chat.write(buffer, animate=False)
                        buffer = ""
                    errors.append(chunk["message"])
                    chat.write(f"[bold red]错误: {chunk['message']}[/]")
                elif chunk["type"] == "done":
                    tokens = chunk.get("tokens", 0)
                    # 只在最终回答时保存记忆
                    self._session.remember_final_answer(text, "".join(response_parts))
            # 刷新剩余缓冲区
            if buffer:
                chat.write(buffer, animate=False)

        except Exception as e:
            errors.append(str(e))
            chat.write(f"[bold red]异常: {e}[/]")

        # 审计：每轮后展示 + 自动记忆
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        response_text = "".join(response_parts)
        self._session.add_round(
            tokens=tokens, duration_ms=elapsed_ms, errors=errors,
            user_input=text, assistant_text=response_text,
        )

        audit = f"[dim]⏱ {elapsed_ms}ms  |  💬 {tokens} tokens  |  🔄 第{self._session.rounds}轮[/]"
        if errors:
            audit += f"  |  [red]⚠ {len(errors)}错误[/]"
        chat.write(audit)

        # 自动触发记忆检查
        self._session.record_dream_hint()
        if self._session.should_dream():
            import asyncio
            asyncio.create_task(self._bridge.run_dream())
            self._session.mark_dream_run()


        self._update_status()

    # ====== 命令 ======

    async def _handle_command(self, text: str) -> str:
        parts = text.lstrip("/").strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # 模型
        if cmd in ("model", "url", "key"):
            return await self._bridge.handle_command(text)

        # 会话
        if cmd == "new":
            self._session.new()
            await self._bridge.reset()
            return "🆕 新会话已开始"
        if cmd == "save":
            self._session.save()
            return "💾 会话已保存"
        if cmd == "load":
            await self._bridge.reset()
            return "✅ 对话历史已清除"

        # 记忆
        if cmd == "memory":
            days = self._session.list_memory_days()
            return "📁 记忆文件:\n" + "\n".join(f"  {d}.md" for d in days) if days else "暂无记忆文件"
        if cmd == "dream":
            return f"🎯 {await self._bridge.run_dream()}"

        # 压缩
        if cmd == "compact":
            return await self._session.compact()

        # 审计
        if cmd == "audit":
            return self._session.audit_report()
        if cmd == "trace":
            return self._session.trace_last()

        # 状态
        if cmd == "status":
            return self._session.status_report()

        # 子Agent
        if cmd == "agent":
            if not arg:
                return "用法: /agent <type> <任务>  类型: explore/plan/worker"
            return "📋 子Agent 待集成"

        # 帮助
        if cmd == "help":
            return (
                "命令:  /model /url /key  配置\n"
                "       /new /save /load   会话\n"
                "       /memory /dream     记忆\n"
                "       /compact           压缩\n"
                "       /audit /trace      审计\n"
                "       /status /help      信息\n"
                "快捷键: Ctrl+N 新会话  Ctrl+S 保存  Ctrl+D 调试  Ctrl+X 压缩  Ctrl+L 清屏"
            )

        return await self._bridge.handle_command(text)

    # ====== 快捷键 ======

    async def action_clear_screen(self) -> None:
        self.query_one("#chat-area", RichLog).clear()

    async def action_save(self) -> None:
        self._session.save()
        self._log_system("💾 会话已保存")

    async def action_new_session(self) -> None:
        self._session.new()
        await self._bridge.reset()
        self.query_one("#chat-area", RichLog).clear()
        self._log_system("🆕 新会话")
        self._update_status()

    async def action_toggle_debug(self) -> None:
        self._debug = not self._debug
        if self._debug:
            self._log_system("[cyan]🐛 调试面板[/]")
            self._log_system(f"[dim]{self._session.status_report()}[/]")
        self._log_system(f"调试: {'开' if self._debug else '关'}")

    async def action_compact(self) -> None:
        result = await self._session.compact()
        self._log_system(f"📦 {result}")

    # ====== 辅助 ======

    def _update_status(self, extra: str = "") -> None:
        bar = self.query_one("#status-line", StatusBar)
        bar.update(
            model=self._bridge.model if self._bridge else "",
            tokens=self._session.total_tokens,
            rounds=self._session.rounds,
            extra=extra,
        )

    def _log_user(self, text: str) -> None:
        self.query_one("#chat-area", RichLog).write(ChatMessage.render("user", text))

    def _log_system(self, text: str) -> None:
        self.query_one("#chat-area", RichLog).write(text)


def main():
    import sys
    project = sys.argv[1] if len(sys.argv) > 1 else "."
    app = ChachaApp(project_root=project)
    app.run()

if __name__ == "__main__":
    main()
