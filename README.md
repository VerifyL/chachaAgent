# ChaChaAgent

通用 AI Agent 框架 — 微内核架构，支持多模型、多工具、多前端。

## 项目简介

ChaChaAgent 是一个可扩展的通用 AI Agent 框架，提供从模型调用、上下文管理到工具执行的全链路编排。采用**微内核控制平面**设计，核心编排层与能力插件层解耦。

**当前实现状态 (v3.1.4):**
- ✅ CLI 前端 (prompt_toolkit + Rich) — 完整可用
- ✅ `Orchestrator.run_stream()` 统一编排入口 — 13 步流水线 (Hook/Policy/Gateway/并发工具)
- ✅ OpenAI / DeepSeek 兼容 API 流式调用 — 含 reasoning_content 支持
- ✅ 21 个内置工具 — 文件读写、代码搜索、依赖分析、Git、沙箱 bash、记忆、子Agent、审批控制
- ✅ 安全策略引擎 — 加权风险评估 + 命令黑名单 + 成本熔断 + CLI 交互式审批
- ✅ 钩子系统 — 内置 Python 钩子 + 外部 ShellCommand，YAML 声明式规则
- ✅ 记忆系统 — 每日记忆 / 永久记忆 / Topic 主题 / Session 隔离 / Dream 整合
- ✅ JSON-RPC 2.0 网关 — 异步消息总线，背压控制，全局事件监听
- ✅ 遥测系统 — 结构化日志 + 指标收集 (counter/gauge/histogram) + Span 追踪
- ✅ 模型路由 — priority/cost/random 三策略 + 故障隔离 + 降级链
- ✅ 用量追踪 — 按模型统计 token 消耗和成本
- 🚧 Web 前端 — 目录已创建，待实现
- 🚧 多模态 — 配置模型已预留，待实现
- 🚧 Anthropic 客户端 — 待实现（当前仅 OpenAI 兼容）
- 🚧 Code-RAG 引擎 — 骨架已创建，待实现
- 🚧 MCP 客户端 — 骨架已创建，待实现，缺少 stdio 通信

## 架构全景图

```
表现层
  ├─ CLI (prompt_toolkit + Rich) ✅  终端：消息滚动、审批弹窗、session管理、快捷键
  └─ Web (FastAPI + React)      🚧  目录占位，待实现

网关层
  └─ ChaChaAsyncGateway  ✅  JSON-RPC 2.0 异步消息总线，背压控制

核心编排层 (微内核)
  ├─ Orchestrator       ✅  编排主入口 run_stream() 13步流水线 (v2.1)
  ├─ ChatEngine         ✅  消息存储 + 检查点持久化 (v2.1 降级)
  ├─ Dispatcher         ✅  LLM↔工具桥接 (v2.1 并发 + Circuit Breaker)
  ├─ LLMInvoker         ✅  流式调用 + tool_call 增量解析 + 异常映射 + 重试
  ├─ ToolExecutor       ✅  策略审批 + 钩子 + 超时重试 + 并发 + 遥测
  ├─ ContextManager     ✅  双区组装(protected/dynamic)，Token 预算感知
  ├─ PolicyEngine       ✅  加权风险评估 + 成本熔断 + 审批缓存 + 四级权限
  ├─ HookOrchestrator   ✅  责任链：Python/外部进程双模式，洋葱排序
  ├─ OutputGovernor     ✅  流式 JSON 修复(4策略) + 非法内容拦截
  ├─ RuleEngine         ✅  YAML → HookOrchestrator，冲突检测
  └─ Telemetry          ✅  结构化日志 + 指标 + Span 追踪 + Prometheus 导出

模型客户端层
  ├─ OpenAIClient       ✅  OpenAI / DeepSeek / Ollama / Qwen 兼容 API
  ├─ RetryHandler       ✅  指数退避重试
  ├─ ModelRouter        🚧  路由策略待实现
  ├─ ModelFactory       🚧  工厂模式待实现
  └─ UsageTracker       🚧  精确成本计算待实现

记忆与上下文子系统
  ├─ MemoryManager      ✅  每日会话 / 永久记忆 / Topic 主题 / Session 隔离
  ├─ StaticRuleLoader   ✅  分层加载 ~/.chacha/CHACHA.md + {project}/CHACHA.md
  ├─ DreamPipeline      ✅  项目级记忆整合（每 N 轮或定时）
  ├─ GlobalDream        ✅  用户级跨项目永久记忆整合
  ├─ ContextCompressor  ✅  混合压缩（FROZEN→TRIMMED→SUMMARIZED→CONSOLIDATED）
  ├─ Summarizer         ✅  LLM 摘要压缩
  └─ TokenCounter       ✅  Token 估算

能力与插件层
  ├─ 17 个内置工具     ✅  read_file/write/grep/bash/git/memory/subagent/code_intel…
  ├─ 沙箱执行器         ✅  subprocess 隔离 + 环境白名单 + 资源限制 + 进程组隔离
  ├─ SubAgent 孵化器    ✅  explore/plan/worker 三种子Agent
  ├─ MCP 客户端         🚧  骨架，待实现 stdio 通信
  ├─ Code-RAG 引擎      🚧  骨架(symbol_parser/vector_store)，待实现
  ├─ OpenClaw 加载器    🚧  骨架，待实现
  └─ 插件安装器         🚧  骨架，待实现
```

> 🚧 = 骨架/占位，待实现。完整架构文档见 [docs/architecture.md](docs/architecture.md)

## 快速开始

### 环境要求

- Python ≥ 3.10
- Git 已安装并配置
- 终端编码 UTF-8

### 安装

```bash
git clone https://github.com/VerifyL/chachaAgent.git
cd chachaAgent
pip install -e "."

# 设置 API Key
export DEEPSEEK_API_KEY="sk-your-key"
```

### 启动

```bash
# 在项目目录中启动 CLI
cd /path/to/your/project && chacha

# 或指定项目路径
chacha /path/to/project
```

CLI 快捷键：`Ctrl+N` 新会话 | `Ctrl+S` 保存 | `Ctrl+B` 会话列表 | `Ctrl+X` 压缩上下文 | `Ctrl+J` 换行 | `Ctrl+D` 退出

### 配置

首次启动自动生成 `~/.chacha/config.toml` 和 `~/.chacha/CHACHA.md`。

主要配置项 (`chachaConfig.toml`):

| 配置段 | 状态 | 说明 |
|--------|------|------|
| `[model.providers.default]` | ✅ | 模型提供商、API Key、模型名、上下文窗口 |
| `[context]` | ✅ | Token 预算、压缩触发比例、各层保留参数 |
| `[sandbox]` | ✅ | 命令白名单、超时限制 |
| `[policy]` | ✅ | 命令黑名单、成本上限、审批缓存 TTL |
| `[telemetry]` | ✅ | 日志级别、审计开关、Prometheus 端口 |
| `[multimodal]` | 🚧 | 多模态预留（v1.5+） |
| `[interface]` | 🚧 | Web 服务器配置预留 |
| `[auto_memory]` | ✅ | Dream/GlobalDream 触发阈值 |

详细说明见 [docs/configuration.md](docs/configuration.md)

## 项目结构

```
ChachaAgent/
├── core/                    核心编排层
│   ├── orchestrator.py      编排主入口 (run_stream 13步流水线)
│   ├── chat_engine.py       消息存储 + 检查点 (v2.1 降级)
│   ├── dispatcher.py        LLM↔工具桥接 (v2.1 并发)
│   ├── llm_invoker.py       流式 LLM 调用器
│   ├── tool_executor.py     工具执行调度器
│   ├── context_manager.py   上下文组装管理器
│   ├── policy_engine.py     安全策略引擎
│   ├── hook_orchestrator.py 钩子责任链引擎
│   ├── output_governor.py   流式JSON修复+内容拦截
│   ├── rule_engine.py       YAML声明式规则引擎
│   ├── telemetry.py         统一可观测性
│   ├── config_manager.py    配置加载/热重载
│   ├── checkpoint_manager.py 会话检查点
│   ├── session_service.py   会话编排服务
│   ├── project_init.py      项目初始化器
│   ├── environment_validator.py 环境校验
│   ├── cli_theme.py         CLI 主题
│   ├── llm_clients/         LLM 客户端适配器
│   │   ├── openai_client.py  OpenAI/DeepSeek 适配器 ✅
│   │   ├── retry_handler.py  重试处理器 ✅
│   │   ├── factory.py        工厂 🚧
│   │   ├── router.py         路由器 🚧
│   │   └── usage_tracker.py  用量追踪 🚧
│   ├── context/             记忆与上下文子系统
│   │   ├── memory_manager.py    记忆文件I/O ✅
│   │   ├── context_compressor.py 上下文压缩 ✅
│   │   ├── dream.py             DreamPipeline ✅
│   │   ├── global_dream.py      GlobalDream ✅
│   │   ├── summarizer.py        LLM摘要 ✅
│   │   ├── token_counter.py     Token估算 ✅
│   │   └── static_rule_loader.py CHACHA.md加载 ✅
│   ├── subagent/            子Agent系统
│   │   ├── spawner.py         孵化器 ✅
│   │   ├── definitions.py     类型定义 ✅
│   │   └── __init__.py
│   ├── models/              Pydantic 数据模型
│   │   ├── config.py          配置模型 ✅
│   │   ├── context.py         上下文模型 ✅
│   │   ├── session.py         会话模型 ✅
│   │   ├── hook.py            钩子模型 ✅
│   │   ├── audit.py           审计模型 ✅
│   │   └── stream_event.py    流式事件 ✅
│   └── debug/               🚧 调试工具 (占位)
├── capabilities/           能力与插件层
│   ├── base.py              BaseTool 抽象基类 ✅
│   ├── registry.py          工具注册表 ✅
│   ├── sandbox.py           沙箱执行器 ✅
│   ├── mcp_client.py        MCP 客户端 🚧
│   ├── plugin_installer.py  插件安装器 🚧
│   ├── openclaw_loader.py   OpenClaw 加载器 🚧
│   ├── atomic_writer.py     原子写入工具 ✅
│   ├── builtins/            内置工具 ✅
│   │   ├── chunk_streamer.py   文件读取 (read_file/read_files)
│   │   ├── code_patcher.py     代码编辑 (edit_file)
│   │   ├── code_intel.py       代码智能分析
│   │   ├── depe_analyzer.py    依赖分析
│   │   ├── file_outline.py     文件骨架提取
│   │   ├── list_files.py       目录列表
│   │   ├── project_overview.py 项目总览
│   │   ├── git_tools.py        Git 工具 (diff/log/status)
│   │   ├── git_context.py      Git上下文钩子
│   │   ├── memory_tool.py      记忆工具 (load/remember/write_topic/read_topic)
│   │   ├── subagent_tool.py    子Agent调度工具
│   │   └── http_tool.py        HTTP 请求工具
│   ├── multimodal/          🚧 多模态 (占位)
│   └── rag/                 🚧 Code-RAG (骨架)
├── protocol/               通信与网关层
│   ├── gateway.py           ChaChaAsyncGateway ✅
│   └── rpc_schema.py        JSON-RPC 2.0 消息模型 ✅
├── interface/              表现层
│   ├── cli/
│   │   ├── app.py           CLI 主程序 (prompt_toolkit+Rich) ✅
│   │   └── agent_bridge.py  CLI↔核心桥接层 ✅
│   └── web/                 🚧 Web 前端 (占位)
│       ├── static/           (仅 __init__.py)
│       └── templates/        (仅 __init__.py)
├── tests/                  测试套件
│   ├── unit/                单元测试 (40+ 文件)
│   ├── integration/         集成测试 (20+ 文件)
│   ├── benchmark/           🚧 基准测试 (占位)
│   ├── evaluation/          🚧 评测 (占位)
│   ├── fuzz/                🚧 模糊测试 (占位)
│   └── mocks/               🚧 Mock (占位)
├── scripts/                运维脚本
│   └── init_project.py     项目初始化脚本
├── docs/                   项目文档
├── examples/               示例
└── pyproject.toml          项目元数据与依赖
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
| [模型管理](docs/model.md) | 模型提供商、切换方法、用量追踪与成本控制 |

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

---

**ChaChaAgent** — Build smart, stay in control.
