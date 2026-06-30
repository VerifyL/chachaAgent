#!/usr/bin/env python3
"""
scripts/init_project.py
初始化 ChachaAgent 项目运行时目录与配置文件
"""

import os
import sys
import argparse
import shutil
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="初始化 ChachaAgent 项目")
    parser.add_argument("-p", "--project-id", default=os.environ.get("PROJECT_ID", "default"), help="项目标识符")
    parser.add_argument("-f", "--force", action="store_true", help="强制覆盖已存在的文件/目录")
    args = parser.parse_args()

    project_id = args.project_id
    force = args.force

    print(f"ChachaAgent 项目初始化")
    print(f"项目ID: {project_id}")
    print(f"强制模式: {force}\n")

    runtime_dir = Path(".chacha")
    subdirs = [
        "checkpoints",
        f"memory/projects/{project_id}/memory",
        f"memory/projects/{project_id}/topics",
        "rag_store",
        "logs",
    ]

    for sub in subdirs:
        path = runtime_dir / sub
        if path.exists():
            if force:
                print(f"强制重建目录: {path}")
                shutil.rmtree(path)
                path.mkdir(parents=True)
            else:
                print(f"目录已存在，跳过: {path}")
        else:
            path.mkdir(parents=True)
            print(f"创建目录: {path}")

    # MEMORY.md 由 autoDream 管道异步生成，不创建占位文件
    print(f"记忆目录已准备: memory/projects/{project_id}/memory")

    config_file = Path("chachaConfig.toml")
    examples_dir = Path("examples")
    if not config_file.exists() or force:
        if (examples_dir / "chachaConfig.toml").exists():
            shutil.copy(examples_dir / "chachaConfig.toml", config_file)
            print(f"从 {examples_dir}/chachaConfig.toml 复制配置文件")
        else:
            config_file.write_text(f"""# ChachaAgent 配置文件
# 详细说明请参考 docs/configuration.md

project_id = "{project_id}"
environment = "dev"

[model]
[model.providers.default]
provider = "openai"
api_key = ""   # 请在此设置您的 API Key 或通过环境变量
default_model = "gpt-4"

[context]
max_tokens = 128000

[memory]
prune_days = 30                  # 每日记忆文件保留天数
max_memory_lines = 200           # MEMORY.md 索引最大条目数

[sandbox]
allowed_commands = ["ls", "cat", "grep", "python", "pytest", "git"]

[policy]
cost_limit_dollars = 10.0

[telemetry]
log_level = "INFO"

[multimodal]   # v1.5 预留
enabled = false

[interface]
cli_theme = "default"
web_enabled = false
""")
            print(f"生成默认配置文件: {config_file}")
    else:
        print(f"配置文件已存在，跳过: {config_file}")

    # 设置权限（仅 owner 可读写执行）
    runtime_dir.chmod(0o700)
    print("\n初始化完成！")
    print("您现在可以运行 'chacha' 或 'python main.py' 启动 Agent。")
    print(f"如需调整配置，请编辑 {config_file}。")

if __name__ == "__main__":
    main()