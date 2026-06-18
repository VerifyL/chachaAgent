"""
capabilities/openclaw_loader.py
SkillLoader — 6 级优先级技能加载器（参考 OpenClaw SkillPriority）。

TODO(阶段7): 实现 SYSTEM 级核心技能（echo/help/self-check）
TODO(阶段7): 实现 USER 级自定义技能（.chacha_agent/skills/user/ 目录扫描）
TODO(阶段7): 实现 DOMAIN 级领域技能（CHACHA.md 声明的项目技能）
TODO(阶段7): 实现 DISCOVERY 级 ClawHub 技能市场集成
TODO(阶段7): 实现技能 Schema 校验与版本管理
TODO(阶段7): 实现技能缓存与惰性加载
"""

import logging
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SkillPriority(IntEnum):
    """技能加载优先级（数字越小越优先）"""
    SYSTEM = 1       # 核心系统技能（echo/help）
    SESSION = 2      # 会话上下文注入
    USER = 3         # 用户自定义（.chacha_agent/skills/user/）
    DOMAIN = 4       # 领域技能（CHACHA.md 声明的项目技能）
    BUILTIN = 5      # 内置工具（BaseTool: read_file/shell）
    DISCOVERY = 6    # 动态发现（MCP/ClawHub）


class SkillLoader:
    """6 级优先级技能加载器骨架"""

    def __init__(self, skills_dir: Optional[Path] = None):
        self._dir = skills_dir or Path(".chacha_agent/skills")
        self._dir.mkdir(parents=True, exist_ok=True)

    async def load_all(self, session_id: str = "") -> List:
        """按优先级加载所有技能 → List[BaseTool]。

        TODO(阶段7): 实际实现 6 级加载逻辑。
        """
        tools: List = []
        tools.extend(await self._load_system())
        tools.extend(await self._load_session(session_id))
        tools.extend(await self._load_user())
        tools.extend(await self._load_domain(session_id))
        tools.extend(await self._load_builtin())
        tools.extend(await self._load_discovery(session_id))
        return tools

    # ====== 各级加载（阶段 7 实现） ======

    async def _load_system(self) -> List:
        """SYSTEM 级：核心系统技能（echo/help）。

        TODO(阶段7): 扫描 .chacha_agent/skills/system/ 目录。
        """
        return []

    async def _load_session(self, session_id: str) -> List:
        """SESSION 级：会话上下文注入的技能。

        TODO(阶段7): 从会话元数据中提取临时注入的技能。
        """
        return []

    async def _load_user(self) -> List:
        """USER 级：用户自定义技能。

        TODO(阶段7): 动态导入 .chacha_agent/skills/user/*.py → BaseTool。
        """
        return []

    async def _load_domain(self, session_id: str) -> List:
        """DOMAIN 级：CHACHA.md 中声明的项目相关技能。

        TODO(阶段7): 解析 CHACHA.md 中的 @skill 指令。
        """
        return []

    async def _load_builtin(self) -> List:
        """BUILTIN 级：内置 BaseTool。

        TODO(阶段7): 返回 ToolExecutor 中已注册的 BaseTool 列表。
        """
        return []

    async def _load_discovery(self, session_id: str) -> List:
        """DISCOVERY 级：MCP 动态发现 + ClawHub 技能市场。

        TODO(阶段7): MCPClient.get_tools() + ClawHub API 查询。
        """
        return []
