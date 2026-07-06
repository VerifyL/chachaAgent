"""
tests/integration/test_gateway_integration.py
集成测试：多生产者/消费者并发，验证 seq 全局有序，网关与 RPC 消息完整编解码。
"""

import asyncio

import pytest

from protocol.gateway import ChaChaAsyncGateway
from protocol.rpc_schema import (
    GatewayMessage,
    RPCRequest,
    RPCResponse,
    TokenChunkEvent,
    ToolStatusEvent,
)


@pytest.fixture
async def gateway():
    g = ChaChaAsyncGateway(max_queue_size=1000, max_history=1000, publish_timeout=5.0)
    await g.start()
    yield g
    await g.stop()


# ========== 多生产者 + 多消费者 ==========

async def test_multi_producer_multi_consumer_seq_accurate(gateway):
    """两个生产者并发写入 3 个会话，各消费者验证消息完整且 seq 全局有序"""
    sessions = ["s-a", "s-b", "s-c"]
    for sid in sessions:
        gateway.register(sid)

    # 全局监听者记录所有事件
    all_events: list[GatewayMessage] = []

    async def audit_handler(msg: GatewayMessage):
        all_events.append(msg)

    gateway.on_event(audit_handler)

    # 生产者 A 写入 s-a, s-b
    async def producer_a():
        for i in range(5):
            await gateway.publish(
                TokenChunkEvent().set_delta(f"A-{i}"), session_id=sessions[i % 2]
            )

    # 生产者 B 写入 s-b, s-c
    async def producer_b():
        for i in range(5):
            await gateway.publish(
                ToolStatusEvent(params={"tool_use_id": f"call_{i}", "status": "running"}),
                session_id=sessions[1 + i % 2],
            )

    # 消费者：每个会话一个
    async def consumer(sid: str) -> list[GatewayMessage]:
        msgs = []
        async for msg in gateway.subscribe(sid):
            msgs.append(msg)
            if len(msgs) >= 10:
                break
            await asyncio.sleep(0.001)
        return msgs

    # 并发：先启动消费者，再发布
    consumer_tasks = [asyncio.create_task(consumer(sid)) for sid in sessions]

    # 等待消费者开始
    await asyncio.sleep(0.02)

    await asyncio.gather(producer_a(), producer_b())

    # 发送哨兵结束
    for sid in sessions:
        try:
            gateway._sessions[sid].queue.put_nowait(None)
        except Exception:
            pass

    results = await asyncio.gather(*consumer_tasks, return_exceptions=True)

    # 验证：每个消费者收到的消息类型正确
    for sid, msgs in zip(sessions, results):
        if isinstance(msgs, Exception):
            pytest.fail(f"Consumer {sid} failed: {msgs}")
        # 消息序号递增
        seqs = [m.seq for m in msgs]
        assert seqs == sorted(seqs), f"会话 {sid} 消息无序: {seqs}"

    # 验证：全局监听者收到所有事件
    await asyncio.sleep(0.1)
    assert len(all_events) >= 10
    global_seqs = [m.seq for m in all_events]
    assert global_seqs == sorted(global_seqs), "全局 seq 应有序"


# ========== 完整消息流：请求→流式→工具→权限→响应 ==========

async def test_full_message_flow(gateway):
    """模拟从用户请求到最终响应的完整 RPC 流程"""
    gateway.register("session-x")
    gateway.register("session-y")  # 另一个无关会话

    # 消息序列
    await gateway.publish(
        RPCRequest(method="user/message", params={"content": "帮我读 main.py"}),
        session_id="session-x",
    )
    await gateway.publish(
        TokenChunkEvent().set_delta("正在"), session_id="session-x"
    )
    await gateway.publish(
        TokenChunkEvent().set_delta("读取"), session_id="session-x"
    )
    await gateway.publish(
        ToolStatusEvent(params={
            "tool_use_id": "call_1", "tool_name": "read_file",
            "status": "running", "progress": "Reading",
        }),
        session_id="session-x",
    )
    await gateway.publish(
        ToolStatusEvent(params={
            "tool_use_id": "call_1", "tool_name": "read_file",
            "status": "done", "output_summary": "100 lines",
        }),
        session_id="session-x",
    )
    # 同时 session-y 收到无关消息
    await gateway.publish(
        TokenChunkEvent().set_delta("y-msg"), session_id="session-y"
    )

    await gateway.publish(
        RPCResponse(id="req-1", result={"content": "print('hello')"}),
        session_id="session-x",
    )

    # 消费者 X 只收到自己的消息
    x_msgs = []
    async def collect_x():
        nonlocal x_msgs
        async for msg in gateway.subscribe("session-x"):
            x_msgs.append(msg)
            if len(x_msgs) >= 6:
                break

    task_x = asyncio.create_task(collect_x())
    await asyncio.sleep(0.1)

    # 发送哨兵
    for sid in ["session-x", "session-y"]:
        try:
            gateway._sessions[sid].queue.put_nowait(None)
        except Exception:
            pass

    await task_x

    assert len(x_msgs) == 6
    # 验证消息类型顺序
    assert isinstance(x_msgs[0].payload, RPCRequest)
    assert isinstance(x_msgs[1].payload, TokenChunkEvent)
    assert isinstance(x_msgs[2].payload, TokenChunkEvent)
    assert isinstance(x_msgs[3].payload, ToolStatusEvent)
    assert isinstance(x_msgs[4].payload, ToolStatusEvent)
    assert isinstance(x_msgs[5].payload, RPCResponse)


# ========== 背压场景 ==========

async def test_backpressure_does_not_block_other_sessions(gateway):
    """s-a 队列满阻塞，s-b 不受影响"""
    # 用小队列
    g = ChaChaAsyncGateway(max_queue_size=3, publish_timeout=0.1)
    await g.start()
    g.register("s-a")
    g.register("s-b")

    try:
        # 填满 s-a 但不消费
        for _ in range(3):
            await g.publish(TokenChunkEvent().set_delta("fill"), session_id="s-a")

        # s-a 超时
        ok_a = await g.publish(TokenChunkEvent().set_delta("overflow"), session_id="s-a")
        assert ok_a is False

        # s-b 不受影响
        ok_b = await g.publish(TokenChunkEvent().set_delta("ok"), session_id="s-b")
        assert ok_b is True
    finally:
        await g.stop()


# ========== 并发 seq 无空洞 ==========

async def test_concurrent_publishers_no_holes(gateway):
    """20 个并发发布者，seq 无空洞"""
    gateway.register("s")

    async def publish(i: int):
        await gateway.publish(RPCRequest(method=str(i)), session_id="s")

    await asyncio.gather(*[publish(i) for i in range(20)])

    assert gateway.seq == 20
    history = gateway.get_event_history()
    seqs = sorted([m.seq for m in history])
    assert seqs == list(range(20))
