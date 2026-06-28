"""
interface/cli/agent_bridge.py
AgentBridge — CLI ↔ 核心的薄桥接层。消息历史 + 压缩托管给 ChatEngine。
"""

import logging
import os
import select
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.chat_engine import ChatEngine

logger = logging.getLogger(__name__)


def _interruptible_input(prompt: str) -> str:
    """原生 input() + _in_approval 标志（signal handler 在审批时抛 KeyboardInterrupt）。"""
    import interface.cli.app as _app
    _app._in_approval = True
    try:
        return input(prompt)
    finally:
        _app._in_approval = False


class AgentBridge:
    """CLI 桥接层（薄）"""

    def __init__(
        self,
        system_prompt: str = "",
        tools: Optional[List] = None,
        project_root: Optional[Path] = None,
        force_telemetry: bool = False,
        verbose: bool = False,
    ):
        self._root = project_root or Path.cwd()
        self._system_prompt = system_prompt
        self._custom_tools = tools or []

        # Orchestrator（内嵌 ChatEngine，app.py 不直接访问 engine）
        from core.orchestrator import Orchestrator
        self._orchestrator: Optional[Orchestrator] = None

        # 配置：chachaConfig.toml → 环境变量 → 默认值
        self._telemetry_cfg = None
        default_provider = None
        try:
            from core.config_manager import get_config_manager
            cfg = get_config_manager().load()
            default_provider = cfg.model.providers.get("default")
            self._telemetry_cfg = cfg.telemetry
        except Exception:
            pass

        # 可观测性（开关控制，session_id 后续由 app.py 注入）
        from core.telemetry import Telemetry
        if self._telemetry_cfg:
            if force_telemetry or verbose:
                self._telemetry_cfg = self._telemetry_cfg.model_copy(update={
                    "enabled": True,
                    "log_level": "DEBUG" if verbose else self._telemetry_cfg.log_level,
                })
        self._telemetry = Telemetry(self._telemetry_cfg) if self._telemetry_cfg else None

        self._project_id = getattr(default_provider, "project_id", "") if default_provider else ""
        try:
            from core.config_manager import get_config_manager
            full_cfg = get_config_manager().load()
            self._project_id = full_cfg.project_id or ""
        except Exception:
            pass

        self._api_key = (os.environ.get("DEEPSEEK_API_KEY") or
                         os.environ.get("OPENAI_API_KEY") or
                         (default_provider.api_key.get_secret_value() if default_provider and default_provider.api_key else ""))
        self._base_url = (os.environ.get("DEEPSEEK_BASE_URL") or
                          os.environ.get("OPENAI_BASE_URL") or
                          (default_provider.base_url if default_provider else "https://api.deepseek.com"))
        self._model = (os.environ.get("DEEPSEEK_MODEL") or
                       os.environ.get("OPENAI_MODEL") or
                       (default_provider.default_model if default_provider else "deepseek-v4-pro"))

        # 上下文窗口：配置 → 模型名推断 → 默认 1M
        context_window = ChatEngine.infer_context_window(self._model)
        if default_provider and default_provider.context_window != 1_048_576:
            context_window = default_provider.context_window

        compress_cfg = self._load_compress_cfg()

        # ContextManager（注入 system prompt + telemetry）
        from core.context_manager import ContextManager
        self._context_manager = ContextManager(telemetry=self._telemetry)
        self._context_manager.set_system_prompt(system_prompt)

        self._engine = ChatEngine(
            system_prompt=system_prompt,
            tools=tools,
            context_window=context_window,
            compress_cfg=compress_cfg,
            context_manager=self._context_manager,
        )

        from core.context.dream import DreamPipeline
        self._dream_pipeline = DreamPipeline(llm_invoker=None)
        self._orchestrator = Orchestrator(
            context_manager=self._context_manager,
            dream_pipeline=self._dream_pipeline,
        )
        self._orchestrator.set_engine(self._engine)

        # Hook 系统（可插拔模块：Git 感知等）
        from core.hook_orchestrator import HookOrchestrator
        from core.models.hook import HookPoint
        from core.git_context import GitContextHook
        self._hooks = HookOrchestrator(telemetry=self._telemetry)
        self._hooks.register(
            "git-context",
            HookPoint.PRE_CONTEXT_ASSEMBLY,
            GitContextHook(project_root=self._root),
            priority=10,
        )
        self._orchestrator._hooks = self._hooks

        self._dispatcher = None
        self._invoker = None
        self._initialized = False

    # ====== 属性 ======

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_key(self) -> str:
        return self._api_key[:10] + "..." if self._api_key else "(未设置)"

    @property
    def _messages(self) -> list:
        return self._engine._messages

    @_messages.setter
    def _messages(self, value: list) -> None:
        self._engine._messages = value

    @property
    def _context_window(self) -> int:
        return self._engine._context_window

    @property
    def _compress_cfg(self) -> dict:
        return self._engine._compress_cfg

    # ====== 初始化 ======

    async def initialize(self) -> str:
        """初始化 LLM + Dispatcher + 策略 + 重试 + 治理"""
        from core.llm_invoker import LLMInvoker
        from core.llm_clients.openai_client import OpenAIClient
        from core.llm_clients.retry_handler import RetryHandler
        from core.output_governor import OutputGovernor
        from core.tool_executor import ToolExecutor
        from core.dispatcher import Dispatcher

        # 重试处理器 + 输出治理器
        retry = RetryHandler(max_retries=3)
        governor = OutputGovernor()

        # 1. LLM
        client = OpenAIClient(
            api_key=self._api_key,
            model=self._model,
            base_url=self._base_url,
        )
        self._invoker = LLMInvoker(
            model_client=client,
            retry_handler=retry,
            output_governor=governor,
        )
        self._engine.set_llm(self._invoker)

        # 启动可观测性
        if self._telemetry and self._telemetry.enabled:
            self._telemetry.start()

        await self.rebuild()

        self._initialized = True
        return f"API: {self._model} | 上下文: {self._context_window // 1000}K"

    def set_tools_for_session(self, memory_manager) -> None:
        """根据 session 的 MemoryManager 重建工具（统一走 registry）。"""
        from capabilities.registry import build_tools
        self._session_memory = memory_manager
        self._custom_tools = build_tools(root=self._root, memory_manager=memory_manager)

    async def rebuild(self) -> None:
        """重建 Dispatcher + ToolExecutor"""
        from core.tool_executor import ToolExecutor
        from core.dispatcher import Dispatcher
        from core.policy_engine import PolicyEngine
        policy = PolicyEngine()

        async def _cli_approval(req) -> bool:
            """CLI 交互式审批回调"""
            print(f"\n⚠️  工具 '{req.tool_name}' 需要审批")
            print(f"   风险等级: {req.risk_level} (分数: {req.risk_score:.0f})")
            # 展示关键参数（大值显示长度+摘要，避免 diff/new_string 被硬截断）
            arg_parts = []
            for k, v in req.arguments.items():
                s = str(v)
                if len(s) > 500:
                    s = f"{s[:200]}... [{len(s)} chars] ...{s[-100:]}"
                arg_parts.append(f"{k}={s}")
            args_str = " ".join(arg_parts)
            if args_str:
                print(f"   参数: {args_str}")
            if req.diff:
                print(f"\n--- 文件变更 ({req.arguments.get('path', '?')}) ---")
                print(req.diff)
                print("--- diff 结束 ---")
            if req.tool_name in PolicyEngine.SYSTEM_TOOLS:
                print(f"   ⛔ '{req.tool_name}' 是系统级工具，可能执行任意命令！")
                default = "N"
            else:
                default = "y"
            default_hint = "[Y/n]" if default == "y" else "[y/N]"
            try:
                answer = _interruptible_input(f"   是否执行？{default_hint}: ").strip().lower()
            except KeyboardInterrupt:
                return False
            except EOFError:
                return False
            if not answer:
                return default == "y"
            return answer in ("y", "yes")

        self._executor = ToolExecutor(
            tools=self._custom_tools,
            policy_engine=policy,
            hook_orchestrator=self._hooks,
            telemetry=self._telemetry,
            approval_handler=_cli_approval,
        )

        # 创建 SubAgentSpawner（供 TaskTool 使用）
        from core.subagent.spawner import SubAgentSpawner
        spawner = SubAgentSpawner(
            llm_invoker=self._invoker,
            parent_tool_executor=self._executor,
            hook_orchestrator=self._hooks,
            project_root=str(self._root) if self._root else None,
            telemetry=self._telemetry,
        )

        self._dispatcher = Dispatcher(
            llm_invoker=self._invoker,
            tool_executor=self._executor,
            telemetry=self._telemetry,
            project_id=self._project_id,
            context_window=self._context_window,
        )
        self._engine.set_dispatcher(self._dispatcher)

        # 重新注入工具运行时依赖
        # SubAgentSpawner 依赖 ToolExecutor（需工具列表已就绪），而 ToolExecutor
        # 构造时也需要工具列表。这个循环依赖决定了只能两阶段：先 build_tools()
        # 再事后注入 spawner。registry.build_tools() 的 subagent_spawner= 参数
        # 预留为未来解耦通道（如引入 factory 模式打破循环）。
        for tool in self._custom_tools:
            if hasattr(tool, 'configure'):
                tool.configure(
                    llm_invoker=self._invoker,
                    parent_tool_executor=self._executor,
                    project_root=self._root,
                    telemetry=self._telemetry,
                    policy_engine=policy,
                    subagent_spawner=spawner,
                )

        # ContextManager — 注入记忆和技能
        from core.context.memory_manager import MemoryManager
        import json
        try:
            mgr = getattr(self, '_session_memory', None) or MemoryManager(project_root=self._root)
            perm = mgr.read_permanent_memory()
            if perm:
                self._context_manager.set_permanent_memory(perm)
            idx = mgr.read()
            if idx:
                self._context_manager.set_memory_index(idx)
            recent = mgr.read_recent_days(3)
            if recent:
                self._context_manager.set_session_memory(recent)
            user_path = Path.home() / ".chacha" / "USER_MEMORY.md"
            if user_path.exists():
                self._context_manager.set_global_permanent_memory(
                    user_path.read_text(encoding="utf-8"))
            schemas = self._executor.get_schemas()
            if schemas:
                skills_text = "\n".join(
                    json.dumps(s, ensure_ascii=False) for s in schemas)
                self._context_manager.set_skills(skills_text)
        except Exception:
            pass

    # ====== 发送消息（委托 ChatEngine） ======

    def build_orchestrator(self, session_id: str = "", memory_manager=None) -> None:
        """注入运行时依赖（LLM/Dispatcher/Telemetry/MemoryManager/DreamPipeline）到 Orchestrator。"""
        self._orchestrator._llm = self._invoker
        self._orchestrator._tools = self._executor
        self._orchestrator._dispatcher = self._dispatcher
        self._orchestrator._telemetry = self._telemetry
        self._orchestrator._memory = memory_manager
        if self._orchestrator._dream:
            self._orchestrator._dream._llm = self._invoker

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        """委托 Orchestrator（统一编排路径）。"""
        async for chunk in self.send_message_orchestrated(user_input):
            yield chunk

    async def send_message_orchestrated(
        self, user_input: str, session_id: str = "", project_id: str = "", memory_manager=None
    ) -> AsyncIterator[Dict[str, Any]]:
        """通过 Orchestrator 调度（预留 Hook/Policy 通道）。"""
        if not self._orchestrator:
            self.build_orchestrator(session_id=session_id, memory_manager=memory_manager)
        async for chunk in self._orchestrator.run_stream(
            user_input, session_id=session_id, project_id=project_id,
        ):
            yield chunk

    async def get_result(self) -> str:
        return ""

    def set_project_root(self, root) -> None:
        self._engine._project_root = root

    def set_checkpoint_dir(self, path) -> None:
        self._engine.set_checkpoint_dir(path)

    def save_checkpoint(self) -> None:
        self._engine.save_checkpoint()

    async def reset(self) -> None:
        self._engine.reset()

    # ====== 命令 ======

    def toggle_telemetry(self, enable: bool) -> str:
        """运行时热切换遥测开关。

        子系统持有 Telemetry 对象引用，运行时检查 enabled/logger/agent，
        因此翻转后立即生效，无需重建 Dispatcher/ToolExecutor。
        """
        if not self._telemetry:
            return "⚠️ 遥测未初始化"
        self._telemetry.toggle(enable)
        status = "🟢 已启用" if enable else "⚫ 已禁用"
        return f"📊 遥测: {status}"

    async def handle_command(self, text: str) -> str:
        parts = text.lstrip("/").strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "model":
            return self._cmd_model(arg)
        if cmd == "url":
            return self._cmd_url(arg)
        if cmd == "key":
            return self._cmd_key(arg)
        if cmd == "memory":
            return await self._cmd_memory(arg)

        return f"未知命令: {cmd}"

    def _cmd_model(self, arg: str) -> str:
        if not arg:
            return f"当前模型: {self._model}"
        self._model = arg
        # 更新上下文窗口推断
        self._engine._context_window = ChatEngine.infer_context_window(arg)
        self._rebuild_llm_client()
        return f"模型切换为: {arg} (窗口 {self._engine._context_window // 1000}K)"

    def _cmd_url(self, arg: str) -> str:
        if not arg:
            return f"当前 API URL: {self._base_url}"
        self._base_url = arg
        self._rebuild_llm_client()
        return f"API URL 切换为: {arg}"

    def _cmd_key(self, arg: str) -> str:
        if not arg:
            return f"当前 Key: {self.api_key}"
        self._api_key = arg
        self._rebuild_llm_client()
        return "Key 已更新"

    def _rebuild_llm_client(self) -> None:
        """当 model/url/key 变更后，重建 OpenAIClient 并注入到 LLMInvoker。

        LLMInvoker 被 Engine 和 Dispatcher 以引用方式持有，
        因此更新 self._invoker._client 即可全局生效，无需重建 Dispatcher。
        """
        if not self._invoker:
            return
        from core.llm_clients.openai_client import OpenAIClient
        self._invoker._client = OpenAIClient(
            api_key=self._api_key,
            model=self._model,
            base_url=self._base_url,
        )

    async def _cmd_memory(self, arg: str) -> str:
        try:
            from core.context.memory_manager import MemoryManager
            mgr = getattr(self, '_session_memory', None) or MemoryManager(project_root=self._root)
            permanent = mgr.read_permanent_memory()
            index = mgr.read()
            days = mgr.list_days(limit=7)
            lines = ["--- 记忆状态 ---"]
            lines.append(f"永久记忆: {'已加载' if permanent else '无'} ({len(permanent)} 字符)")
            lines.append(f"索引记忆: {'已加载' if index else '无'} ({len(index)} 字符)")
            lines.append(f"最近记忆天数: {len(days)}")
            if permanent:
                lines.append(f"\n永久记忆预览:\n{permanent[:500]}...")
            return "\n".join(lines)
        except Exception as e:
            return f"读取记忆失败: {e}"

    # ====== 遥测查询（供 CLI 仪表盘使用） ======

    def get_telemetry_dashboard(self) -> str:
        """完整遥测仪表盘：指标摘要 + 日志概览 + 成本。"""
        if not self._telemetry:
            return "⚠️ 遥测未初始化（使用 --debug 启动以启用遥测）"
        if not self._telemetry.enabled:
            return "⚫ 遥测未启用（使用 /telemetry on 或 --debug 启动）"

        snap = self._telemetry.snapshot()
        m = snap.get("metrics", {})
        counters = m.get("counters", {})
        histograms = m.get("histograms", {})
        gauges = m.get("gauges", {})
        uptime = snap.get("uptime_seconds", 0)

        lines = [
            f"📊 遥测仪表盘",
            f"   状态: 🟢 已启用 | 日志级别: {snap['log_level']} | 运行: {int(uptime)}s",
            f"   日志目录: {snap['log_dir']} | 审计: {'开' if snap.get('audit_enabled') else '关'}",
            "",
            "   ═══ 调用统计 ═══",
        ]

        # LLM 调用
        llm_total = counters.get("chacha_llm_calls_total", 0)
        llm_in = counters.get("chacha_llm_input_tokens_total", 0)
        llm_out = counters.get("chacha_llm_output_tokens_total", 0)
        llm_lat = histograms.get("chacha_llm_latency_ms", {})
        lines.append(f"   LLM 调用: {llm_total} 次 | 输入 {llm_in}T | 输出 {llm_out}T")
        if llm_lat:
            lines.append(f"   延迟: avg={llm_lat['avg']:.0f}ms p50={llm_lat['p50']:.0f}ms p99={llm_lat['p99']:.0f}ms")

        # 工具调用
        tool_total = counters.get("chacha_tool_calls_total", 0)
        tool_lat = histograms.get("chacha_tool_duration_ms", {})
        lines.append(f"   工具调用: {tool_total} 次")
        if tool_lat:
            lines.append(f"   耗时: avg={tool_lat['avg']:.0f}ms p50={tool_lat['p50']:.0f}ms p99={tool_lat['p99']:.0f}ms")

        # 会话
        sess = counters.get("chacha_sessions_total", 0)
        ctx_util = gauges.get("chacha_context_utilization", 0)
        compressions = counters.get("chacha_context_compressions_total", 0)
        lines.append(f"   会话: {sess} | 上下文利用率: {ctx_util:.1%} | 压缩: {compressions} 次")

        # 成本
        cost = self._telemetry.cost_summary()
        lines.append(f"")
        lines.append(f"   ═══ 成本 ═══")
        lines.append(f"   累计: ${cost['total_cost_usd']:.4f}")
        for model, c in cost.get("by_model", {}).items():
            lines.append(f"     {model}: ${c:.4f}")

        # 日志文件大小
        import os
        log_dir = Path(snap["log_dir"])
        for fname in ["debug.jsonl", "audit.jsonl"]:
            fp = log_dir / fname
            if fp.exists():
                size = fp.stat().st_size
                lines.append(f"   {fname}: {self._fmt_size(size)}")
            else:
                lines.append(f"   {fname}: (空)")

        return "\n".join(lines)

    def get_logs(self, n: int = 10, level: str = "", filter_text: str = "") -> str:
        """读取并格式化最近 N 条调试日志。"""
        if not self._telemetry or not self._telemetry.enabled:
            return "⚠️ 遥测未启用"
        level_arg = level.upper() if level else None
        filter_arg = filter_text if filter_text else None
        entries = self._telemetry.read_logs("debug", n=n, level=level_arg, filter_text=filter_arg)
        if not entries:
            return "📭 无匹配日志"
        lines = [f"📋 调试日志（最近 {len(entries)} 条）:"]
        for e in entries:
            ts = e.get("ts", "")[:19].replace("T", " ")
            lvl = e.get("level", "?")
            msg = e.get("msg", "")
            extra = {k: v for k, v in e.items() if k not in ("ts", "level", "msg", "session")}
            extra_str = " " + " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
            lines.append(f"  {ts} [{lvl}] {msg}{extra_str}")
        return "\n".join(lines)

    def get_audit_logs(self, n: int = 10) -> str:
        """读取最近 N 条审计日志。"""
        if not self._telemetry or not self._telemetry.enabled:
            return "⚠️ 遥测未启用"
        entries = self._telemetry.read_logs("audit", n=n)
        if not entries:
            return "📭 审计日志为空"
        lines = [f"🔒 审计日志（最近 {len(entries)} 条）:"]
        for e in entries:
            ts = e.get("ts", "")[:19].replace("T", " ")
            event_type = e.get("event_type", e.get("type", "?"))
            summary = str(e)[:200]
            lines.append(f"  {ts} [{event_type}] {summary}")
        return "\n".join(lines)

    def get_trace(self) -> str:
        """列出最近的 Span 追踪链。"""
        if not self._telemetry or not self._telemetry.enabled:
            return "⚠️ 遥测未启用"
        spans = self._telemetry.list_spans()
        if not spans:
            return "📭 无 Span 记录"
        lines = [f"🔗 Span 追踪链（{len(spans)} 条，按耗时降序）:"]
        for s in spans:
            err = f" ❌{s['error']}" if s['error'] else ""
            tags = " ".join(f"{k}={v}" for k, v in s.get("tags", {}).items())
            lines.append(
                f"  {s['operation']:<20} {s['duration_ms']:>8.1f}ms  "
                f"span={s['span_id']}  trace={s['trace_id']}  parent={s['parent']}{err}"
            )
            if tags:
                lines.append(f"  {'':20} {'':>8}   {tags}")
        return "\n".join(lines)

    def get_cost(self) -> str:
        """成本汇总。"""
        if not self._telemetry or not self._telemetry.enabled:
            return "⚠️ 遥测未启用"
        cost = self._telemetry.cost_summary()
        lines = [
            f"💰 成本汇总",
            f"   累计: ${cost['total_cost_usd']:.6f}",
        ]
        for model, c in cost.get("by_model", {}).items():
            lines.append(f"   {model}: ${c:.6f}")
        if not cost.get("by_model"):
            lines.append("   (暂无调用)")
        return "\n".join(lines)

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"

    def _load_compress_cfg(self) -> dict:
        try:
            from core.config_manager import get_config_manager
            cfg = get_config_manager().load()
            ctx = cfg.context
            return {
                "trigger_ratio": ctx.compression_trigger_ratio,
                "warn_ratio": ctx.warn_ratio,
                "frozen_keep": ctx.frozen_keep_latest,
                "trim_head": ctx.trim_keep_head,
                "trim_tail": ctx.trim_keep_tail,
                "summary_head": ctx.summarize_keep_head,
                "summary_tail": ctx.summarize_keep_tail,
            }
        except Exception:
            return {}
