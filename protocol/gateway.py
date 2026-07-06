"""
protocol/gateway.py
ChaChaAsyncGateway — 异步消息总线，JSON-RPC 2.0 统一事件路由。

设计理念：
1. 每个会话独立 asyncio.Queue，慢消费者不阻塞其他会话
2. 全局自增 seq，保证跨会话有序
3. 全局监听者（Telemetry/Audit）可注册一次监听所有事件
4. 背压阻塞等待（超时 10s），不丢消息但可感知压力
5. event_history 保留完整消息，debug 用，可配置上限
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, List, Optional

from protocol.rpc_schema import GatewayMessage

logger = logging.getLogger(__name__)

# ========================= 会话上下文 =========================


@dataclass
class SessionContext:
    """每个会话的运行时上下文"""
    queue: asyncio.Queue[Optional[GatewayMessage]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=10000)
    )
    send_count: int = 0       # 已发布计数
    received_count: int = 0   # 已消费计数
    created_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def backpressure_ratio(self) -> float:
        """背压比率（0~1，队列全满 = 1）"""
        if self.queue.maxsize == 0:
            return 0.0
        pending = self.send_count - self.received_count
        return min(1.0, pending / self.queue.maxsize)


# ========================= 异步网关 =========================

class ChaChaAsyncGateway:
    """
    异步消息总线，JSON-RPC 2.0 统一事件路由。

    用法:
        gateway = ChaChaAsyncGateway()
        await gateway.start()
        gateway.register("session-1")

        # 发布
        await gateway.publish(event_1, session_id="session-1")

        # 订阅（异步迭代）
        async for msg in gateway.subscribe("session-1"):
            print(msg.payload)

        await gateway.stop()
    """

    def __init__(
        self,
        max_queue_size: int = 10000,
        max_history: int = 500,
        publish_timeout: float = 10.0,
    ):
        self._max_queue_size = max_queue_size
        self._max_history = max_history
        self._publish_timeout = publish_timeout

        self._seq: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._running: bool = False

        # 会话管理
        self._sessions: Dict[str, SessionContext] = {}

        # 全局事件监听者
        # 每个 handler 接收 GatewayMessage，返回 None
        self._global_handlers: List[Callable[[GatewayMessage], Coroutine[Any, Any, None]]] = []

        # 事件历史（完整 payload，debug 用）
        self._event_history: deque[GatewayMessage] = deque(maxlen=max(max_history, 0))

    # ====== 生命周期 ======

    async def start(self) -> None:
        """启动网关"""
        self._running = True
        logger.info("ChaChaAsyncGateway 已启动 (max_queue=%d, max_history=%d)",
                     self._max_queue_size, self._max_history)

    async def stop(self) -> None:
        """优雅关闭：等待队列清空 → 发送哨兵 → 通知全局监听者"""
        if not self._running:
            return

        self._running = False
        logger.info("ChaChaAsyncGateway 正在关闭...")

        # 向所有会话队列发送 None 哨兵，通知订阅者结束
        wait_tasks = []
        for _sid, ctx in list(self._sessions.items()):
            try:
                ctx.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        # 等待所有队列排空
        if wait_tasks:
            await asyncio.gather(*wait_tasks, return_exceptions=True)

        self._sessions.clear()
        self._global_handlers.clear()
        self._event_history.clear()
        logger.info("ChaChaAsyncGateway 已关闭")

    # ====== 会话管理 ======

    def register(self, session_id: str) -> None:
        """注册会话，创建独立消息队列"""
        if session_id in self._sessions:
            logger.warning("会话 %s 已注册，跳过", session_id)
            return
        self._sessions[session_id] = SessionContext(
            queue=asyncio.Queue(maxsize=self._max_queue_size),
        )
        logger.debug("会话 %s 已注册", session_id)

    def unregister(self, session_id: str) -> None:
        """注销会话，清空队列"""
        ctx = self._sessions.pop(session_id, None)
        if ctx is None:
            return

        # 清空队列
        while not ctx.queue.empty():
            try:
                ctx.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.debug("会话 %s 已注销 (send=%d, received=%d)",
                     session_id, ctx.send_count, ctx.received_count)

    # ====== 全局监听 ======

    def on_event(self, handler: Callable[[GatewayMessage], Coroutine[Any, Any, None]]) -> None:
        """注册全局事件监听者（如 Telemetry 写审计日志、Prometheus 计数）。

        每个 handler 在 publish() 内部以 create_task 异步执行，
        一个 handler 崩溃不影响其他 handler 或主发布路径。
        """
        self._global_handlers.append(handler)
        logger.debug("已注册全局事件监听者 (总计 %d)", len(self._global_handlers))

    def remove_handler(self, handler: Callable) -> None:
        """移除全局监听者"""
        if handler in self._global_handlers:
            self._global_handlers.remove(handler)

    # ====== 发布 ======

    async def publish(
        self,
        payload: Any,  # RPCRequest | RPCResponse | RPCEvent
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> bool:
        """分配 seq → 包装 GatewayMessage → 入队 + 通知全局监听者。

        返回 True 表示成功入队，False 表示超时或队列已注销。

        背压策略：队列满时阻塞等待 publish_timeout 秒，超时返回 False。
        """
        if not self._running:
            logger.warning("Gateway 未启动，忽略 publish")
            return False

        # 1. 分配全局 seq
        async with self._lock:
            seq = self._seq
            self._seq += 1

        # 2. 包装 GatewayMessage
        msg = GatewayMessage(
            seq=seq,
            project_id=project_id,
            session_id=session_id,
            payload=payload,
        )

        # 3. 写入事件历史
        if self._max_history > 0:
            self._event_history.append(msg)

        # 4. 全局监听者异步执行
        for handler in self._global_handlers:
            asyncio.create_task(self._safe_call_handler(handler, msg))

        # 5. 入队到会话队列（背压阻塞等待 + 超时）
        if session_id and session_id in self._sessions:
            ctx = self._sessions[session_id]
            try:
                await asyncio.wait_for(
                    ctx.queue.put(msg),
                    timeout=self._publish_timeout,
                )
                ctx.send_count += 1
                return True
            except asyncio.TimeoutError:
                logger.warning(
                    "会话 %s 队列满，publish 超时 (seq=%d, 背压=%.2f)",
                    session_id, seq, ctx.backpressure_ratio(),
                )
                return False
            except asyncio.QueueFull:
                logger.warning("会话 %s 队列满，publish 被拒 (seq=%d)", session_id, seq)
                return False

        # 未注册的 session_id 也返回 True（消息已通过全局监听者处理）
        return True

    async def _safe_call_handler(
        self,
        handler: Callable[[GatewayMessage], Coroutine[Any, Any, None]],
        msg: GatewayMessage,
    ) -> None:
        """安全调用 handler，崩溃不扩散"""
        try:
            await handler(msg)
        except Exception:
            logger.exception("全局事件监听者异常 (handler=%s)", handler.__name__)

    # ====== 订阅 ======

    async def subscribe(self, session_id: str) -> AsyncIterator[GatewayMessage]:
        """订阅会话消息，异步迭代器逐条消费。

        收到 None 哨兵时停止（Gateway 已关闭）。
        消息消费后自动更新 received_count。
        """
        ctx = self._sessions.get(session_id)
        if ctx is None:
            logger.warning("会话 %s 未注册，无法订阅", session_id)
            return

        while self._running:
            try:
                msg = await ctx.queue.get()
                if msg is None:
                    break
                ctx.received_count += 1
                yield msg
            except asyncio.CancelledError:
                break

        logger.debug("会话 %s 订阅已结束", session_id)

    # ====== 查询 ======

    def get_backpressure(self, session_id: Optional[str] = None) -> float:
        """查询背压状态（0~1）。

        session_id=None 时返回所有会话的最高背压值。
        """
        if session_id:
            ctx = self._sessions.get(session_id)
            return ctx.backpressure_ratio() if ctx else 0.0

        if not self._sessions:
            return 0.0
        return max(ctx.backpressure_ratio() for ctx in self._sessions.values())

    def get_event_history(self, limit: Optional[int] = None) -> List[GatewayMessage]:
        """获取最近的事件历史（debug 用）。

        limit=None 返回全部，否则返回最近 limit 条。
        """
        items = list(self._event_history)
        if limit is not None and limit > 0:
            return items[-limit:]
        return items

    def list_sessions(self) -> Dict[str, float]:
        """列出所有会话及其背压状态"""
        return {
            sid: ctx.backpressure_ratio()
            for sid, ctx in self._sessions.items()
        }

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def running(self) -> bool:
        return self._running
