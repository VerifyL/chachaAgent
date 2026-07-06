"""
core/cli_theme.py
CLI 主题配置 — 类似 vimrc 的可自定义配色。
加载 ~/.chacha/clirc.toml，未配置项使用默认值。
"""

import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# ====== 默认主题（色弱友好） ======

DEFAULT_THEME: Dict[str, str] = {
    "user_border": "bold yellow",
    "user_text": "bold yellow",
    "user_title": "bold reverse yellow",
    "agent_header": "bold cyan",
    "tool_thinking": "bold cyan",
    "tool_done": "bold bright_white",
    "help_cmd": "bold yellow",
    "help_desc": "yellow",
    "help_title": "bold reverse bright_white",
    "separator": "dim",
    "audit": "dim",
    "tool_error": "bold red",
    "system": "dim",
    "prompt": "bold",
}


def load_theme() -> Dict[str, str]:
    """加载主题：~/.chacha/clirc.toml → 合并默认值。"""
    theme = dict(DEFAULT_THEME)
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    path = Path.home() / ".chacha" / "clirc.toml"
    if not path.exists():
        return theme

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        user_theme = data.get("theme", {})
        for k, v in user_theme.items():
            if k in theme:
                theme[k] = str(v)
    except Exception as e:
        logger.warning("加载 clirc.toml 失败: %s", e)

    return theme


def write_default_theme() -> None:
    """首次运行时写出默认 clirc.toml 模板。"""
    path = Path.home() / ".chacha" / "clirc.toml"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    content = """# Chacha CLI 主题配置 (类似 vimrc)
# 修改配色后重启生效。删除此文件恢复默认。

[theme]
# 用户输入 Panel
user_border = "bold yellow"
user_text = "bold yellow"
user_title = "bold reverse yellow"

# Agent 回复
agent_header = "bold cyan"

# 工具调用
tool_thinking = "bold cyan"
tool_done = "bold bright_white"
tool_error = "bold red"

# 帮助
help_title = "bold reverse bright_white"
help_cmd = "bold yellow"
help_desc = "yellow"

# 其他
separator = "dim"
audit = "dim"
system = "dim"
prompt = "bold"

# 上下文压缩阈值 (token 数，亦可设环境变量 COMPACT_AT / WARN_AT)
# DeepSeek v4 1M 上下文可设高些, GPT-4o 128K 设低些
# compact_at = 80000    # 超过此值自动压缩
# warn_at = 500000      # 超过此值黄色警告

# 可用样式: bold, italic, underline, reverse
# 可用颜色: black, red, green, yellow, blue, magenta, cyan, white
#           加 bright_ 前缀: bright_white, bright_yellow ...
"""
    path.write_text(content, encoding="utf-8")
