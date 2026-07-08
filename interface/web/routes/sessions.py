"""
interface/web/routes/sessions.py
会话管理 REST API — 列表/详情/新建/删除。
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from core.context.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _get_project_root() -> Path:
    """获取项目根目录 — 从 server 模块导入 bridge，确保与启动时传入的 project_root 一致"""
    from interface.web.server import get_bridge

    return get_bridge().project_root


@router.get("/sessions")
async def list_sessions():
    """列出所有会话"""
    root = _get_project_root()
    mgr = MemoryManager(project_root=root)
    sessions = mgr.list_all_sessions()

    result = []
    for sid in sessions:
        smgr = MemoryManager(project_root=root, session_id=sid)
        preview = ""
        days = smgr.list_days(limit=5)
        for day in days:
            for line in smgr.read_day(day).split("\n"):
                if line.strip().startswith("Q:"):
                    preview = line.strip()[2:].strip()[:60]
                    break
            if preview:
                break
        if not preview:
            preview = "(新会话)"

        # 解析时间（session_id 格式: YYYYMMDD-HHMMSS）
        time_str = ""
        if len(sid) > 14:
            time_str = f"{sid[:4]}-{sid[4:6]}-{sid[6:8]} {sid[9:11]}:{sid[11:13]}:{sid[13:15]}"

        result.append({
            "id": sid,
            "preview": preview,
            "time": time_str or sid,
        })

    # 新建在前
    result.sort(key=lambda s: s["id"], reverse=True)
    return result


@router.get("/sessions/{session_id}")
async def get_session_messages(session_id: str):
    """获取会话消息历史"""
    root = _get_project_root()
    mgr = MemoryManager(project_root=root, session_id=session_id)

    if session_id not in mgr.list_all_sessions():
        raise HTTPException(status_code=404, detail="会话不存在")

    messages = []
    days = mgr.list_days(limit=100)

    for day in days:
        day_content = mgr.read_day(day)
        for line in day_content.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("Q:"):
                messages.append({"role": "user", "content": line[2:].strip()})
            elif line.startswith("A:"):
                messages.append({"role": "assistant", "content": line[2:].strip()})

    return {
        "session_id": session_id,
        "messages": messages,
        "days": days,
    }


@router.post("/sessions")
async def create_session():
    """创建新会话（返回新 session_id，实际创建由前端 WebSocket 触发）"""
    from core.session_service import SessionService

    svc = SessionService(Path.cwd())
    return {
        "session_id": svc.session_id,
        "message": "会话已创建，请通过 WebSocket 连接使用",
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    root = _get_project_root()
    mgr = MemoryManager(project_root=root)

    if session_id not in mgr.list_all_sessions():
        raise HTTPException(status_code=404, detail="会话不存在")

    ok = MemoryManager(project_root=root).delete_session(session_id)
    if ok:
        return {"message": f"已删除: {session_id}"}
    else:
        raise HTTPException(status_code=500, detail=f"删除失败: {session_id}")


@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}
