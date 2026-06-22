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

        # 配置：chachaConfig.toml → 环境变量 → 默认值
        default_provider = None
        try:
            from core.config_manager import get_config_manager
            cfg = get_config_manager().load()
            default_provider = cfg.model.providers.get("default")
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
        self._engine = ChatEngine(
            system_prompt=system_prompt,
            tools=tools,
            context_window=context_window,
            compress_cfg=compress_cfg,
        )

        self._dispatcher = None
        self._invoker = None
        self._context_manager = None
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
        """初始化 LLM + Dispatcher"""
        from core.llm_invoker import LLMInvoker
        from core.llm_clients.openai_client import OpenAIClient
        from core.tool_executor import ToolExecutor
        from core.dispatcher import Dispatcher

        # 1. LLM
        client = OpenAIClient(
            api_key=self._api_key,
            model=self._model,
            base_url=self._base_url,
        )
        self._invoker = LLMInvoker(model_client=client)
        self._engine.set_llm(self._invoker)

        # 2. Dispatcher
        executor = ToolExecutor(tools=self._custom_tools)
        self._dispatcher = Dispatcher(
            llm_invoker=self._invoker,
            tool_executor=executor,
        )
        self._engine.set_dispatcher(self._dispatcher)

        # 3. ContextManager
        from core.context_manager import ContextManager
        cm = ContextManager()
        cm.set_system_prompt(self._system_prompt)
        self._context_manager = cm

        self._initialized = True
        return f"API: {self._model} | 上下文: {self._context_window // 1000}K"

    async def rebuild(self) -> None:
        """重建 Dispatcher + ToolExecutor"""
        from core.tool_executor import ToolExecutor
        from core.dispatcher import Dispatcher
        executor = ToolExecutor(tools=self._custom_tools)
        self._dispatcher = Dispatcher(
            llm_invoker=self._invoker,
            tool_executor=executor,
        )
        self._engine.set_dispatcher(self._dispatcher)

    # ====== 发送消息（委托 ChatEngine） ======

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        async for chunk in self._engine.send_message(user_input):
            yield chunk

    async def get_result(self) -> str:
        return ""

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
