"""
interface/web/web_bridge.py
WebBridge — WebSocket 适配桥接层。

封装 AgentBridge，为 Web 端提供与 CLI 同等的流式聊天能力。
单例模式：服务启动时初始化一次，所有 WebSocket 连接复用。
"""

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from interface.cli.agent_bridge import AgentBridge

logger = logging.getLogger(__name__)


class WebBridge:
    """Web 桥接层 — 封装 AgentBridge，为 WebSocket 优化"""

    def __init__(self, project_root: Optional[Path] = None):
        self._root = project_root or Path.cwd()
        self._bridge = AgentBridge(project_root=self._root)
        self._initialized = False

    # ====== 生命周期 ======

    async def initialize(self) -> str:
        """初始化 LLM + Dispatcher + MCP（服务启动时调用一次）"""
        if self._initialized:
            return "已初始化"
        result = await self._bridge.initialize()
        self._initialized = True
        logger.info(f"[web] bridge 初始化完成: {result}")
        return result

    async def shutdown(self) -> None:
        """优雅关闭：断开 MCP 连接"""
        if self._bridge:
            await self._bridge.shutdown()
            logger.info("[web] bridge 已关闭")

    # ====== 聊天流 ======

    async def chat_stream(
        self,
        user_input: str,
        session_id: str = "",
        memory_manager=None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式聊天：将 StreamEvent 对象转为 JSON 兼容字典"""
        async for event in self._bridge.send_message_orchestrated(
            user_input,
            session_id=session_id,
            memory_manager=memory_manager,
        ):
            yield event.model_dump()

    # ====== 会话工具注入 ======

    def set_tools_for_session(self, memory_manager) -> None:
        """为指定 session 重建工具集（含 memory 工具）"""
        self._bridge.set_tools_for_session(memory_manager)

    def build_orchestrator(self, session_id: str = "", memory_manager=None) -> None:
        """注入运行时依赖到 Orchestrator"""
        self._bridge.build_orchestrator(session_id=session_id, memory_manager=memory_manager)

    # ====== Checkpoint ======

    def set_checkpoint_dir(self, path) -> None:
        """设置当前 session 的 checkpoint 目录"""
        self._bridge.set_checkpoint_dir(path)

    def save_checkpoint(self) -> None:
        """保存当前会话状态的 checkpoint"""
        self._bridge.save_checkpoint()

    # ====== 属性 ======

    @property
    def project_root(self) -> Path:
        return self._root

    @property
    def model(self) -> str:
        return self._bridge._model

    @property
    def initialized(self) -> bool:
        return self._initialized
