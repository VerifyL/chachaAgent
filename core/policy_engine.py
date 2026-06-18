"""
core/policy_engine.py
PolicyEngine — 安全策略引擎：命令拦截、风险评估、成本熔断、审批缓存。

设计理念（融合 权限模式 + 加权风险评估）：
1. 三级工具管控：黑名单(绝对拦截) > 白名单(自由通行) > 风险评估(按需审批)
2. 加权风险模型（Harness）：数据敏感度、财务影响、不可逆性、置信度、用户授权
3. 成本熔断器（Harness CircuitBreaker）：closed→open→half-open 三态
4. 审批缓存 TTL：相同 session+tool 的审批结果缓存 N 秒，减少重复询问
5. Claude Code 三级权限：Free(跳过) / AskFirst(每次问) / ApproveOnce(任务级一次授权)
"""

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from core.models.config import PolicyConfig


# ========================= 枚举 =========================

class RiskLevel(str, Enum):
    """风险等级（参考 Harness RiskEvaluator）"""
    LOW = "low"          # 读取类操作
    MEDIUM = "medium"    # 写入类操作
    HIGH = "high"        # 修改系统配置
    CRITICAL = "critical"  # 破坏性操作


class PermissionLevel(str, Enum):
    """权限级别（参考 Claude Code + Harness PermissionLevel）"""
    FREE = "free"              # 无需审批
    ASK_FIRST = "ask_first"    # 每次执行前询问
    APPROVE_ONCE = "approve_once"  # 任务级一次性授权（缓存至会话结束）


class ApprovalStatus(str, Enum):
    """审批状态"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CACHED = "cached"  # 命中审批缓存


class CircuitState(str, Enum):
    """熔断器状态（参考 Harness CircuitBreaker）"""
    CLOSED = "closed"      # 正常
    OPEN = "open"          # 熔断
    HALF_OPEN = "half_open"  # 半开（尝试恢复）


# ========================= 风险评估因子 =========================

@dataclass
class RiskFactors:
    """风险评估加权因子（参考 Harness RiskEvaluator）"""
    data_sensitivity: float = 0.0    # 是否访问敏感数据（0~1）
    financial_impact: float = 0.0    # 是否产生费用（0~1）
    irreversibility: float = 0.0     # 是否不可逆（0~1）
    model_confidence: float = 0.8    # 模型置信度（0~1，越高越确定）
    user_authorization: float = 1.0  # 用户授权级别（0~1）

    # 权重（可配置）
    weights: Tuple[float, ...] = (0.3, 0.25, 0.2, 0.15, 0.1)

    def score(self) -> float:
        """计算风险分数（0~100）"""
        values = (
            self.data_sensitivity,
            self.financial_impact,
            self.irreversibility,
            max(0, 1.0 - self.model_confidence),
            max(0, 1.0 - self.user_authorization),
        )
        raw = sum(v * w for v, w in zip(values, self.weights))
        return min(100.0, raw * 100)

    def to_level(self) -> RiskLevel:
        s = self.score()
        if s < 20: return RiskLevel.LOW
        if s < 50: return RiskLevel.MEDIUM
        if s < 80: return RiskLevel.HIGH
        return RiskLevel.CRITICAL


# ========================= 策略决策 =========================

@dataclass
class PolicyDecision:
    """策略评估结果"""
    allowed: bool = True
    needs_approval: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    risk_score: float = 0.0
    blocked_reason: Optional[str] = None
    permission_level: PermissionLevel = PermissionLevel.FREE
    cache_key: Optional[str] = None  # 审批缓存键


# ========================= 审批条目 =========================

@dataclass
class ApprovalEntry:
    """审批缓存条目"""
    approved: bool
    cached_at: float = field(default_factory=time.time)
    ttl_seconds: int = 300


# ========================= 成本熔断器 =========================

class CostCircuitBreaker:
    """成本熔断器（参考 Harness CircuitBreaker 三态模型）"""

    def __init__(self, limit_dollars: float = 10.0, failure_reset_seconds: float = 60.0):
        self.limit = limit_dollars
        self._cumulative: float = 0.0
        self._state = CircuitState.CLOSED
        self._state_changed_at: float = time.time()
        self._reset_seconds = failure_reset_seconds

    @property
    def state(self) -> CircuitState:
        # half-open 超时自动恢复
        if self._state == CircuitState.OPEN:
            if time.time() - self._state_changed_at > self._reset_seconds:
                self._state = CircuitState.HALF_OPEN
                self._state_changed_at = time.time()
        return self._state

    @property
    def cumulative_cost(self) -> float:
        return self._cumulative

    def add_cost(self, cost: float) -> bool:
        """记录成本。返回 True=继续，False=已熔断。"""
        self._cumulative += cost

        if self._cumulative > self.limit:
            self._state = CircuitState.OPEN
            self._state_changed_at = time.time()
            return False
        return True

    def is_available(self) -> bool:
        """是否可以发送新请求"""
        return self.state != CircuitState.OPEN

    def on_success(self) -> None:
        """请求成功 → half-open 恢复到 closed"""
        if self.state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._state_changed_at = time.time()

    def reset(self) -> None:
        self._cumulative = 0.0
        self._state = CircuitState.CLOSED


# ========================= 策略引擎 =========================

class PolicyEngine:
    """
    安全策略引擎。

    评估流程：
      1. 命令黑名单检查 → 直接拦截
      2. 工具白名单检查 → 直接放行
      3. 风险评估（加权因子模型）→ 决定是否需要审批
      4. 审批缓存检查 → 命中缓存跳过审批
      5. 成本熔断器 → 超限禁止 LLM 调用

    用法:
        engine = PolicyEngine(PolicyConfig())
        decision = engine.evaluate_tool("shell", "rm -rf /tmp", risk_factors=...)
        if not decision.allowed:
            return  # 被拦截
        if decision.needs_approval:
            await ask_user(...)
    """

    def __init__(self, config: Optional[PolicyConfig] = None, telemetry: Optional[Any] = None):
        # 从配置初始化，未提供则用默认值
        cfg = config or PolicyConfig()
        self._telemetry = telemetry
        self._command_blacklist = set(cfg.command_blacklist)
        self._command_whitelist: Set[str] = set()
        self._cost_limit = cfg.cost_limit_dollars
        self._cache_ttl = cfg.approval_cache_ttl_seconds

        # 熔断器
        self._circuit_breaker = CostCircuitBreaker(limit_dollars=self._cost_limit)

        # 审批缓存：cache_key → ApprovalEntry
        self._approval_cache: Dict[str, ApprovalEntry] = {}

        # 任务级授权标记（APPROVE_ONCE 模式）：
        # 某个 session+tool 被授权一次后，后续不再询问
        self._task_approvals: Set[str] = set()  # "session_id::tool_name"

        # 工具→权限级别映射
        self._tool_permissions: Dict[str, PermissionLevel] = {}
        self._init_default_permissions()

    # ====== 默认权限映射 ======

    def _init_default_permissions(self) -> None:
        """Claude Code 风格的默认工具权限级别"""
        # Free：只读类
        self._tool_permissions.update({
            t: PermissionLevel.FREE
            for t in ["read_file", "grep", "ls", "cat", "head", "tail", "echo", "pwd"]
        })
        # AskFirst：修改类
        self._tool_permissions.update({
            t: PermissionLevel.ASK_FIRST
            for t in ["write_file", "patch", "rm", "mv", "cp", "chmod", "chown"]
        })
        # ApproveOnce：高风险类
        self._tool_permissions.update({
            t: PermissionLevel.APPROVE_ONCE
            for t in ["shell", "exec", "pip", "npm", "docker", "kubectl"]
        })

    # ====== 公开接口 ======

    def _emit_metric(self, tool_name: str, status: str) -> None:
        if self._telemetry:
            self._telemetry.metrics.inc(
                "chacha_policy_decisions_total",
                tags={"tool": tool_name, "status": status},
            )

    def evaluate_tool(
        self,
        tool_name: str,
        command_or_action: str = "",
        session_id: str = "",
        risk_factors: Optional[RiskFactors] = None,
    ) -> PolicyDecision:
        # 1. 工具白名单（显式放行，覆盖后续所有检查）
        if tool_name in self._command_whitelist:
            self._emit_metric(tool_name, "whitelisted")
            return PolicyDecision(allowed=True, permission_level=PermissionLevel.FREE)

        # 2. 命令黑名单（绝对拦截）
        reason = self._check_blacklist(command_or_action)
        if reason:
            self._emit_metric(tool_name, "blocked")
            return PolicyDecision(
                allowed=False,
                risk_level=RiskLevel.CRITICAL,
                risk_score=100.0,
                blocked_reason=reason,
            )

        # 3. 权限级别
        perm = self._tool_permissions.get(tool_name, PermissionLevel.ASK_FIRST)

        # 4. FREE → 直接放行
        if perm == PermissionLevel.FREE:
            return PolicyDecision(allowed=True, permission_level=PermissionLevel.FREE)

        # 5. APPROVE_ONCE → 检查是否已授权
        if perm == PermissionLevel.APPROVE_ONCE:
            task_key = f"{session_id}::{tool_name}"
            if task_key in self._task_approvals:
                return PolicyDecision(
                    allowed=True,
                    permission_level=PermissionLevel.APPROVE_ONCE,
                    cache_key=task_key,
                )

        # 6. 风险评估
        if risk_factors is None:
            risk_factors = RiskFactors()
        score = risk_factors.score()
        level = risk_factors.to_level()

        # 7. 审批缓存检查
        cache_key = f"{session_id}:{tool_name}:{command_or_action[:50]}"
        hashed = hashlib.sha256(cache_key.encode()).hexdigest()[:16]
        entry = self._approval_cache.get(hashed)
        if entry:
            elapsed = time.time() - entry.cached_at
            if elapsed < entry.ttl_seconds:
                if entry.approved:
                    return PolicyDecision(
                        allowed=True,
                        risk_level=level,
                        risk_score=score,
                        permission_level=PermissionLevel.ASK_FIRST,
                        cache_key=hashed,
                    )

        # 8. ASK_FIRST → 总是需要审批（写操作默认不可信）
        if perm == PermissionLevel.ASK_FIRST:
            return PolicyDecision(
                allowed=True,
                needs_approval=True,
                risk_level=level,
                risk_score=score,
                permission_level=PermissionLevel.ASK_FIRST,
                cache_key=hashed,
            )

        # 9. APPROVE_ONCE + 未授权 + 需要审批
        if perm == PermissionLevel.APPROVE_ONCE:
            return PolicyDecision(
                allowed=True,
                needs_approval=True,
                risk_level=level,
                risk_score=score,
                permission_level=PermissionLevel.APPROVE_ONCE,
            )

        return PolicyDecision(allowed=True)

    def evaluate_cost(self, cost: float) -> Tuple[bool, Optional[str], float]:
        """评估成本：是否可以继续调用 LLM。

        返回 (allowed, reason, cumulative_cost)。
        """
        if not self._circuit_breaker.is_available():
            return False, f"成本熔断: 累计 {self._circuit_breaker.cumulative_cost:.2f} > {self._cost_limit:.2f}", self._circuit_breaker.cumulative_cost

        self._circuit_breaker.add_cost(cost)
        return True, None, self._circuit_breaker.cumulative_cost

    def record_approval(self, cache_key: Optional[str], approved: bool) -> None:
        """记录审批结果（缓存 + 任务级授权）"""
        if cache_key and self._cache_ttl > 0:
            self._approval_cache[cache_key] = ApprovalEntry(
                approved=approved, ttl_seconds=self._cache_ttl,
            )

    def grant_task_approval(self, session_id: str, tool_name: str) -> None:
        """授予任务级一次性授权（APPROVE_ONCE 模式）"""
        self._task_approvals.add(f"{session_id}::{tool_name}")

    def reset_task_approvals(self, session_id: str) -> None:
        """重置会话的任务级授权"""
        to_remove = [k for k in self._task_approvals if k.startswith(session_id + "::")]
        for k in to_remove:
            self._task_approvals.discard(k)

    def reset_cost(self) -> None:
        """重置成本计数器"""
        self._circuit_breaker.reset()

    # ====== 内部 ======

    def _check_blacklist(self, command: str) -> Optional[str]:
        """检查命令是否命中黑名单"""
        for pattern in self._command_blacklist:
            if pattern in command:
                return f"命令命中黑名单: {pattern}"
        return None

    # ====== 查询 ======

    @property
    def circuit_state(self) -> CircuitState:
        return self._circuit_breaker.state

    @property
    def cumulative_cost(self) -> float:
        return self._circuit_breaker.cumulative_cost

    def risk_assess(
        self,
        tool_name: str,
        data_sensitivity: float = 0.0,
        financial_impact: float = 0.0,
        irreversibility: float = 0.0,
        model_confidence: float = 0.8,
    ) -> Tuple[RiskLevel, float]:
        """便捷方法：快速风险评估"""
        factors = RiskFactors(
            data_sensitivity=data_sensitivity,
            financial_impact=financial_impact,
            irreversibility=irreversibility,
            model_confidence=model_confidence,
        )
        return factors.to_level(), factors.score()

    def add_to_whitelist(self, tool_name: str) -> None:
        self._command_whitelist.add(tool_name)

    def set_tool_permission(self, tool_name: str, level: PermissionLevel) -> None:
        self._tool_permissions[tool_name] = level

    def clear_cache(self) -> None:
        self._approval_cache.clear()
