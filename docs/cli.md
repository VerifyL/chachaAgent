# ChachaAgent CLI

基于 prompt_toolkit + Rich 的命令行界面。Enter 发送，Ctrl+J 换行，支持多行粘贴/编辑。

## 运行

```bash
# 安装
pip install -e .

# 配置（可选，三选一）
# 方式 1: 环境变量
export DEEPSEEK_API_KEY=sk-...

# 方式 2: 全局配置
cat > ~/.chacha/config.toml << 'EOF'
[model.providers.default]
api_key = "sk-..."
base_url = "https://api.deepseek.com"
default_model = "deepseek-v4-pro"
EOF

# 方式 3: 项目配置
# {project}/chachaConfig.toml

# 启动
chacha /path/to/project
```

## 配置体系

```
~/.chacha/
  config.toml         ← 全局配置（API Key、模型、dream 阈值、max_tokens）
  clirc.toml          ← CLI 主题配色（首次自动生成，可自定义）
  CHACHA.md           ← 全局宪法（首次自动生成默认模板）
  cli_history         ← 命令历史

{project}/
  chachaConfig.toml   ← 项目级配置（覆盖全局）
  CHACHA.md           ← 项目级宪法（覆盖全局）
```

优先级: 项目级 → 用户级 → 环境变量 → 默认值

## 主题配色

`~/.chacha/clirc.toml`（删除后自动重新生成默认模板）：

```toml
[theme]
user_border = "bold yellow"
user_text = "bold yellow"
user_title = "bold reverse yellow"
agent_header = "bold cyan"
tool_thinking = "bold cyan"
tool_done = "bold bright_white"
tool_error = "bold red"
help_title = "bold reverse bright_white"
help_cmd = "bold yellow"
help_desc = "yellow"
separator = "dim"
audit = "dim"
system = "dim"
prompt = "bold"
```

可用样式: `bold`, `italic`, `underline`, `reverse`
可用颜色: `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white` + `bright_` 前缀

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Enter` | 发送消息 |
| `Ctrl+J` | 插入换行（多行输入） |
| `Ctrl+C` | 中断当前回答 |
| `Ctrl+D` | 退出程序 |
| `Ctrl+N` | 新 Session |
| `Ctrl+S` | 保存 checkpoint |
| `Ctrl+F` | 切换调试模式 |
| `Ctrl+B` | 会话列表 |
| `Ctrl+X` | 压缩上下文 |
| `Ctrl+L` | 清屏 |
| `Ctrl+R` | 切换推理过程显示 |
| `Ctrl+T` | 遥测仪表盘 |
| `Ctrl+\` | 强制退出 |

## 命令

### 配置
| 命令 | 功能 |
|------|------|
| `/model <name>` | 切换模型 |
| `/url <url>` | 切换 API URL |
| `/key <sk->` | 设置 API Key |
| `/status` | 系统状态 |
| `/exit` | 退出（状态已自动保存，无需额外操作） |

### Session
| 命令 | 功能 |
|------|------|
| `/session` | 列出所有 session |
| `/session <id>` | 切换到指定 session |
| `/session del <id>` | 删除 session（含记忆） |
| `/session new` | 新建 session |
| `/new` | 新建 session |
| `/save` | 保存 checkpoint |

### 记忆
| 命令 | 功能 |
|------|------|
| `/memory` | 查看记忆文件 |
| `/dream` | 运行 Session Dream |
| `/dreamglobal` | 运行 GlobalDream |

### 调试
| 命令 | 功能 |
|------|------|
| `/debug` | 切换调试（开启后显示工具链） |
| `/telemetry` | 遥测仪表盘（P50/P99/成本） |
| `/telemetry on/off` | 运行时开关遥测 |
| `/logs [n] [level] [kw]` | 查看/过滤调试日志 |
| `/auditlog [n]` | 查看审计日志 |
| `/trace` | Span 追踪链 |
| `/cost` | API 成本汇总 |
| `/compact` | 压缩上下文 |
| `/status` | 系统状态报告 |

## 自动行为

| 时机 | 动作 |
|------|------|
| 启动 | 加载 CHACHA.md + 配置 + 主题 |
| 每轮结束 | 审计行 + 自动 save checkpoint |
| 切 session | 保存旧 checkpoint + Dream 旧 session |
| Session Dream | 10 轮 / 24h / 切 session 前 |
| Global Dream | 50 轮 / 72h / 手动 |

## 视觉布局

```
╭─ ❯ You ───────────────────────────╮
│ 读取 main.py                       │  ← 用户输入（Panel，可配主题）
╰────────────────────────────────────╯

🤖 Chacha                             ← Agent 标签（可配主题）
  🔧 read_file                        ← 工具调用（可配主题）
  ✅ read_file — main.py: hello       ← 工具结果（可配主题）

文件内容分析...                        ← 正文

⏱ 1245ms | 💬 352T | 📦 37% | 📥 +128T | 🔄 3轮  ← 审计（可配主题）
│  │  API total tokens  │ 上下文利用率  │ 缓存命中  │
──────────────────────────────────    ← 分隔线
```

## 架构

```
app.py (CLI 层)
  └─ agent_bridge.py (编排层)
       ├─ ContextCompressor.auto_compact()  ← 每轮结束自动压缩
       ├─ Dispatcher.dispatch_stream()      ← LLM + 工具循环
       └─ SessionService.add_round()        ← 审计 + 记忆
```

| 文件 | 说明 |
|------|------|
| `app.py` | prompt_toolkit + Rich 主 CLI |
| `agent_bridge.py` | CLI ↔ 核心编排，含自动压缩 |
| `core/context/context_compressor.py` | 四层渐进压缩：FROZEN → TRIMMED → SUMMARIZED → CONSOLIDATED |
| `core/cli_theme.py` | 主题加载（~/.chacha/clirc.toml） |
| `core/config_manager.py` | 配置加载（自动生成 ~/.chacha/config.toml） |
| `core/session_service.py` | Session 编排 + Dream + 审计 |
| `core/project_init.py` | CHACHA.md 加载 + 工具工厂 |

## 首次运行

启动 `chacha` 自动创建三个默认文件（后续改完重启生效）：

| 文件 | 内容 |
|------|------|
| `~/.chacha/config.toml` | API Key、模型、dream 阈值、max_tokens |
| `~/.chacha/CHACHA.md` | 默认宪法（安全规则、代码规范等） |
| `~/.chacha/clirc.toml` | CLI 主题配色（每个区域均可配色） |

### 配置示例

```toml
# ~/.chacha/config.toml
[model.providers.default]
provider = "openai"
api_key = "sk-..."
base_url = "https://api.deepseek.com"
default_model = "deepseek-v4-pro"
# context_window = 1048576    # 上下文窗口（自动根据模型名推断）
# max_tokens = 131072         # 输出上限（默认 16384）

[context]
# 上下文压缩（自动 /compact 均生效）
compression_trigger_ratio = 0.7     # 超过 70% 窗口触发压缩
warn_ratio = 0.9                    # 超过 90% 触发警告

# 压缩参数
trim_keep_head = 5                  # TRIMMED: 保留前 N 条
trim_keep_tail = 12                 # TRIMMED: 保留后 N 条
summarize_keep_head = 3             # SUMMARIZED: 保留前 N 条
summarize_keep_tail = 8             # SUMMARIZED: 保留后 N 条

[telemetry]
# enabled = true               # 开启结构化日志 + 审计（默认关闭）
# log_level = "INFO"           # DEBUG 可查看详细日志

[dream]
dream_rounds = 15
global_dream_rounds = 100
```

优先级: `~/.chacha/config.toml` → `{project}/chachaConfig.toml` → 环境变量 → 默认值

## 上下文压缩

| 级别 | 动作 | 触发 |
|------|------|------|
| FROZEN | > N 个工具结果时，最旧的冻结为占位符（文件系统已缓存） | 自动 + 手动 |
| TRIMMED | 保 system + 前 N + 后 M 条，中间裁剪 | 自动 + 手动 |
| SUMMARIZED | 保 system + 前 N + 后 M 条，中间 LLM 摘要 | 自动 + 手动 |

逐级降落，直到总数低于`compression_trigger_ratio` 窗口比例。`agent_bridge.send_message()` 每轮结束后自动调用。

## 可观测性 (Telemetry)

默认关闭，`config.toml` 中 `[telemetry] enabled = true` 开启。

```
~/.chacha/logs/
  debug.jsonl      ← 结构化调试日志（JSONL，每行一条事件）
  audit.jsonl      ← 安全审计日志（工具调用记录）
```

```json
// debug.jsonl 示例
{"ts":"...","level":"INFO","session":"20260623-1051","msg":"Telemetry 已启动"}
{"ts":"...","level":"INFO","session":"20260623-1051","msg":"工具调用","tool":"write_topic","duration_ms":1}
{"ts":"...","level":"INFO","session":"20260623-1051","msg":"本轮完成","round":1,"tokens":352}
{"ts":"...","level":"INFO","session":"20260623-1051","msg":"LLM 调用","tokens":352,"duration_ms":2800}
```

指标 `MetricsCollector` 支持 counter / gauge / histogram + P50/P99 百分位，可对接 Prometheus。
Span 追踪 `Tracer` 支持单进程 trace_id → span_id 全链路关联。

## 当前架构集成状态

以下组件已在  13 步流水线中**全部接入**，无需额外配置：

| 组件 | 文件 | 集成方式 |
|------|------|----------|
| `Orchestrator` | `core/orchestrator.py` | ✅ 主入口，13 步流水线 |
| `HookOrchestrator` | `core/hook_orchestrator.py` | ✅ 第 2 步 PRE_CONTEXT_ASSEMBLY |
| `PolicyEngine` | `core/policy_engine.py` | ✅ 第 3 步策略检查 + 工具审批 |
| `ChaChaAsyncGateway` | `protocol/gateway.py` | ✅ 第 4/12 步事件发布 |
| `ContextManager` | `core/context_manager.py` | ✅ 第 5 步上下文组装 |
| `Dispatcher` | `core/dispatcher.py` | ✅ 第 6 步 LLM ↔ 工具桥接 |
| `ContextCompressor` | `core/context/context_compressor.py` | ✅ 第 7 步自动压缩 |
| `ModelRouter` | `core/llm_clients/router.py` | ✅ 模型选择 + 故障转移 |
| `ModelFactory` | `core/llm_clients/factory.py` | ✅ 客户端工厂创建 |
| `UsageTracker` | `core/llm_clients/usage_tracker.py` | ✅ 按模型统计 token/成本 |
| `TokenCounter` | `core/context/token_counter.py` | ✅ ContextManager 内部 |
| `SubAgentSpawner` | `core/subagent/spawner.py` | ✅ task 工具触发 |
| `MemoryManager` | `core/context/memory_manager.py` | ✅ 第 11 步会话记忆 |
| `DreamPipeline` | `core/context/dream.py` | ✅ 第 13 步条件触发 |
