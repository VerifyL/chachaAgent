"""
interface/cli/agent_bridge.py
AgentBridge — CLI ↔ ChachaAgent 核心模块桥接（v2.0）。

v2.0 新增:
  - 加载 CHACHA.md / CHACHA_MEMORY.md / MEMORY.md 并注入 ContextManager
  - /chacha 命令：显示/编辑 CHACHA.md 宪法
  - /memory 命令：显示/刷新记忆
  - /refresh 命令：手动触发 DreamPipeline

系统提示词和工具通过外部传入，不硬编码。
支持流式输出（逐字）。
"""

import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional


class AgentBridge:
    """CLI ↔ 核心模块桥接（v2.0）"""

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
        self._context_manager = None
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

    @property
    def context_manager(self):
        return self._context_manager

    # ====== 初始化 ======

    async def initialize(self) -> str:
        if not self._api_key:
            return "⚠️  未设置 API Key。使用 /key sk-xxx 设置。"
        await self._rebuild()
        await self._load_static_contexts()
        self._initialized = True
        return f"✅ 就绪 — 模型: {self._model} | 项目: {self._root.name}"

    async def _rebuild(self) -> None:
        from core.llm_invoker import LLMInvoker
        from core.llm_clients.openai_client import OpenAIClient
        from core.dispatcher import Dispatcher
        from core.tool_executor import ToolExecutor
        from core.context_manager import ContextManager
        from core.context.memory_manager import MemoryManager

        client = OpenAIClient(
            api_key=self._api_key, model=self._model,
            base_url=self._base_url, max_tokens=2000,
        )
        self._invoker = LLMInvoker(model_client=client)
        self._context_manager = ContextManager()

        mgr = MemoryManager(project_id=self._root.name)
        for tool in self._custom_tools:
            if hasattr(tool, '_mgr'):
                tool._mgr = mgr

        tools = ToolExecutor(tools=self._custom_tools)
        self._dispatcher = Dispatcher(self._invoker, tools)

        self._messages = [{"role": "system", "content": self._system_prompt}]

    async def _load_static_contexts(self) -> None:
        """加载 CHACHA.md / CHACHA_MEMORY.md / MEMORY.md 并注入 ContextManager。"""
        if not self._context_manager:
            return

        # 1. CHACHA.md 宪法
        chacha_path = self._root / "CHACHA.md"
        if chacha_path.exists():
            self._context_manager.set_static_rules(chacha_path.read_text(encoding="utf-8"))

        # 2. CHACHA_MEMORY.md 永久记忆（保护区）
        from core.context.memory_manager import MemoryManager
        mgr = MemoryManager(project_id=self._root.name)
        permanent = mgr.read_permanent_memory()
        if permanent:
            self._context_manager.set_permanent_memory(permanent)

        # 3. MEMORY.md 轻量索引（动态区）
        memory_index = mgr.read()
        if memory_index:
            self._context_manager.set_memory_index(memory_index)

    # ====== 命令 ======

    async def handle_command(self, cmd: str) -> str:
        parts = cmd.lstrip("/").strip().split(None, 1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if action == "help":
            return self._help_text()
        if action == "model":
            self._model = arg or self._model
            await self._rebuild()
            self._initialized = True
            return f"✅ 模型切换为: {self._model}"
        if action == "url":
            self._base_url = arg
            await self._rebuild()
            self._initialized = True
            return f"✅ API URL 切换为: {self._base_url}"
        if action == "key":
            self._api_key = arg
            await self._rebuild()
            self._initialized = True
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
        if action == "chacha":
            return await self._cmd_chacha(arg)
        if action == "memory":
            return await self._cmd_memory(arg)
        if action == "refresh":
            return await self._cmd_refresh(arg)
        return f"未知命令: /{action}。使用 /help 查看帮助。"

    def _help_text(self) -> str:
        return (
            "/model <name>  切换模型\n"
            "/url <url>     切换 API URL\n"
            "/key <sk-...>  设置 API Key\n"
            "/status        显示配置\n"
            "/clear         清除历史\n"
            "/chacha        显示 CHACHA.md 宪法\n"
            "/memory        显示记忆摘要\n"
            "/refresh       手动触发 Memory DreamPipeline\n"
            "/help          帮助"
        )

    async def _cmd_chacha(self, arg: str) -> str:
        """显示 CHACHA.md 宪法内容。"""
        chacha_path = self._root / "CHACHA.md"
        if not chacha_path.exists():
            return "CHACHA.md 不存在。在此项目根目录创建 CHACHA.md 以设置项目规则。"
        content = chacha_path.read_text(encoding="utf-8")
        if len(content) > 2000:
            content = content[:2000] + "\n... [截断]"
        return f"--- CHACHA.md ---\n{content}"

    async def _cmd_memory(self, arg: str) -> str:
        """显示记忆摘要。"""
        try:
            from core.context.memory_manager import MemoryManager
            mgr = MemoryManager(project_id=self._root.name)
            permanent = mgr.read_permanent_memory()
            index = mgr.read()
            days = mgr.list_days(limit=7)

            lines = ["--- 记忆状态 ---"]
            lines.append(f"永久记忆: {'已加载' if permanent else '无'} ({len(permanent)} 字符)")
            lines.append(f"索引记忆: {'已加载' if index else '无'} ({len(index)} 字符)")
            lines.append(f"最近记忆天数: {len(days)}")
            if days:
                lines.append(f"日期范围: {days[-1]} ~ {days[0]}")
            if permanent:
                preview = permanent[:500]
                lines.append(f"\n永久记忆预览:\n{preview}...")
            return "\n".join(lines)
        except Exception as e:
            return f"记忆查询失败: {e}"

    async def _cmd_refresh(self, arg: str) -> str:
        """手动触发 Memory DreamPipeline。"""
        try:
            from core.context.memory_manager import MemoryManager
            from core.context.dream import DreamPipeline

            mgr = MemoryManager(project_id=self._root.name)
            pipeline = DreamPipeline(self._invoker)
            memory_md, permanent_md = await pipeline.run(mgr)

            # 刷新上下文
            if self._context_manager:
                if permanent_md:
                    self._context_manager.set_permanent_memory(permanent_md)
                if memory_md:
                    self._context_manager.set_memory_index(memory_md)

            return (
                f"✅ DreamPipeline 完成\n"
                f"  MEMORY.md: {len(memory_md)} 字符\n"
                f"  CHACHA_MEMORY.md: {len(permanent_md)} 字符"
            )
        except Exception as e:
            return f"DreamPipeline 失败: {e}"

    # ====== 对话（流式） ======

    async def send_message(self, user_input: str) -> AsyncIterator[Dict[str, Any]]:
        if not self._dispatcher:
            yield {"type": "error", "message": "未初始化。请先设置 API Key。"}
            return

        self._messages.append({"role": "user", "content": user_input})
        t0 = __import__("time").monotonic()
        #full_response: list[str] = []  

        try:
            async for chunk in self._dispatcher.dispatch_stream(
                messages=self._messages,
                session_id=f"cli-{int(t0)}",
                max_rounds=10,
            ):
                #if chunk.get("type") == "text":
                #    full_response.append(chunk["content"])
                yield chunk
        except Exception as e:
            yield {"type": "error", "message": str(e)}
        #else:
        #    text = "".join(full_response)
        #    if text.strip():
        #        self._messages.append({"role": "assistant", "content": text})

    async def get_result(self) -> str:
        return ""

    async def reset(self) -> None:
        self._messages = self._messages[:1]

    async def run_dream(self) -> str:
        """运行 DreamPipeline 记忆整合。"""
        if not self._initialized:
            return "未初始化"
        try:
            from core.context.memory_manager import MemoryManager
            from core.context.dream import DreamPipeline
            mgr = MemoryManager(project_id=self._root.name)
            pipeline = DreamPipeline(self._invoker)
            memory_md, permanent_md = await pipeline.run(mgr)
            return (
                f"完成: MEMORY.md={len(memory_md)}字符, CHACHA_MEMORY.md={len(permanent_md)}字符"
                if memory_md else "无需整合"
            )
        except Exception as e:
            return f"失败: {e}"
