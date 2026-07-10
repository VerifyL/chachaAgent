"""
interface/web/web_bridge.py
WebBridge — WebSocket 适配桥接层。

封装 AgentBridge，为 Web 端提供与 CLI 同等的流式聊天能力。
单例模式：服务启动时初始化一次，所有 WebSocket 连接复用。

v2: 审批支持 — 通过混流队列（interleave queue）注入异步审批，
    ToolExecutor 阻塞等待时，审批事件仍能推送到 WebSocket 客户端。
"""

import asyncio
import logging
import uuid
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

        # ── 审批系统 ──
        # 混流队列：同时承载 stream 事件和审批请求事件
        self._interleave_queue: asyncio.Queue = asyncio.Queue()
        # 等待审批的 Future 映射：request_id → Future[bool]
        self._pending_approvals: Dict[str, asyncio.Future] = {}

    # ====== 生命周期 ======

    async def initialize(self) -> str:
        """初始化 LLM + Dispatcher + MCP（服务启动时调用一次）"""
        if self._initialized:
            return "已初始化"
        result = await self._bridge.initialize()
        # 注入 Web 端审批处理器（覆盖 CLI 的 input() 交互式审批）
        self._bridge.set_approval_handler(self._web_approval_handler)
        self._initialized = True
        logger.info(f"[web] bridge 初始化完成: {result}")
        return result

    async def shutdown(self) -> None:
        """优雅关闭：断开 MCP 连接、拒绝所有待审批请求"""
        self._reject_all_approvals()
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
        """流式聊天：混流输出 stream 事件 + 审批请求事件。

        核心设计：
        - stream 事件来自 bridge.send_message_orchestrated()（后台 task）
        - 审批事件来自 _web_approval_handler（tool 执行中触发）
        - 两者汇入同一个 _interleave_queue，保证事件时序正确

        取消安全：
        - 每次调用使用唯一 done_token，防止上一轮 cancel 残留的
          ("done", None) 毒化队列（导致下一轮 chat 立即退出）。
        - finally 中清理残留队列 + await task 加超时。
        """
        # ── 唯一 done_token：防止旧 cancel 残留毒化队列 ──
        done_token = object()

        async def _run_bridge() -> None:
            """后台运行 bridge 流，所有事件 push 到混流队列"""
            try:
                async for event in self._bridge.send_message_orchestrated(
                    user_input,
                    session_id=session_id,
                    memory_manager=memory_manager,
                ):
                    await self._interleave_queue.put(("stream", event.model_dump()))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[web] bridge 流异常: {e}")
                await self._interleave_queue.put(("stream", {"type": "error", "message": str(e)}))
            finally:
                await self._interleave_queue.put(("done", done_token))

        # ── 清理上一轮 cancel 残留（防御性） ──
        while not self._interleave_queue.empty():
            try:
                self._interleave_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        task = asyncio.create_task(_run_bridge())

        try:
            while True:
                kind, data = await self._interleave_queue.get()
                if kind == "done":
                    # 仅匹配本轮 done_token；旧 cancel 残留的 done 直接忽略
                    if data is done_token:
                        break
                    continue
                # "stream" 和审批事件都走这里输出
                yield data
        finally:
            # 清理：取消后台 task + 拒绝所有待审批
            if not task.done():
                task.cancel()
                try:
                    # 加超时防止 _run_bridge 卡在同步操作中永久阻塞
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            self._reject_all_approvals()
            # 清理本轮残留（含 _run_bridge finally 放入的 done_token）
            while not self._interleave_queue.empty():
                try:
                    self._interleave_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    # ====== 审批系统 ======

    async def _web_approval_handler(self, req) -> bool:
        """Web 端异步审批处理器。

        被 ToolExecutor 调用（同步语义，即需返回 bool）。
        内部：向混流队列推送审批事件 → 创建 Future 等待 → WebSocket 客户端响应后 resolve。
        """
        rid = uuid.uuid4().hex[:12]
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_approvals[rid] = future

        # 向混流队列推送审批请求（在阻塞等待之前推送）
        # 参数值截断以防大参数撑爆 WebSocket 帧
        safe_args = {}
        for k, v in (req.arguments or {}).items():
            s = str(v)
            safe_args[k] = s[:500] + ("..." if len(s) > 500 else "")

        await self._interleave_queue.put(
            (
                "stream",
                {
                    "type": "permission_request",
                    "request_id": rid,
                    "tool_name": req.tool_name,
                    "arguments": safe_args,
                    "risk_level": req.risk_level,
                    "risk_score": req.risk_score,
                    "reason": req.reason,
                    "diff": req.diff,
                },
            )
        )

        try:
            return await asyncio.wait_for(future, timeout=120.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self._pending_approvals.pop(rid, None)

    def resolve_approval(self, request_id: str, approved: bool) -> None:
        """WebSocket 路由调用：客户端对审批请求做出响应。"""
        future = self._pending_approvals.get(request_id)
        if future and not future.done():
            future.set_result(approved)

    def _reject_all_approvals(self) -> None:
        """拒绝所有待审批请求（连接断开/关闭时调用）。"""
        for rid, future in list(self._pending_approvals.items()):
            if not future.done():
                future.set_result(False)
        self._pending_approvals.clear()

    # ====== 会话工具注入 ======

    async def set_tools_for_session(self, memory_manager) -> None:
        """为指定 session 重建工具集（含 memory 工具），并重新注入审批处理器。"""
        await self._bridge.set_tools_for_session(memory_manager)
        # rebuild() 会把 approval_handler 重置为 CLI 版本，重新注入 Web 版
        self._bridge.set_approval_handler(self._web_approval_handler)

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

    def restore_checkpoint(self) -> None:
        """回滚到上次保存的 checkpoint"""
        self._bridge.restore_checkpoint()

    async def compact_context(self, *, force: bool = False) -> dict | None:
        """手动触发上下文压缩。返回 compact 事件 dict 或 None。
        force=True 时跳过阈值检查，强制执行。"""
        stats = await self._bridge.compact_context(force=force)
        if stats:
            return {"type": "compact", **stats}
        return None

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
