# 安全策略引擎 (`core/policy_engine.py`)

本文档说明 `PolicyEngine` 的工具管控、风险评估、审批缓存和成本熔断机制。策略引擎位于 ToolExecutor 之前，是 Harness 安全防线的核心。

## 概述

设计融合了 **Claude Code 权限模式 + Harness 加权风险评估**：

- **三级工具管控**：白名单(显式放行) > 黑名单(绝对拦截) > 风险评估(按需审批)
- **加权风险模型**：数据敏感度、财务影响、不可逆性、置信度、用户授权五个维度的加权评分
- **三级权限**：FREE(读操作) / ASK_FIRST(写操作每次问) / APPROVE_ONCE(高风险工具任务级授权)
- **审批缓存 TTL**：相同 session+tool 的审批结果缓存 N 秒，减少重复询问
- **成本熔断器**：累计成本超限后禁止 LLM 调用，支持 closed→open→half-open 三态恢复

### 评估流程

```
ToolExecutor 准备执行工具
  │
  ├─ PolicyEngine.evaluate_tool(tool_name, command, session_id, risk_factors)
  │
  ├─ 1. 白名单检查
  │     tool_name 在白名单 → 直接放行（return）
  │
  ├─ 2. 黑名单检查
  │     command 命中模式 → 拦截（allowed=False, risk_level=CRITICAL）
  │
  ├─ 3. 权限级别
  │     FREE → 直接放行
  │     APPROVE_ONCE → 检查任务级授权标记
  │     ASK_FIRST → 继续评估
  │
  ├─ 4. 风险评估（加权因子模型）
  │     RiskFactors → score(0-100) → RiskLevel(LOW/MEDIUM/HIGH/CRITICAL)
  │
  ├─ 5. 审批缓存
  │     cache_key = SHA256(session:tool:command)
  │     缓存命中 + 未过期 → 用缓存结果
  │     缓存未命中 → needs_approval=True
  │
  └─ 返回 PolicyDecision(allowed, needs_approval, risk_level, risk_score, cache_key)
```

---

## 1. 三级权限模型

```python
class PermissionLevel(str, Enum):
    FREE = "free"              # 无需审批
    ASK_FIRST = "ask_first"    # 每次执行前询问
    APPROVE_ONCE = "approve_once"  # 任务级一次性授权
```

### 默认权限映射

| 权限 | 工具 | 原因 |
|------|------|------|
| **FREE** | read_file, grep, ls, cat, head, tail, echo, pwd | 只读，无副作用 |
| **ASK_FIRST** | write_file, patch, rm, mv, cp, chmod, chown | 修改文件系统 |
| **APPROVE_ONCE** | shell, exec, pip, npm, docker, kubectl | 执行外部命令，影响面大 |

### APPROVE_ONCE 工作原理

```
第1次调用 docker run → needs_approval=True → 用户确认
  └─ engine.grant_task_approval("s1", "docker")

第2次调用 docker ps → needs_approval=False（任务级已授权）
  └─ session 内后续所有 docker 调用都放行

会话结束 → engine.reset_task_approvals("s1")
```

---

## 2. 风险评估

### 2.1 加权因子

```python
RiskFactors(
    data_sensitivity: float = 0.0,    # 是否访问敏感数据（0~1）
    financial_impact: float = 0.0,    # 是否产生费用（0~1）
    irreversibility: float = 0.0,     # 是否不可逆（0~1）
    model_confidence: float = 0.8,    # 模型置信度（0~1，越高越确定）
    user_authorization: float = 1.0,  # 用户授权级别（0~1）
)
```

| 因子 | 权重 | 说明 |
|------|------|------|
| data_sensitivity | 0.30 | 操作是否涉及敏感数据路径（如 /etc、.env） |
| financial_impact | 0.25 | 是否可能产生额外费用（API 调用、资源创建） |
| irreversibility | 0.20 | 操作是否不可逆（删除、格式化、push --force） |
| model_confidence | 0.15 | 模型输出的置信度（低置信度 = 高风险） |
| user_authorization | 0.10 | 用户是否明确授权（如 ApproveOnce 已授权） |

### 2.2 分数→等级映射

| 分数 | 等级 | 处理 |
|------|------|------|
| 0~19 | `LOW` | ASK_FIRST 也放行 |
| 20~49 | `MEDIUM` | ASK_FIRST → 审批 |
| 50~79 | `HIGH` | ASK_FIRST → 审批，APPROVE_ONCE → 审批 |
| 80~100 | `CRITICAL` | 黑名单命中 → 直接拦截 |

---

## 3. 审批缓存

```python
decision = engine.evaluate_tool("write_file", "write config.json", "s1")
# → needs_approval=True, cache_key="a1b2c3d4..."

# 用户确认后记录
engine.record_approval(decision.cache_key, approved=True)

# 5 分钟内再次调用
decision2 = engine.evaluate_tool("write_file", "write config.json", "s1")
# → needs_approval=False（缓存命中）
```

**缓存键**：`SHA256(f"{session_id}:{tool_name}:{command[:50]}")`

**TTL**：可配置，默认 300 秒（通过 `PolicyConfig.approval_cache_ttl_seconds`）

---

## 4. 成本熔断器

### 4.1 三态模型

```
CLOSED ──(累计超限)──→ OPEN ──(60s后)──→ HALF_OPEN ──(请求成功)──→ CLOSED
                        │                    │
                        │  拒绝所有 LLM 请求    │ 允许一次试探
                        │                    │
                        └────────────────────┘
```

### 4.2 使用

```python
# 每次 LLM 调用前
allowed, reason, cumulative = engine.evaluate_cost(0.015)
if not allowed:
    return f"成本熔断: {reason}"  # → 前端提示

# 请求成功后
engine._circuit_breaker.on_success()  # HALF_OPEN → CLOSED
```

---

## 5. 使用示例

### 5.1 基本评估

```python
from core.policy_engine import PolicyEngine, RiskFactors

engine = PolicyEngine()

# 读文件 → FREE → 直接放行
d = engine.evaluate_tool("read_file", "read main.py", "s1")
assert d.allowed and not d.needs_approval

# 写文件 → ASK_FIRST → 每次审批
d = engine.evaluate_tool("write_file", "write config.json", "s1")
assert d.needs_approval
# 用户确认后
engine.record_approval(d.cache_key, approved=True)

# 危险命令 → 直接拦截
d = engine.evaluate_tool("shell", "rm -rf /", "s1")
assert not d.allowed  # 被黑名单拦截
```

### 5.2 自定义风险因子

```python
# 高风险操作（删除生产数据）
factors = RiskFactors(
    data_sensitivity=1.0,
    financial_impact=0.8,
    irreversibility=1.0,
    model_confidence=0.5,   # 模型不太确定
    user_authorization=0.0,  # 用户未授权
)
d = engine.evaluate_tool("shell", "DROP TABLE users", "s1", risk_factors=factors)
assert d.risk_level == RiskLevel.CRITICAL
```

### 5.3 成本控制

```python
# 每次 LLM 调用前
allowed, reason, cumulative = engine.evaluate_cost(0.015)
if not allowed:
    return f"成本熔断: 累计 {cumulative:.2f} 已超限"

# 会话结束时
engine.reset_cost()
```

### 5.4 自定义权限

```python
# 将某个 MCP 工具标记为 FREE
engine.set_tool_permission("mcp_read_only", PermissionLevel.FREE)

# 添加白名单（绕过黑名单）
engine.add_to_whitelist("trusted_admin_tool")

# 清除所有审批缓存
engine.clear_cache()
```

---

## 6. 与 Orchestrator 的集成

```python
# ToolExecutor 执行前
decision = policy_engine.evaluate_tool(
    tool_name=event.tool_name,
    command_or_action=event.command_or_action,
    session_id=session_id,
    risk_factors=assess_risk(event),  # Orchestrator 构建因子
)

if not decision.allowed:
    await send_blocked_notification(decision.blocked_reason)
    return

if decision.needs_approval:
    approved = await request_user_approval(decision)
    if not approved:
        return
    policy_engine.record_approval(decision.cache_key, approved)
```
