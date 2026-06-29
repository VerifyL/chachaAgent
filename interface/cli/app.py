"""
interface/cli/app.py
ChachaAgent CLI — prompt_toolkit + Rich。
Enter 发送，Ctrl+J 换行，支持多行粘贴/编辑。
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from core.cli_history import SessionHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich import box

from interface.cli.agent_bridge import AgentBridge
from core.project_init import ProjectInit
from core.models.stream_event import (
    TextEvent, ReasoningEvent, ToolCallStartEvent, ToolCallEndEvent,
    ToolExecStartEvent, ToolExecEndEvent, DoneEvent, ErrorEvent, CompactEvent,
)
from core.session_service import SessionService
from core.cli_theme import load_theme, write_default_theme

RICH_CONSOLE = Console()

# ====== 版本号 ======


def _get_version(project_root: Path) -> str:
    """从 pyproject.toml 或包元数据读取版本号"""
    # 优先读取 pyproject.toml（开发模式改了立刻生效）
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # Python < 3.11
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    # Fallback：从已安装包读取
    import importlib.metadata
    try:
        return importlib.metadata.version("chachaAgent")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


# ====== 多行续写标记 ======
CONTINUE_MARKER = "... "

# ====== Ctrl+C 中断标志 ======
_interrupted = False
_in_approval = False  # input() 阻塞时允许抛 KeyboardInterrupt


class ChachaCLI:
    """基于 prompt_toolkit + Rich 的 CLI"""

    def __init__(self, project_root: str = ".", debug: bool = False, verbose: bool = False):
        self._project = Path(project_root).resolve()
        self._bridge: Optional[AgentBridge] = None
        self._session: Optional[SessionService] = None
        self._sending = False
        self._debug = debug
        self._verbose = verbose
        self._version = _get_version(self._project)

        # 主题
        write_default_theme()
        self._t = load_theme()
        self._show_reasoning = True  # Ctrl+R 切换
        self._cli_history = None     # SessionHistory，initialize() 中创建

    # ====== 启动 ======

    async def initialize(self) -> str:
        self._ensure_default_constitution()
        self._session = SessionService(self._project)
        si = self._session.project_init

        # Session 级 CLI 历史
        sessions_base = self._session.memory_manager.session_dir.parent
        self._cli_history = SessionHistory(sessions_base, self._session.session_id)

        self._bridge = AgentBridge(
            system_prompt=si.build_system_prompt(),
            tools=si.build_tools(),
            project_root=self._project,
            force_telemetry=self._debug or self._verbose,
            verbose=self._verbose,
        )
        # Telemetry session 必须在 initialize（包含 start）之前设置
        if self._bridge._telemetry:
            self._bridge._telemetry.set_session_id(self._session.session_id)
        msg = await self._bridge.initialize()
        # 注入 project_root + project_id
        self._bridge.set_project_root(self._project)
        pid = self._session.memory_manager._project_id
        self._bridge._project_id = pid
        if self._bridge._dispatcher:
            self._bridge._dispatcher._project_id = pid
        if self._bridge._telemetry:
            self._session._telemetry = self._bridge._telemetry
        self._session.set_llm(self._bridge._invoker)
        self._bridge.set_checkpoint_dir(
            self._session.memory_manager.session_dir)
        # 构建 Orchestrator（为 Hook/Policy 准备）
        self._bridge.build_orchestrator(session_id=self._session.session_id, memory_manager=self._session.memory_manager)
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
        self._print_system(f"ChachaAgent v{self._version} — " + self._project.name)
        self._print_system(init_msg)
        if self._session.project_init._rules:
            self._print_system("[cyan]📜 CHACHA.md 已加载[/]")
        self._print_system("Ctrl+N 新会话  Ctrl+S 保存  Ctrl+F 调试  Ctrl+B 会话列表  Ctrl+X 压缩  Ctrl+L 清屏  Ctrl+R 推理  Ctrl+T 遥测  Ctrl+C 中断  Ctrl+D 退出  Ctrl+\\ 强退  Ctrl+J 换行  /help 命令")
        self._print_system("")

        # 输入循环
        session = PromptSession(
            history=self._cli_history,
            key_bindings=self._make_bindings(),
            multiline=False,  # Enter 发送，Ctrl+J 换行（key binding 实现）
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

    def _enable_tty_signals(self):
        """临时切到 cook 模式：信号 + 换行翻译 + 回显"""
        try:
            import termios
            attrs = termios.tcgetattr(sys.stdin.fileno())
            attrs[0] |= termios.ICRNL   # \r → \n 翻译
            attrs[3] |= termios.ISIG | termios.ICANON | termios.ECHO
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attrs)
        except Exception:
            pass

    async def _run_dialog(self, text: str) -> None:
        global _interrupted
        self._enable_tty_signals()

        t0 = time.monotonic()
        tokens = 0
        errors: list[str] = []
        response_parts: list[str] = []
        tool_trace: list[dict] = []
        in_tools = False
        in_reasoning = False
        usage: dict = {}

        self._print_user(text)
        RICH_CONSOLE.print(f"[{self._t['agent_header']}]🤖 Chacha[/]")

        try:
            async for chunk in self._bridge.send_message_orchestrated(
                text, session_id=self._session.session_id,
                project_id=self._bridge._project_id or "",
            ):
                # Ctrl+C 中断检查
                if _interrupted:
                    _interrupted = False
                    raise KeyboardInterrupt()

                if isinstance(chunk, ReasoningEvent):
                    if self._show_reasoning:
                        RICH_CONSOLE.print(f"[dim]{escape(chunk.content)}[/]", end="")
                        in_reasoning = True

                elif isinstance(chunk, TextEvent):
                    if in_reasoning:
                        RICH_CONSOLE.print()  # 思考 → 回答 换行
                        in_reasoning = False
                    response_parts.append(chunk.content)
                    if in_tools:
                        in_tools = False
                        RICH_CONSOLE.print(f"[{self._t['separator']}]" + "─" * 30 + "[/]")
                    RICH_CONSOLE.print(chunk.content, end="")

                elif isinstance(chunk, ToolCallStartEvent):
                    if in_reasoning:
                        RICH_CONSOLE.print()  # 思考 → 工具 换行
                        in_reasoning = False
                    if not in_tools:
                        in_tools = True
                        RICH_CONSOLE.print()
                        RICH_CONSOLE.print(
                            f"[{self._t['separator']}]" + "━" * 30 + "[/]"
                        )
                    tool_trace.append({
                        "tool": chunk.tool_name, "t0": time.monotonic(),
                    })

                elif isinstance(chunk, ToolExecStartEvent):
                    if tool_trace:
                        t = tool_trace[-1]
                        t["ms"] = int((time.monotonic() - t["t0"]) * 1000)
                    # 静默工具：不打印调用（memory, cache_read）
                    if chunk.tool_name not in ("memory", "cache_read"):
                        if chunk.args:
                            RICH_CONSOLE.print(f"  [{self._t['tool_thinking']}]🔧 {escape(chunk.tool_name)} — {escape(chunk.args)}[/]")
                        else:
                            RICH_CONSOLE.print(f"  [{self._t['tool_thinking']}]🔧 {escape(chunk.tool_name)}[/]")

                elif isinstance(chunk, ToolCallEndEvent):
                    if tool_trace:
                        t = tool_trace[-1]
                        t["ms"] = int((time.monotonic() - t["t0"]) * 1000)

                elif isinstance(chunk, ErrorEvent):
                    errors.append(chunk.message)
                    RICH_CONSOLE.print(f"[red]错误: {escape(chunk.message)}[/]")

                elif isinstance(chunk, DoneEvent):
                    tokens = chunk.tokens
                    usage = chunk.usage if chunk.usage else usage

                elif isinstance(chunk, CompactEvent):
                    self._print_system(f"🔄 自动压缩: {chunk.reason}")

        except KeyboardInterrupt:
            RICH_CONSOLE.print(f"\n[{self._t['separator']}]" + "─" * 30 + "[/]")
            RICH_CONSOLE.print(f"[yellow]⏹ 已中断[/]")
            # 移除未完成轮次的 user message，避免下一轮残留旧提问
            msgs = self._bridge._messages
            if msgs and msgs[-1].get("role") == "user":
                msgs.pop()
        except Exception as e:
            errors.append(str(e))
            RICH_CONSOLE.print(f"[red]异常: {escape(str(e))}[/]")

        # 审计
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await self._session.add_round(
            tokens=tokens, duration_ms=elapsed_ms, errors=errors,
            user_input=text, assistant_text="".join(response_parts),
            skip_memory=True,  # 由 Orchestrator.run_stream() 接管
        )

        # 上下文利用率 + 缓存命中
        ctx_pct = ""
        cache_str = ""
        if self._bridge._messages:
            from core.context.context_compressor import ContextCompressor
            est = ContextCompressor.estimate_tokens(self._bridge._messages)
            ctx_w = getattr(self._bridge, "_context_window", 1_048_576)
            ctx_pct = f" | 📦 {int(est/ctx_w*100)}%"
        cache_hit = usage.get("cache_hit", 0)
        if cache_hit:
            cache_str = f" | 📥 +{cache_hit}T"

        audit = f"⏱ {elapsed_ms}ms  |  💬 {tokens}T{ctx_pct}{cache_str}  |  🔄 第{self._session.rounds}轮"
        if errors:
            audit += f"  |  ⚠ {len(errors)}错"
        if self._debug and tool_trace:
            steps = "; ".join(f"{t['tool']}({t.get('ms','?')}ms)" for t in tool_trace)
            audit += f"  |  🐛 {steps}"
        RICH_CONSOLE.print()
        RICH_CONSOLE.print(f"[{self._t['audit']}]{escape(audit)}[/]")
        RICH_CONSOLE.print(f"[{self._t['separator']}]" + "─" * 40 + "[/]")

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
        if cmd == "save":
            self._bridge.save_checkpoint()
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
        if cmd in ("telemetry", "telem"):
            return self._cmd_telemetry(arg)
        if cmd == "logs":
            return self._cmd_logs(arg)
        if cmd in ("auditlog", "audit"):
            return self._cmd_auditlog(arg)
        if cmd == "trace":
            return self._cmd_trace(arg)
        if cmd == "cost":
            return self._cmd_cost(arg)
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
            self._cli_history.switch_session(sid)
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
            self._cli_history.switch_session(sid)
            await self._reload_bridge(old_sid)
        return result

    async def _del_by_index(self, idx_str: str, sessions: list) -> str:
        sid = self._session_by_index(idx_str, sessions)
        return await self._session.delete_session(sid)

    async def _reload_bridge(self, old_sid: str) -> None:
        # 保存旧 session
        self._bridge.save_checkpoint()
        # 重建工具 + Dispatcher
        self._bridge.set_tools_for_session(self._session.memory_manager)
        await self._bridge.rebuild()
        # 重置引擎并切换 checkpoint 目录
        await self._bridge.reset()
        self._bridge.set_checkpoint_dir(
            self._session.memory_manager.session_dir)
        # 更新 Orchestrator 的 memory_manager 引用
        self._bridge.build_orchestrator(
            session_id=self._session.session_id,
            memory_manager=self._session.memory_manager,
        )

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
                trigger_ratio=0.0,  # Ctrl+X 强制压缩，无视利用率阈值
                **getattr(self._bridge, "_compress_cfg", {}),
            )
            self._bridge._messages = msgs
            return f"📦 {n} → {len(msgs)} 条"
        except KeyboardInterrupt:
            RICH_CONSOLE.print(f"\n⏹ 已中断")
        except Exception as e:
            return f"压缩失败: {e}"

    # ====== 遥测 ======

    def _cmd_telemetry(self, arg: str) -> str:
        """显示遥测仪表盘 / 热切换开关"""
        if not self._bridge or not self._bridge._telemetry:
            return "⚠️ 遥测未初始化（使用 --debug 启动以启用遥测）"
        if arg in ("on", "enable"):
            return self._bridge.toggle_telemetry(True)
        if arg in ("off", "disable"):
            return self._bridge.toggle_telemetry(False)
        # 默认：完整仪表盘
        return self._bridge.get_telemetry_dashboard()

    def _cmd_logs(self, arg: str = "") -> str:
        """查看日志：/logs [N] [level] [filter]"""
        if not self._bridge:
            return "⚠️ 桥接层未初始化"
        # 解析参数：/logs 20 ERROR keyword
        parts = arg.split() if arg else []
        n = 10
        level = ""
        filter_text = ""
        i = 0
        while i < len(parts):
            p = parts[i]
            if p.isdigit():
                n = int(p)
            elif p.upper() in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                level = p
            else:
                # 剩余全部作为过滤关键词
                filter_text = " ".join(parts[i:])
                break
            i += 1
        return self._bridge.get_logs(n=n, level=level, filter_text=filter_text)

    def _cmd_auditlog(self, arg: str = "") -> str:
        """查看审计日志：/audit [N]"""
        if not self._bridge:
            return "⚠️ 桥接层未初始化"
        n = int(arg) if arg.isdigit() else 10
        return self._bridge.get_audit_logs(n=n)

    def _cmd_trace(self, arg: str = "") -> str:
        """查看 Span 追踪链"""
        if not self._bridge:
            return "⚠️ 桥接层未初始化"
        return self._bridge.get_trace()

    def _cmd_cost(self, arg: str = "") -> str:
        """查看成本汇总"""
        if not self._bridge:
            return "⚠️ 桥接层未初始化"
        return self._bridge.get_cost()

    # ====== 键绑定 ======

    def _make_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-n")
        def _(event):
            sid = self._session.new()
            self._cli_history.switch_session(sid)
            asyncio.create_task(self._bridge.reset())
            self._print_system(f"🆕 新会话: {sid}")

        @kb.add("c-s")
        def _(event):
            self._bridge.save_checkpoint()
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

        @kb.add("c-j")
        def _(event):
            """Ctrl+J 插入换行（多行输入）"""
            event.current_buffer.insert_text("\n")

        @kb.add("c-l")
        def _(event):
            RICH_CONSOLE.clear()

        @kb.add("c-r")
        def _(event):
            self._show_reasoning = not self._show_reasoning
            status = "开" if self._show_reasoning else "关"
            self._print_system(f"🧠 思考过程: {status}")

        @kb.add("c-t")
        def _(event):
            result = self._cmd_telemetry("")
            self._print_system(result)

        @kb.add("c-\\")
        def _(event):
            os._exit(0)

        return kb

    def _status_text(self) -> str:
        if not self._bridge:
            return f"ChachaAgent v{self._version}"
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
        RICH_CONSOLE.print(f"[{self._t['system']}]{escape(text)}[/]")

    def _print_tool(self, text: str, style: str = "status") -> None:
        if style == "thinking":
            RICH_CONSOLE.print(f"  [{self._t['tool_thinking']}]🔧 {escape(text)}[/]")
        else:
            RICH_CONSOLE.print(f"  [{self._t['tool_done']}]✅ {escape(text)}[/]")

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
                ("/save", "保存 checkpoint"),
            ],
            "记忆": [
                ("/memory", "查看记忆文件"),
                ("/dream", "运行 Session Dream"),
                ("/dream global", "运行 GlobalDream"),
            ],
            "调试": [
                ("/telemetry", "遥测仪表盘（P50/P99/成本）"),
                ("/telemetry on/off", "运行时开关遥测"),
                ("/logs [n] [level] [kw]", "查看/过滤调试日志"),
                ("/auditlog [n]", "查看审计日志"),
                ("/trace", "Span 追踪链"),
                ("/cost", "API 成本汇总"),
                ("/compact", "压缩上下文"),
                ("/debug", "切换调试模式"),
            ],
            "快捷键": [
                ("Ctrl+N", "新会话"),
                ("Ctrl+S", "保存 checkpoint"),
                ("Ctrl+F", "调试模式"),
                ("Ctrl+B", "会话列表"),
                ("Ctrl+X", "压缩上下文"),
                ("Ctrl+L", "清屏"),
                ("Ctrl+R", "切换推理显示"),
                ("Ctrl+T", "遥测状态"),
                ("Ctrl+C", "中断回答"),
                ("Ctrl+D", "退出程序"),
                ("Ctrl+\\", "强制退出"),
                ("Ctrl+J", "插入换行"),
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
    import argparse
    parser = argparse.ArgumentParser(description="ChachaAgent CLI")
    parser.add_argument("project", nargs="?", default=".", help="项目路径")
    parser.add_argument("-d", "--debug", action="store_true", help="启用遥测（结构化日志+指标+审计）")
    parser.add_argument("-v", "--verbose", action="store_true", help="启用遥测并设置 DEBUG 日志级别")
    args = parser.parse_args()
    cli = ChachaCLI(args.project, debug=args.debug, verbose=args.verbose)

    # Ctrl+C → 中断标志（C 级 signal，绕过 asyncio 屏蔽）
    # Ctrl+\ → 立即强制退出（包括审批阻塞时）
    def _on_sigint(_sig, _frame):
        global _interrupted, _in_approval
        _interrupted = True
        if _in_approval:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGQUIT, lambda *_: os._exit(0))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(cli.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


if __name__ == "__main__":
    main()
