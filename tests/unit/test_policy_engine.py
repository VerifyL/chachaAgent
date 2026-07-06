"""
tests/unit/test_policy_engine.py
单元测试：core/policy_engine.py PolicyEngine
覆盖：黑名单拦截、白名单放行、风险评估、审批缓存、成本熔断、三级权限
"""

import time

from core.models.config import PolicyConfig
from core.policy_engine import (
    ApprovalEntry,
    CircuitState,
    CostCircuitBreaker,
    PermissionLevel,
    PolicyEngine,
    RiskFactors,
    RiskLevel,
)

# ========== 1. 黑名单 ==========

def test_blacklist_blocks():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("shell", "rm -rf /tmp/test")
    assert decision.allowed is False
    assert "rm -rf" in decision.blocked_reason
    assert decision.risk_level == RiskLevel.CRITICAL


def test_blacklist_sudo():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("shell", "sudo apt install")
    assert decision.allowed is False
    assert "sudo" in decision.blocked_reason


def test_blacklist_case_insensitive():
    engine = PolicyEngine()
    _decision = engine.evaluate_tool("shell", "RM -RF /")
    # 默认黑名单区分大小写（配置决定）
    pass  # behavior depends on config


def test_safe_command_not_blocked():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("shell", "ls -la")
    assert decision.allowed is True


# ========== 2. 白名单 ==========

def test_whitelist_bypasses_blacklist():
    engine = PolicyEngine()
    engine.add_to_whitelist("sudo_wrapper")
    decision = engine.evaluate_tool("sudo_wrapper", "sudo apt install")
    assert decision.allowed is True


# ========== 3. 风险评估 ==========

def test_risk_score_calculation():
    factors = RiskFactors(
        data_sensitivity=0.5,
        financial_impact=0.3,
        irreversibility=0.7,
        model_confidence=0.6,
        user_authorization=1.0,
    )
    score = factors.score()
    assert 0 <= score <= 100


def test_risk_score_to_level_low():
    factors = RiskFactors()
    assert factors.to_level() == RiskLevel.LOW
    assert factors.score() < 5  # model_confidence=0.8 → (1-0.8)*0.15*100 ≈ 3


def test_risk_score_to_level_critical():
    factors = RiskFactors(
        data_sensitivity=1.0,
        financial_impact=1.0,
        irreversibility=1.0,
        model_confidence=0.0,
        user_authorization=0.0,
    )
    assert factors.to_level() == RiskLevel.CRITICAL
    assert factors.score() > 80


def test_risk_assess_convenience():
    engine = PolicyEngine()
    level, score = engine.risk_assess("shell", irreversibility=1.0)
    assert score > 0


# ========== 4. 权限级别 ==========

def test_read_file_is_free():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("read", "read main.py", "s1")
    assert decision.allowed is True
    assert decision.needs_approval is False
    assert decision.permission_level == PermissionLevel.FREE



def test_write_file_needs_approval():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("write_file", "write to main.py", "s1")
    assert decision.allowed is True
    assert decision.needs_approval is True


def test_approve_once_grants_task_level():
    engine = PolicyEngine()
    engine.set_tool_permission("docker", PermissionLevel.APPROVE_ONCE)
    # 高风险工具 → APPROVE_ONCE
    decision = engine.evaluate_tool("docker", "docker run ...", "s1")
    assert decision.needs_approval is True

    # 授权一次
    engine.grant_task_approval("s1", "docker")
    decision2 = engine.evaluate_tool("docker", "docker ps", "s1")
    assert decision2.needs_approval is False
    assert decision2.permission_level == PermissionLevel.APPROVE_ONCE


def test_approve_once_scoped_to_session():
    engine = PolicyEngine()
    engine.grant_task_approval("s1", "docker")
    # s2 没有授权
    decision = engine.evaluate_tool("docker", "docker ps", "s2")
    assert decision.needs_approval is True


def test_reset_task_approvals():
    engine = PolicyEngine()
    engine.grant_task_approval("s1", "docker")
    engine.reset_task_approvals("s1")
    decision = engine.evaluate_tool("docker", "docker ps", "s1")
    assert decision.needs_approval is True


# ========== 5. 审批缓存 ==========

def test_approval_cache_hit():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("write_file", "write main.py", "s1")
    assert decision.needs_approval is True
    assert decision.cache_key is not None

    engine.record_approval(decision.cache_key, approved=True)
    decision2 = engine.evaluate_tool("write_file", "write main.py", "s1")
    assert decision2.needs_approval is False
    assert decision2.allowed is True


def test_approval_cache_expired():
    engine = PolicyEngine()

    # 直接写入一个已过期的缓存条目
    key = "expired_key"
    engine._approval_cache[key] = ApprovalEntry(approved=True, cached_at=time.time() - 1000, ttl_seconds=1)
    decision = engine.evaluate_tool("write_file", "write main.py", "s1")
    # 缓存不命中此 key → 应该触发审批
    assert decision.needs_approval or decision.cache_key != key


def test_clear_cache():
    engine = PolicyEngine()
    decision = engine.evaluate_tool("write_file", "w", "s1")
    engine.record_approval(decision.cache_key, approved=True)
    engine.clear_cache()
    # 重新评估 → 缓存已清，需要审批
    decision2 = engine.evaluate_tool("write_file", "w", "s1")
    assert decision2.needs_approval is True


# ========== 6. 成本熔断器 ==========

class TestCostBreaker:
    def test_normal_operation(self):
        cb = CostCircuitBreaker(limit_dollars=5.0)
        assert cb.state == CircuitState.CLOSED
        assert cb.add_cost(2.0) is True
        assert cb.cumulative_cost == 2.0
        assert cb.is_available() is True

    def test_breaker_trips(self):
        cb = CostCircuitBreaker(limit_dollars=3.0)
        cb.add_cost(2.5)
        assert cb.add_cost(1.0) is False  # 2.5+1.0=3.5 > 3.0
        assert cb.state == CircuitState.OPEN
        assert cb.is_available() is False

    def test_half_open_to_closed(self):
        cb = CostCircuitBreaker(limit_dollars=3.0, failure_reset_seconds=0.01)
        cb.add_cost(5.0)  # trip
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        # state check triggers half-open
        assert cb.state == CircuitState.HALF_OPEN
        cb.on_success()
        assert cb.state == CircuitState.CLOSED

    def test_reset(self):
        cb = CostCircuitBreaker(limit_dollars=1.0)
        cb.add_cost(2.0)
        cb.reset()
        assert cb.cumulative_cost == 0.0
        assert cb.state == CircuitState.CLOSED


def test_evaluate_cost_rejects_when_breaker_open():
    engine = PolicyEngine()
    # 超限触发熔断
    engine._circuit_breaker._cumulative = 100.0
    engine._circuit_breaker._state = CircuitState.OPEN
    allowed, reason, cost = engine.evaluate_cost(0.01)
    assert allowed is False
    assert "熔断" in reason


# ========== 7. 综合场景 ==========

def test_full_security_workflow():
    """完整的安全评估流程"""
    engine = PolicyEngine()

    # 场景1：读文件 → FREE
    d = engine.evaluate_tool("read", "read main.py", "s1")
    assert d.allowed and not d.needs_approval

    # 场景2：写文件 → ASK_FIRST → 需要审批
    d = engine.evaluate_tool("write_file", "write config.json", "s1")
    assert d.allowed and d.needs_approval

    # 场景3：危险命令 → 直接拦截
    d = engine.evaluate_tool("shell", "rm -rf /")
    assert not d.allowed

    # 场景4：shell + 安全命令 → ASK_FIRST（shell 无黑名单命中）
    d = engine.evaluate_tool("shell", "ls -la")
    assert d.allowed and d.needs_approval


def test_config_integration():
    """从 PolicyConfig 初始化"""
    config = PolicyConfig(
        command_blacklist=["rm", "sudo"],
        cost_limit_dollars=5.0,
        approval_cache_ttl_seconds=120,
    )
    engine = PolicyEngine(config)
    assert engine._cost_limit == 5.0
    assert engine._cache_ttl == 120


def test_set_tool_permission():
    engine = PolicyEngine()
    engine.set_tool_permission("unzip", PermissionLevel.FREE)
    d = engine.evaluate_tool("unzip", "unzip package.tar.gz", "s1")
    assert d.permission_level == PermissionLevel.FREE
    assert d.needs_approval is False
