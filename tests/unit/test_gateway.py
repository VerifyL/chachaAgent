"""
tests/unit/test_gateway.py
单元测试：protocol/gateway.py ChaChaAsyncGateway
覆盖：启停、会话注册/注销、publish→subscribe 消息序、seq 自增、
      背压阻塞超时、全局监听者、event_history、并发生产者
"""

import asyncio

import pytest

from protocol.gateway import ChaChaAsyncGateway
from protocol.rpc_schema import (
    GatewayMessage,
    RPCRequest,
    SystemNotificationEvent,
    TokenChunkEvent,
)


@pytest.fixture
async def gateway():
    g = ChaChaAsyncGateway(max_queue_size=10, max_history=50, publish_timeout=0.5)
    await g.start()
    yield g
    await g.stop()


# ========== 1. 生命周期 ==========


class TestLifecycle:
    async def test_start_stop(self, gateway):
        assert gateway.running is True
        await gateway.stop()
        assert gateway.running is False

    async def test_double_stop_no_error(self, gateway):
        await gateway.stop()
        await gateway.stop()  # 不应报错


# ========== 2. 会话管理 ==========


class TestSessionManagement:
    async def test_register_and_list(self, gateway):
        gateway.register("s1")
        gateway.register("s2")
        sessions = gateway.list_sessions()
        assert "s1" in sessions
        assert "s2" in sessions

    async def test_double_register_no_error(self, gateway):
        gateway.register("s1")
        gateway.register("s1")  # 不应报错

    async def test_unregister(self, gateway):
        gateway.register("s1")
        gateway.unregister("s1")
        assert "s1" not in gateway.list_sessions()

    async def test_subscribe_unregistered(self, gateway):
        """未注册会话订阅不应报错"""
        msgs = []
        async for msg in gateway.subscribe("nonexistent"):
            msgs.append(msg)
        assert len(msgs) == 0


# ========== 3. 发布与订阅 ==========


class TestPublishSubscribe:
    async def test_single_message(self, gateway):
        gateway.register("s1")
        req = RPCRequest(method="user/message", params={"content": "hello"})

        # 发布
        ok = await gateway.publish(req, session_id="s1")
        assert ok is True

        # 订阅
        msgs = []
        async for msg in gateway.subscribe("s1"):
            msgs.append(msg)
            if len(msgs) >= 1:
                break

        assert len(msgs) == 1
        assert isinstance(msgs[0].payload, RPCRequest)

    async def test_multiple_messages_ordered(self, gateway):
        gateway.register("s1")

        for i in range(5):
            await gateway.publish(RPCRequest(method="test", params={"i": i}), session_id="s1")

        msgs = []
        async for msg in gateway.subscribe("s1"):
            msgs.append(msg)
            if len(msgs) >= 5:
                break

        assert len(msgs) == 5
        # seq 递增
        for i in range(1, 5):
            assert msgs[i].seq > msgs[i - 1].seq

    async def test_messages_cross_sessions_isolated(self, gateway):
        gateway.register("s1")
        gateway.register("s2")

        await gateway.publish(TokenChunkEvent().set_delta("s1-msg"), session_id="s1")
        await gateway.publish(TokenChunkEvent().set_delta("s2-msg"), session_id="s2")

        # s1 只收到自己的消息
        s1_msgs = []
        async for msg in gateway.subscribe("s1"):
            s1_msgs.append(msg)
            if len(s1_msgs) >= 1:
                break

        assert len(s1_msgs) == 1
        assert s1_msgs[0].session_id == "s1"

    async def test_publish_to_unknown_session_still_returns_true(self, gateway):
        """未注册的 session 仍返回 True（全局监听者仍可处理）"""
        ok = await gateway.publish(RPCRequest(method="test"), session_id="unknown")
        assert ok is True

    async def test_publish_when_stopped(self, gateway):
        await gateway.stop()
        ok = await gateway.publish(RPCRequest(method="test"), session_id="s1")
        assert ok is False


# ========== 4. seq 全局有序 ==========


class TestSeqOrdering:
    async def test_seq_increments_globally(self, gateway):
        gateway.register("s1")
        gateway.register("s2")

        _mid_s1 = await gateway.publish(RPCRequest(method="a"), session_id="s1")
        _mid_s2 = await gateway.publish(RPCRequest(method="b"), session_id="s2")

        # 两个会话共享全局 seq
        assert gateway.seq == 2

        msgs = []
        async for msg in gateway.subscribe("s1"):
            msgs.append(msg)
            if len(msgs) >= 1:
                break

        assert msgs[0].seq == 0
        assert msgs[0].session_id == "s1"


# ========== 5. 背压 ==========


class TestBackpressure:
    async def test_backpressure_ratio_zero_when_empty(self, gateway):
        gateway.register("s1")
        assert gateway.get_backpressure("s1") == 0.0

    async def test_backpressure_increases_with_pending(self, gateway):
        gateway.register("s1")
        # 发布但不消费 → 背压上升
        for _ in range(5):
            await gateway.publish(RPCRequest(method="test"), session_id="s1")

        bp = gateway.get_backpressure("s1")
        assert bp > 0.0

    async def test_backpressure_max_across_sessions(self, gateway):
        gateway.register("s1")
        gateway.register("s2")
        for _ in range(3):
            await gateway.publish(RPCRequest(method="test"), session_id="s1")

        bp = gateway.get_backpressure()  # 不传 session_id → 返回所有 session 中最高值
        assert bp == gateway.get_backpressure("s1")

    @pytest.mark.slow
    async def test_publish_timeout_when_queue_full(self):
        """队列满 + 无消费者 → 超时返回 False"""
        g = ChaChaAsyncGateway(max_queue_size=2, publish_timeout=0.1)
        await g.start()
        g.register("s1")

        try:
            # 填满队列（2 条）
            assert await g.publish(RPCRequest(method="1"), session_id="s1") is True
            assert await g.publish(RPCRequest(method="2"), session_id="s1") is True

            # 第 3 条应该超时
            ok = await g.publish(RPCRequest(method="3"), session_id="s1")
            assert ok is False
        finally:
            await g.stop()


# ========== 6. 全局监听者 ==========


class TestGlobalHandlers:
    async def test_global_handler_receives_events(self, gateway):
        gateway.register("s1")
        received = []

        async def handler(msg: GatewayMessage) -> None:
            received.append(msg.seq)

        gateway.on_event(handler)
        await gateway.publish(RPCRequest(method="test"), session_id="s1")

        # 给 create_task 一点时间执行
        await asyncio.sleep(0.05)
        assert len(received) == 1

    async def test_global_handler_crash_does_not_break_publish(self, gateway):
        gateway.register("s1")

        async def crashy(_msg: GatewayMessage) -> None:
            raise RuntimeError("handler crash")

        gateway.on_event(crashy)
        # 不应抛异常
        ok = await gateway.publish(RPCRequest(method="test"), session_id="s1")
        assert ok is True

    async def test_multiple_handlers_all_called(self, gateway):
        gateway.register("s1")
        results = []

        async def h1(msg: GatewayMessage) -> None:
            results.append(1)

        async def h2(msg: GatewayMessage) -> None:
            results.append(2)

        gateway.on_event(h1)
        gateway.on_event(h2)
        await gateway.publish(RPCRequest(method="test"), session_id="s1")
        await asyncio.sleep(0.05)

        assert 1 in results
        assert 2 in results

    async def test_global_handler_even_without_session(self, gateway):
        """未注册会话的消息仍触发全局监听者"""
        received = []

        async def handler(msg: GatewayMessage) -> None:
            received.append(msg)

        gateway.on_event(handler)
        await gateway.publish(SystemNotificationEvent(params={"level": "info", "message": "test"}))

        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert isinstance(received[0].payload, SystemNotificationEvent)


# ========== 7. 事件历史 ==========


class TestEventHistory:
    async def test_event_history_stores_messages(self, gateway):
        gateway.register("s1")
        await gateway.publish(RPCRequest(method="a"), session_id="s1")
        await gateway.publish(RPCRequest(method="b"), session_id="s1")

        history = gateway.get_event_history()
        assert len(history) == 2
        assert history[0].payload.method == "a"
        assert history[1].payload.method == "b"

    async def test_event_history_limit(self, gateway):
        gateway.register("s1")
        for i in range(10):
            await gateway.publish(RPCRequest(method=str(i)), session_id="s1")

        recent = gateway.get_event_history(limit=3)
        assert len(recent) == 3
        assert recent[-1].payload.method == "9"

    async def test_event_history_capped(self):
        """超出 max_history 时自动丢弃旧消息"""
        g = ChaChaAsyncGateway(max_history=5)
        await g.start()
        g.register("s1")

        for i in range(10):
            await g.publish(RPCRequest(method=str(i)), session_id="s1")

        history = g.get_event_history()
        assert len(history) == 5
        # 最旧的被丢弃，保留最近的
        assert history[0].payload.method == "5"
        assert history[-1].payload.method == "9"

        await g.stop()


# ========== 8. stop 哨兵 ==========


class TestStopSentinel:
    async def test_stop_sends_none_sentinel(self, gateway):
        gateway.register("s1")
        await gateway.publish(RPCRequest(method="test"), session_id="s1")

        # 并发：先订阅，稍后 stop
        async def collect():
            msgs = []
            async for msg in gateway.subscribe("s1"):
                msgs.append(msg)
            return msgs

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.02)
        await gateway.stop()

        msgs = await task
        # 至少收到了 publish 的消息
        assert any(isinstance(m.payload, RPCRequest) for m in msgs)


# ========== 9. 并发 ==========


class TestConcurrent:
    async def test_concurrent_publishers(self, gateway):
        """多个协程同时 publish 不丢失消息"""
        gateway.register("s1")

        async def publisher(n: int):
            for i in range(n):
                await gateway.publish(RPCRequest(method=str(i)), session_id="s1")

        count = 20
        await asyncio.gather(
            publisher(count // 2),
            publisher(count // 2),
        )

        assert gateway.seq == count
        history = gateway.get_event_history()
        assert len(history) == count

    async def test_concurrent_seq_no_gaps(self, gateway):
        """并发 publish 时 seq 无空洞"""
        gateway.register("s1")

        async def publish_one(i: int):
            await gateway.publish(RPCRequest(method=str(i)), session_id="s1")

        await asyncio.gather(*[publish_one(i) for i in range(10)])

        history = gateway.get_event_history()
        seqs = sorted([m.seq for m in history])
        assert seqs == list(range(10))
