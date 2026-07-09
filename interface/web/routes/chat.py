"""
interface/web/routes/chat.py
聊天 WebSocket 端点 — 流式对话。

取消机制（通用方案）:
  - 主循环**绝不 await**旧 task，只 fire-and-forget cancel
  - 用 generation 号标记每一轮，旧 gen 的输出自动丢弃
  - stop 时：cancel → gen+1 → restore_checkpoint → done(cancelled)
  - 新 chat 时：save_checkpoint → cancel 旧 task → gen+1 → 立即创建新 task

  这确保无论如何阻塞（LLM 流 / MCP 异步 / bash 同步），
  主循环始终响应，新消息立刻开始处理。
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """WebSocket 流式聊天端点

    支持 query param ?session_id=xxx 恢复已有会话。

    客户端消息格式:
        {"type": "chat", "content": "用户输入"}
        {"type": "new_session"}
        {"type": "stop"}
        {"type": "permission_response", "request_id": "...", "approved": true}

    服务端事件格式:
        {"type": "text", "content": "..."}
        {"type": "reasoning", "content": "..."}
        {"type": "tool_call_start", "tool_name": "...", "tool_index": 0}
        {"type": "tool_call_end", "tool_index": 0}
        {"type": "tool_exec_start", "tool_name": "...", "args": "..."}
        {"type": "tool_exec_end", "tool_name": "...", "preview": "..."}
        {"type": "permission_request", "request_id": "...", "tool_name": "...", ...}
        {"type": "done", "tokens": 0, "cancelled": true}  (取消时)
        {"type": "error", "message": "..."}
        {"type": "compact", "reason": "...", "old_msgs": N, "new_msgs": N, "old_tokens": N, "new_tokens": N}
        {"type": "session_created", "session_id": "..."}
    """
    await websocket.accept()

    bridge = websocket.app.state.bridge
    project_root = bridge.project_root

    # 尝试从 query param 恢复已有会话
    requested_sid = websocket.query_params.get("session_id")
    existing_sid = None
    if requested_sid:
        from core.context.memory_manager import MemoryManager

        mm = MemoryManager(project_root=project_root)
        if requested_sid in mm.list_all_sessions():
            existing_sid = requested_sid

    session_svc = SessionService(project_root, session_id=existing_sid)

    # 注入 session 工具 + 运行时依赖
    await bridge.set_tools_for_session(session_svc.memory_manager)
    bridge.set_checkpoint_dir(session_svc.memory_manager.session_dir)
    bridge.build_orchestrator(
        session_id=session_svc.session_id,
        memory_manager=session_svc.memory_manager,
    )

    logger.info(f"[ws] 新连接, session={session_svc.session_id}, model={bridge.model}")

    # ── 通用取消机制的核心状态 ──
    chat_task: asyncio.Task | None = None
    generation: int = 0  # 当前"代"号，旧 task 的 gen 不匹配则所有输出自动丢弃

    async def _run_chat(gen: int, content: str) -> None:
        """后台任务：流式聊天 → WebSocket。

        每个事件发送前检查 gen == generation，
        不匹配说明已被取消/替代，立即静默退出。
        CancelledError 不在此处处理（由主循环统一处理）。
        """
        try:
            async for event in bridge.chat_stream(
                content,
                session_id=session_svc.session_id,
                memory_manager=session_svc.memory_manager,
            ):
                if gen != generation:
                    return  # 已被替代，静默退出
                await websocket.send_json(event)

            # 正常结束
            if gen == generation:
                bridge.save_checkpoint()
                await websocket.send_json({"type": "done", "tokens": 0})

        except asyncio.CancelledError:
            # 不在此处发送 done(cancelled) 或 restore，由主循环统一处理
            raise

        except Exception as e:
            logger.error(f"[ws] 聊天异常: {e}")
            if gen == generation:
                try:
                    await websocket.send_json({"type": "error", "message": str(e)})
                except Exception:
                    pass

    async def _cancel_current() -> None:
        """取消当前 task：fire-and-forget + 递增 gen 号使旧输出失效。"""
        nonlocal chat_task, generation

        if chat_task and not chat_task.done():
            chat_task.cancel()
            chat_task = None
        generation += 1  # 旧 gen 的所有输出自动丢弃

    try:
        # 发送会话 ID 给客户端
        await websocket.send_json(
            {
                "type": "session_created",
                "session_id": session_svc.session_id,
                "model": bridge.model,
            }
        )

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "chat":
                content = data.get("content", "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "message": "消息内容不能为空"})
                    continue

                # 1. 取消正在运行的旧 task（不等待）
                await _cancel_current()

                # 2. 保存本轮前 checkpoint（用于取消时回滚）
                bridge.save_checkpoint()

                # 3. 立即启动新 task
                chat_task = asyncio.create_task(_run_chat(generation, content))

            elif msg_type == "stop":
                if chat_task and not chat_task.done():
                    # 取消旧 task（不等待）
                    chat_task.cancel()
                    chat_task = None
                    generation += 1

                    # 回滚到本轮前干净状态
                    bridge.restore_checkpoint()

                await websocket.send_json({"type": "done", "tokens": 0, "cancelled": True})

            elif msg_type == "new_session":
                # 取消当前 task
                await _cancel_current()

                session_svc.new()
                await bridge.set_tools_for_session(session_svc.memory_manager)
                bridge.set_checkpoint_dir(session_svc.memory_manager.session_dir)
                bridge.build_orchestrator(
                    session_id=session_svc.session_id,
                    memory_manager=session_svc.memory_manager,
                )
                await websocket.send_json(
                    {
                        "type": "session_created",
                        "session_id": session_svc.session_id,
                        "model": bridge.model,
                    }
                )

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif msg_type == "compact_now":
                # 手动压缩上下文（强制，跳过阈值检查）
                payload = await bridge.compact_context(force=True)
                if payload:
                    await websocket.send_json(payload)
                else:
                    await websocket.send_json(
                        {
                            "type": "compact",
                            "reason": "压缩完成（已是最小上下文）",
                            "old_msgs": 0,
                            "new_msgs": 0,
                            "old_tokens": 0,
                            "new_tokens": 0,
                        }
                    )

            elif msg_type == "permission_response":
                request_id = data.get("request_id", "")
                approved = data.get("approved", False)
                bridge.resolve_approval(request_id, approved)

            else:
                await websocket.send_json({"type": "error", "message": f"未知消息类型: {msg_type}"})

    except WebSocketDisconnect:
        logger.info(f"[ws] 客户端断开, session={session_svc.session_id}")
    except Exception as e:
        logger.error(f"[ws] 异常: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        # fire-and-forget：不等待 task 完成
        if chat_task and not chat_task.done():
            chat_task.cancel()
