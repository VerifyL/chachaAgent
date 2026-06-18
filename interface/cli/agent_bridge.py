"""
interface/cli/agent_bridge.py
AgentBridge — CLI ↔ ChachaAgent 核心模块桥接。

系统提示词和工具通过外部传入，不硬编码。
支持流式输出（逐字）。
"""

import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional


class AgentBridge:
    """CLI ↔ 核心模块桥接"""

    def __init__(
        self,
        system_prompt: str = "",
        tools: Optional[List] = None,
        project_root: Optional[Path] = None,
    ):
        self._root = project_root or Path.cwd()
        self._system_prompt = system_prompt
        self._custom_tools = tools or []

        # 配置（环境变量优先）
        self._api_key = os.environ.get("DEEPSEEK_API_KEY",
                                       os.environ.get("OPENAI_API_KEY", ""))
        self._base_url = os.environ.get("DEEPSEEK_BASE_URL",
                                        os.environ.get("OPENAI_BASE_URL",
                                                       "https://api.deepseek.com"))
        self._model = os.environ.get("DEEPSEEK_MODEL",
                                     os.environ.get("OPENAI_MODEL", "deepseek-chat"))

        self._dispatcher = None
        self._invoker = None
        self._messages: list[dict] = []
        self._initialized = False

    # ====== 属性 ======

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_key(self) -> str:
        return self._api_key[:10] + "..." if self._api_key else "(未设置)"

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def messages(self) -> list:
        return self._messages

    # ====== 初始化 ======

    async def initialize(self) -> str:
        if not self._api_key:
            return "⚠️  未设置 API Key。使用 /key sk-xxx 设置。"
        await self._rebuild()
        self._initialized = True
        return f"✅ 就绪 — 模型: {self._model} | 项目: {self._root.name}"

    async def _rebuild(self) -> None:
        from core.llm_invoker import LLMInvoker
        from core.llm_clients.openai_client import OpenAIClient
        from core.dispatcher import Dispatcher
        from core.tool_executor import ToolExecutor

        client = OpenAIClient(
            api_key=self._api_key, model=self._model,
            base_url=self._base_url, max_tokens=2000,
        )
        self._invoker = LLMInvoker(model_client=client)

        tools = ToolExecutor(tools=self._custom_tools)
        self._dispatcher = Dispatcher(self._invoker, tools)

        # 系统提示（外部传入）
        self._messages = [{"role": "system", "content": self._system_prompt}]

    # ====== 命令 ======

    async def handle_command(self, cmd: str) -> str:
        parts = cmd.lstrip("/").strip().split(None, 1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if action == "help":
            return self._help_text()
        if action == "model":
            self._model = arg or self._model
            if self._initialized:
                await self._rebuild()
            return f"✅ 模型切换为: {self._model}"
        if action == "url":
            self._base_url = arg
            if self._initialized:
                await self._rebuild()
            return f"✅ API URL 切换为: {self._base_url}"
        if action == "key":
            self._api_key = arg
            if self._initialized:
                await self._rebuild()
            return f"✅ API Key 已设置: {self.api_key}"
        if action == "status":
            return (
                f"模型: {self._model}\nURL: {self._base_url}\n"
                f"Key: {self.api_key}\n项目: {self._root}\n"
                f"消息数: {len(self._messages)}"
            )
        if action == "clear":
            self._messages = self._messages[:1]
            return "✅ 对话历史已清除"
        return f"未知命令: /{action}。使用 /help 查看帮助。"

    def _help_text(self) -> str:
        return (
            "/model <name>  切换模型\n/url <url>     切换 API URL\n"
            "/key <sk-...>  设置 API Key\n/status        显示配置\n"
            "/clear         清除历史\n/help          帮助"
        )

    # ====== 对话（流式） ======

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        """流式发送消息。yields: type=text/tool_call_start/tool_call_end/error/done"""
        import time

        if not self._dispatcher:
            yield {"type": "error", "message": "未初始化。请先设置 API Key。"}
            return

        self._messages.append({"role": "user", "content": user_input})
        t0 = time.monotonic()

        try:
            async for chunk in self._dispatcher.dispatch_stream(
                messages=self._messages,
                session_id=f"cli-{int(t0)}",
                max_rounds=10,
            ):
                yield chunk

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    async def get_result(self) -> str:
        return ""

    async def reset(self) -> None:
        """重置对话历史（保留系统提示）"""
        self._messages = self._messages[:1]

    async def run_dream(self) -> str:
        """运行 DreamPipeline 记忆整合"""
        if not self._initialized:
            return "未初始化"
        try:
            from core.context.memory_manager import MemoryManager
            from core.context.dream import DreamPipeline
            mgr = MemoryManager(project_id=self._root.name)
            pipeline = DreamPipeline(self._invoker)
            result = await pipeline.run(mgr)
            return f"完成: {len(result)} 字符" if result else "无需整合"
        except Exception as e:
            return f"失败: {e}"
