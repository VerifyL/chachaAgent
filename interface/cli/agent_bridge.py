"""
interface/cli/agent_bridge.py
AgentBridge — CLI ↔ 核心的薄桥接层。消息历史 + 压缩托管给 ChatEngine。
"""

import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from core.chat_engine import ChatEngine

logger = logging.getLogger(__name__)


class AgentBridge:
    """CLI 桥接层（薄）"""

    def __init__(
        self,
        system_prompt: str = "",
        tools: Optional[List] = None,
        project_root: Optional[Path] = None,
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

        self._orchestrator = Orchestrator(
            context_manager=self._context_manager,
        )
        self._orchestrator.set_engine(self._engine)

        # Hook 系统（可插拔模块：Git 感知等）
        from core.hook_orchestrator import HookOrchestrator
        from core.models.hook import HookPoint
        from capabilities.builtins.git_context import GitContextHook
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

        # 2. Dispatcher
        self._executor = ToolExecutor(
            tools=self._custom_tools,
            hook_orchestrator=self._hooks,
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

        # 2.5 注入工具运行时依赖（如 SubAgentTool）
        for tool in self._custom_tools:
            if hasattr(tool, 'configure'):
                tool.configure(
                    llm_invoker=self._invoker,
                    parent_tool_executor=self._executor,
                    project_root=self._root,
                    telemetry=self._telemetry,
                )

        # 3. ContextManager — 注入记忆和技能
        from core.context_manager import ContextManager
        from core.context.memory_manager import MemoryManager
        import json
        try:
            mgr = MemoryManager(project_root=self._root)
            perm = mgr.read_permanent_memory()
            if perm:
                self._context_manager.set_permanent_memory(perm)
            idx = mgr.read()
            if idx:
                self._context_manager.set_memory_index(idx)
            # 读取最近 3 天会话记忆
            recent = mgr.read_recent_days(3)
            if recent:
                self._context_manager.set_session_memory(recent)
            user_path = Path.home() / ".chacha" / "USER_MEMORY.md"
            if user_path.exists():
                self._context_manager.set_global_permanent_memory(
                    user_path.read_text(encoding="utf-8"))
            # 工具 schema → skill 文本
            schemas = self._executor.get_schemas()
            if schemas:
                skills_text = "\n".join(
                    json.dumps(s, ensure_ascii=False) for s in schemas)
                self._context_manager.set_skills(skills_text)
        except Exception:
            pass

        self._initialized = True
        return f"API: {self._model} | 上下文: {self._context_window // 1000}K"

    def set_tools_for_session(self, memory_manager) -> None:
        """根据 session 的 MemoryManager 重建工具（统一走 registry）。"""
        from capabilities.registry import build_tools
        self._custom_tools = build_tools(root=self._root, memory_manager=memory_manager)

    async def rebuild(self) -> None:
        """重建 Dispatcher + ToolExecutor"""
        from core.tool_executor import ToolExecutor
        from core.dispatcher import Dispatcher
        self._executor = ToolExecutor(tools=self._custom_tools, hook_orchestrator=self._hooks, telemetry=self._telemetry)
        self._dispatcher = Dispatcher(
            llm_invoker=self._invoker,
            tool_executor=self._executor,
            telemetry=self._telemetry,
            project_id=self._project_id,
            context_window=self._context_window,
        )
        self._engine.set_dispatcher(self._dispatcher)

        # 重新注入工具运行时依赖
        for tool in self._custom_tools:
            if hasattr(tool, 'configure'):
                tool.configure(
                    llm_invoker=self._invoker,
                    parent_tool_executor=self._executor,
                    project_root=self._root,
                    telemetry=self._telemetry,
                )

    # ====== 发送消息（委托 ChatEngine） ======

    def build_orchestrator(self, session_id: str = "", memory_manager=None) -> None:
        """注入运行时依赖（LLM/Dispatcher/Telemetry/MemoryManager）到 Orchestrator。"""
        self._orchestrator._llm = self._invoker
        self._orchestrator._tools = self._executor
        self._orchestrator._dispatcher = self._dispatcher
        self._orchestrator._telemetry = self._telemetry
        self._orchestrator._memory = memory_manager

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        async for chunk in self._engine.send_message(user_input):
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
        return f"模型切换为: {arg} (窗口 {self._engine._context_window // 1000}K)"

    def _cmd_url(self, arg: str) -> str:
        if not arg:
            return f"当前 API URL: {self._base_url}"
        self._base_url = arg
        return f"API URL 切换为: {arg}"

    def _cmd_key(self, arg: str) -> str:
        if not arg:
            return f"当前 Key: {self.api_key}"
        self._api_key = arg
        return "Key 已更新"

    async def _cmd_memory(self, arg: str) -> str:
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(project_root=self._root)
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
