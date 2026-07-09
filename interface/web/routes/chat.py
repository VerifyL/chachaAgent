"""
interface/web/routes/chat.py
聊天 WebSocket 端点 — 流式对话。
"""

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

    服务端事件格式:
        {"type": "text", "content": "..."}
        {"type": "reasoning", "content": "..."}
        {"type": "tool_call_start", "tool_name": "...", "tool_index": 0}
        {"type": "tool_call_end", "tool_index": 0}
        {"type": "tool_exec_start", "tool_name": "...", "args": "..."}
        {"type": "tool_exec_end", "tool_name": "...", "preview": "..."}
        {"type": "done", "text": "...", "tokens": 1234, "usage": {...}}
        {"type": "error", "message": "..."}
        {"type": "compact", "reason": "..."}
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
    bridge.set_tools_for_session(session_svc.memory_manager)
    bridge.set_checkpoint_dir(session_svc.memory_manager.session_dir)
    bridge.build_orchestrator(
        session_id=session_svc.session_id,
        memory_manager=session_svc.memory_manager,
    )

    logger.info(f"[ws] 新连接, session={session_svc.session_id}, model={bridge.model}")

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
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "消息内容不能为空",
                        }
                    )
                    continue

                try:
                    async for event in bridge.chat_stream(
                        content,
                        session_id=session_svc.session_id,
                        memory_manager=session_svc.memory_manager,
                    ):
                        await websocket.send_json(event)
                    # 每轮对话结束后保存 checkpoint
                    bridge.save_checkpoint()
                except Exception as e:
                    logger.error(f"[ws] 聊天异常: {e}")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": str(e),
                        }
                    )

            elif msg_type == "new_session":
                session_svc.new()
                bridge.set_tools_for_session(session_svc.memory_manager)
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

            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f"未知消息类型: {msg_type}",
                    }
                )

    except WebSocketDisconnect:
        logger.info(f"[ws] 客户端断开, session={session_svc.session_id}")
    except Exception as e:
        logger.error(f"[ws] 异常: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
