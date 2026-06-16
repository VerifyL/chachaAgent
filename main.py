#!/usr/bin/env python3
"""
main.py — ChachaAgent 统一入口。

用法:
    chacha run --mode cli                  # 启动 CLI 终端界面（Textual TUI）
    chacha run --mode web [--port 8080]    # 启动 Web 服务（FastAPI）
    chacha init [-p PROJECT_ID] [-f]       # 初始化项目目录和配置
    chacha config                          # 校验并打印当前配置
    chacha --version                       # 显示版本号
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

VERSION = "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chacha",
        description="ChaChaAgent — 通用 AI Agent 框架",
    )
    parser.add_argument(
        "--version", action="version", version=f"chacha {VERSION}"
    )

    sub = parser.add_subparsers(dest="command", title="子命令")

    # ---- run ----
    run_parser = sub.add_parser("run", help="启动 Agent")
    run_parser.add_argument(
        "--mode",
        choices=["cli", "web"],
        default="cli",
        help="运行模式 (默认: cli)",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Web 模式监听端口 (默认: 8080)",
    )
    run_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web 模式监听地址 (默认: 127.0.0.1)",
    )

    # ---- init ----
    init_parser = sub.add_parser("init", help="初始化项目目录和配置")
    init_parser.add_argument(
        "-p", "--project-id",
        default=None,
        help="项目标识符 (默认: 环境变量 PROJECT_ID 或 'default')",
    )
    init_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="强制覆盖已存在的目录和文件",
    )

    # ---- config ----
    config_parser = sub.add_parser("config", help="校验并打印当前配置")
    config_parser.add_argument(
        "--validate-only",
        action="store_true",
        help="仅校验配置有效性，不打印内容",
    )

    return parser


def cmd_run(args: argparse.Namespace) -> int:
    """处理 'run' 子命令"""
    if args.mode == "cli":
        print(f"[INFO] 启动 CLI 模式...")
        # TODO: 阶段 7 — from interface.cli.app import run; run()
        print("[WARN] CLI 模式尚未实现（阶段 7）")
        return 0
    elif args.mode == "web":
        print(f"[INFO] 启动 Web 模式 (http://{args.host}:{args.port})...")
        # TODO: 阶段 8 — from interface.web.server import run; run(host, port)
        print("[WARN] Web 模式尚未实现（阶段 8）")
        return 0
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """处理 'init' 子命令"""
    from scripts.init_project import main as init_main

    # 构造与 scripts/init_project.py 兼容的参数
    sys.argv = [
        "init_project.py",
    ]
    if args.project_id:
        sys.argv.extend(["-p", args.project_id])
    if args.force:
        sys.argv.append("-f")

    init_main()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """处理 'config' 子命令"""
    try:
        from core.config_manager import load_config
        config = load_config()
        if args.validate_only:
            print("[OK] 配置校验通过")
        else:
            print(config.model_dump_json(indent=2))
        return 0
    except Exception as e:
        print(f"[ERROR] 配置加载失败: {e}")
        return 1


def main(argv: list[str] | None = None) -> int:
    """程序主入口。

    返回 0 表示成功，非 0 表示失败。
    """
    parser = build_parser()

    # 无参数时打印 help
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # 路由到具体子命令
    if args.command == "run":
        return cmd_run(args)
    elif args.command == "init":
        return cmd_init(args)
    elif args.command == "config":
        return cmd_config(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
