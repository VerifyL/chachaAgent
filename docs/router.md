# 模型路由器

`core/llm_clients/router.py` — 🚧 骨架占位，待实现。

当前 AgentBridge 直接实例化单模型客户端，路由策略尚未使用。

## 预留路由策略

| 策略 | 行为 | 状态 |
|------|------|------|
| priority | 按 fallback_chain 顺序，返回第一个可用 | 🚧 |
| cost | 按 cost_per_1k_input 升序 | 🚧 |
| random | 随机选 | 🚧 |

ModelRouter 尚未集成到 AgentBridge 初始化流程。
