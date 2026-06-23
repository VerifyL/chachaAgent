# ChaChaAgent

通用 AI Agent 框架 — 微内核架构，支持多模型、多工具、多前端。

## 项目简介

ChaChaAgent 是一个可扩展的通用 AI Agent 框架，提供从模型调用、上下文管理到工具执行的全链路编排。采用**微内核控制平面**设计，核心编排层与能力插件层解耦，支持 CLI（prompt_toolkit + Rich）与 Web（FastAPI + React）双前端，并可接入 MCP 协议工具、Code-RAG 知识引擎、子 Agent 等高级能力。

## 架构全景图

```
表现层 (双前端)
  ├─ CLI (prompt_toolkit + Rich)  终端主界面：消息滚动、审批弹窗、自动补全
  └─ Web (FastAPI + React)      本地Web界面：WebSocket推送、折叠思考看板

网关层 (事件路由)
  └─ ChaChaAsyncGateway         JSON‑RPC 2.0 统一事件总线

核心编排层 (微内核控制平面)
  ├─ Orchestrator               主循环状态机
  ├─ LLMInvoker                 流式调用与tool_call解析
  ├─ ToolExecutor               权限检查与工具执行
  ├─ ContextManager             上下文组装与压缩触发
  ├─ HookOrchestrator           责任链钩子编排
  ├─ OutputGovernor             流式JSON修复与输出拦截
  ├─ PolicyEngine               安全策略与成本控制
  └─ Telemetry                  结构化日志与指标

模型管理层
  ├─ OpenAI / Anthropic / Ollama 客户端适配
  ├─ ModelRouter                路由策略（优先级/成本/随机）
  ├─ UsageTracker               Token计数与成本熔断
  └─ RetryHandler               指数退避重试

记忆与上下文子系统
  ├─ StaticRuleLoader           分层加载CHACHA.md（支持@import）
  ├─ MemoryManager              读写MEMORY.md / CHACHA_MEMORY.md / Topics
  ├─ DreamPipeline              项目级记忆整合（每N轮或定时）
  ├─ GlobalDream                用户级跨项目永久记忆整合
  ├─ ContextAssembler           优先级排序组装
  └─ ContextCompressor          混合压缩（修剪 + LLM摘要）

能力与插件层
  ├─ 内置技能                   精准修补、流式读取、记忆工具、HTTP等
  ├─ OpenClaw 技能加载器        懒加载社区Markdown技能
  ├─ MCP 客户端                 跨进程stdio管理
  ├─ Code‑RAG 引擎              LanceDB语义检索 + Tree-sitter符号图
  └─ 沙箱执行器                 PTY/Docker隔离
```

> 完整架构文档见 [docs/architecture.md](docs/architecture.md)

## 快速开始

### 环境要求

- Python ≥ 3.10
- Git 已安装并配置
- 终端编码 UTF-8

### 安装

```bash
git clone https://github.com/VerifyL/chachaAgent.git
cd chachaAgent
pip install -e "."            # 最小安装
# pip install -e ".[dev]"     # 含测试/格式化工具
```

### 启动

```bash
# CLI 终端界面（默认）
chacha /path/to/project

# 或在项目目录中直接运行
cd /path/to/project && chacha
```

### 配置

首次启动自动生成 `~/.chacha/config.toml`，详细配置项见 [docs/configuration.md](docs/configuration.md)。

主要配置项包括：

| 配置段 | 说明 |
|--------|------|
| `[model]` | 模型提供商、API Key、参数设定 |
| `[context]` | Token 预算、压缩策略 |
| `[sandbox]` | 命令白名单、超时限制 |
| `[policy]` | 安全策略、成本上限 |
| `[hooks]` | 声明式钩子规则 |
| `[mcp]` | MCP Server 连接配置 |
| `[multimodal]` | 多模态预留（v1.5+） |

详细说明见 [docs/configuration.md](docs/configuration.md)

## 项目结构

```
ChachaAgent/
├── .chacha_agent/       运行时数据（检查点、记忆、RAG存储、日志）
├── protocol/            通信与网关层
├── core/                核心编排层 + 模型管理 + 上下文子系统
├── capabilities/        能力与插件层（内置技能、MCP、RAG、沙箱）
├── interface/cli/       prompt_toolkit + Rich 终端界面
├── interface/web/       FastAPI + React Web 界面
├── tests/               测试套件（单元/集成/基准/模糊/评测）
├── scripts/             运维辅助脚本
├── docs/                项目文档
├── examples/            示例配置与脚本
└── pyproject.toml       项目元数据与依赖
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [架构设计](docs/architecture.md) | 系统架构、数据流、模块职责 |
| [开发指南](docs/development.md) | 环境搭建、调试、新增工具开发 |
| [配置详解](docs/configuration.md) | 所有配置项说明、环境变量、安全策略 |
| [钩子开发](docs/hook_orchestrator.md) | 钩子类型、责任链顺序、自定义钩子示例 |
| [记忆系统](docs/memory.md) | 记忆分层设计、DreamPipeline、GlobalDream、Topic 工具 |
| [上下文组装](docs/context.md) | 上下文字段顺序、压缩策略、BlockSource |
| [上下文管理器](docs/context_manager.md) | ContextManager 组装流程、Token 预算、DYNAMIC_BOUNDARY |
| [模型管理](docs/model.md) | 模型提供商、切换方法、用量追踪与成本控制 |

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

---

**ChaChaAgent** — Build smart, stay in control.
