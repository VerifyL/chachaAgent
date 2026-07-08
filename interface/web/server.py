"""
interface/web/server.py
FastAPI 服务入口 — WebSocket + REST API + 静态文件托管。

启动方式:
    chacha web                      # 默认 0.0.0.0:8100
    chacha web --port 3000          # 自定义端口
    chacha web --host 127.0.0.1     # 仅本地访问
    python -m interface.web.server  # 直接运行
"""

import logging
import os
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from interface.web.routes import chat, sessions
from interface.web.web_bridge import WebBridge

logger = logging.getLogger(__name__)

# ── 静态文件目录 ──
# 优先使用 React 构建产物，回退到轻量前端
_FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"
_LITE_STATIC = Path(__file__).parent / "static"
STATIC_DIR = _FRONTEND_DIST if _FRONTEND_DIST.exists() else _LITE_STATIC

# ── 全局 bridge 引用（lifespan 中初始化） ──
_bridge: WebBridge | None = None


def get_bridge() -> WebBridge:
    """获取全局 bridge 实例"""
    if _bridge is None:
        raise RuntimeError("Bridge 未初始化，请先启动服务")
    return _bridge


# ── 应用工厂 ──


def _setup_static_files(app: FastAPI, static_dir: Path) -> None:
    """设置静态文件托管 + SPA fallback。

    不使用 app.mount("/", StaticFiles) 因为它会拦截 WebSocket 请求，
    导致 StaticFiles 收到 websocket scope 后崩溃。
    """
    # 1) 挂载 assets 子目录（Vite 构建的 JS/CSS chunks）
    assets_dir = static_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # 2) Catch-all GET route — 服务根文件（favicon 等）+ SPA fallback
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = static_dir / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(static_dir / "index.html")


def create_app(project_root: Path | None = None) -> FastAPI:
    """创建 FastAPI 应用"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期：启动时初始化 bridge，关闭时清理"""
        global _bridge
        root = project_root or Path.cwd()
        logger.info(f"[web] 启动服务, project_root={root}")

        _bridge = WebBridge(project_root=root)
        await _bridge.initialize()

        # 挂载到 app.state 供路由访问
        app.state.bridge = _bridge
        app.state.project_root = root

        logger.info(f"[web] bridge 就绪, model={_bridge.model}")

        yield

        # 关闭
        logger.info("[web] 正在关闭...")
        if _bridge:
            await _bridge.shutdown()
        _bridge = None

    try:
        _version = pkg_version("chachaAgent")
    except (PackageNotFoundError, ModuleNotFoundError):
        _version = "dev"

    app = FastAPI(
        title="ChachaAgent Web",
        description="ChachaAgent Web 服务 — 流式 AI 对话",
        version=_version,
        lifespan=lifespan,
    )

    # CORS — 允许前端跨域
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由 — 必须在静态文件处理之前注册
    app.include_router(chat.router)
    app.include_router(sessions.router)

    # 静态文件托管（React 构建产物 / 轻量前端）
    # 注意：不能用 app.mount("/", ...) 因为会拦截 WebSocket 请求导致 StaticFiles 崩溃
    if STATIC_DIR.exists():
        _setup_static_files(app, STATIC_DIR)
        logger.info(f"[web] 静态文件托管: {STATIC_DIR}")

    return app


# ── 直接启动入口 ──


def main():
    """直接运行入口"""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="ChachaAgent Web Server")
    parser.add_argument("project", nargs="?", default=".", help="项目路径")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8100, help="端口号 (默认 8100)")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        print(f"错误: 项目路径不存在: {project_root}")
        return

    # 设置环境变量供子模块使用
    os.chdir(str(project_root))

    app = create_app(project_root=project_root)

    # 检查前端构建状态
    frontend_dist = Path(__file__).parent / "frontend" / "dist"
    has_frontend = frontend_dist.exists() and any(frontend_dist.iterdir())

    print("\n  ChachaAgent Web 服务")
    print(f"  地址: http://{args.host}:{args.port}")
    print(f"  项目: {project_root}")
    print(f"  API:  http://{args.host}:{args.port}/api/sessions")
    print(f"  WS:   ws://{args.host}:{args.port}/api/ws/chat")

    if not has_frontend:
        print()
        print("  ⚠ 前端尚未构建，直接访问将无法看到页面。")
        print("  开发模式（热更新，推荐）：")
        print("    cd interface/web/frontend && npm run dev")
        print("    然后浏览器打开 http://localhost:5173")
        print()
        print("  生产模式（构建一次即可）：")
        print("    cd interface/web/frontend && npm run build")
    else:
        print(f"  ✓ 前端已构建: {frontend_dist}")

    print()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
