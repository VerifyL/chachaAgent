"""core/subagent/ — 子Agent 孵化与管理（参考 sub-agent 设计）"""

from core.subagent.definitions import SUBAGENT_DEFINITIONS, SubAgentDef
from core.subagent.spawner import SubAgentResult, SubAgentSpawner

__all__ = ["SubAgentSpawner", "SubAgentResult", "SubAgentDef", "SUBAGENT_DEFINITIONS"]
