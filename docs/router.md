# 模型路由器

`core/llm_clients/router.py` — ✅ 已实现。

支持三种路由策略 (priority/cost/random) + 故障隔离 (BAN_TTL 60s) + 降级链。

## 路由策略

| 策略 | 行为 | 状态 |
|------|------|------|
| priority | 按 fallback_chain 顺序，返回第一个可用 | ✅ |
| cost | 按 cost_per_1k_input 升序 | ✅ |
| random | 随机选 | ✅ |

已集成到 AgentBridge 初始化流程。
