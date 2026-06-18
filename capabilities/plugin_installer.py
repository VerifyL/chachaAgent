"""
capabilities/plugin_installer.py
PluginInstaller — ClawHub 插件市场安装器骨架。

TODO(阶段7): 实现 ClawHub API 集成（搜索/安装/更新）
TODO(阶段7): 实现 Git 源码安装（git clone + pip install）
TODO(阶段7): 实现依赖解析与冲突检测
TODO(阶段7): 实现插件版本管理与 update
TODO(阶段7): 实现 manifest.json 校验与安全签名检查
TODO(阶段7): 实现插件隔离（每个插件独立 venv）

参考: OpenClaw ClawHub + Claude Code MCP marketplace
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PluginInstaller:
    """插件安装器骨架"""

    def __init__(self, plugins_dir: Optional[Path] = None):
        self._dir = plugins_dir or Path(".chacha_agent/skills")
        self._dir.mkdir(parents=True, exist_ok=True)

    async def install(self, name: str, source: str = "") -> bool:
        """从 ClawHub/git 安装插件。

        TODO(阶段7): ClawHub API → 下载 → 解压 → 校验 → 写入 _dir。

        source 格式:
          - "clawhub://user/plugin"  → ClawHub
          - "github://user/repo"     → GitHub
          - "git://url"              → Git URL
        """
        logger.warning("PluginInstaller.install() 尚未实现（阶段 7）")
        return False

    def uninstall(self, name: str) -> bool:
        """卸载插件（删除目录）。

        TODO(阶段7): 删除 _dir/name/，检查依赖关系。
        """
        return False

    def list_installed(self) -> List[str]:
        """列出已安装的插件名称。"""
        installed = []
        for d in self._dir.iterdir():
            if d.is_dir() and (d / "manifest.json").exists():
                installed.append(d.name)
        return installed

    @staticmethod
    def validate(plugin_dir: Path) -> bool:
        """校验插件 manifest.json 结构。

        TODO(阶段7): 完整 JSON Schema 校验 + 签名验证。
        """
        manifest = plugin_dir / "manifest.json"
        return manifest.exists()
